"""幂等受理、关键字段变更人工确认、局部取消与改签重排。"""

import unittest
from datetime import datetime

from src.models import BookingStatus, EligibilityStatus, ServiceType, TaskStatus

from tests.factories import DAY, NOW, booking_raw, make_service, need, tasks_of


def at(hour: int, minute: int = 0) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hour:02d}:{minute:02d}:00")


class IdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()

    def test_duplicate_request_returns_same_booking_without_new_tasks(self) -> None:
        raw = booking_raw()
        first = self.svc.submit_booking(raw, NOW).booking
        duplicate = self.svc.submit_booking(booking_raw(), NOW).booking
        self.assertIs(first, duplicate)
        self.assertTrue(self.svc.submit_booking(booking_raw(), NOW).duplicate)

    def test_key_field_change_opens_review_and_blocks_changes(self) -> None:
        raw = booking_raw()
        booking = self.svc.submit_booking(raw, NOW).booking
        changed = booking_raw()
        changed["alight_station"] = "NPH"  # 关键字段：到达站
        result = self.svc.submit_booking(changed, at(6, 10))
        self.assertFalse(result.duplicate)
        self.assertIsNotNone(result.review)
        self.assertIs(booking.status, BookingStatus.PENDING_REVIEW)
        self.assertIn("车次或乘降站", result.review.changed_fields)
        # 等待确认期间，任何改签/取消都被拦截
        from src.service import ServiceError
        with self.assertRaises(ServiceError) as cm:
            self.svc.cancel(booking.booking_id, "M-VNP", at(6, 11), "旅客放弃")
        self.assertEqual(cm.exception.code, "pending_review")
        with self.assertRaises(ServiceError) as cm:
            self.svc.adjust_train(booking.booking_id, "M-VNP", at(6, 11),
                                  main={"alight_station": "HGH"})
        self.assertEqual(cm.exception.code, "pending_review")

    def test_repeated_change_while_pending_updates_single_review(self) -> None:
        self.svc.submit_booking(booking_raw(), NOW)
        changed = booking_raw(alight_station="NPH")
        first = self.svc.submit_booking(changed, at(6, 10)).review
        changed_again = booking_raw(train_code="G205", board_station="VNP", alight_station="HGH")
        second = self.svc.submit_booking(changed_again, at(6, 20)).review
        self.assertEqual(first.review_id, second.review_id)
        self.assertEqual(len(self.svc.repo.list_reviews()), 1)

    def test_reject_review_keeps_original_booking(self) -> None:
        booking = self.svc.submit_booking(booking_raw(), NOW).booking
        changed = booking_raw(alight_station="NPH")
        review = self.svc.submit_booking(changed, at(6, 10)).review
        self.svc.resolve_review(review.review_id, "rejected", "M-VNP", at(6, 15), "维持原行程")
        self.assertIs(booking.status, BookingStatus.CONFIRMED)
        self.assertEqual(booking.itinerary.alight_station, "HGH")

    def test_accept_review_rebuilds_with_fingerprint_updated(self) -> None:
        from src import builder
        booking = self.svc.submit_booking(booking_raw(), NOW).booking
        changed = booking_raw(alight_station="NPH")
        review = self.svc.submit_booking(changed, at(6, 10)).review
        self.svc.resolve_review(review.review_id, "accepted", "M-VNP", at(6, 15), "确认变更")
        self.assertEqual(booking.itinerary.alight_station, "NPH")
        self.assertEqual(booking.key_fingerprint, builder.fingerprint(changed))
        self.assertEqual(booking.version, 2)
        # 确认后的重复请求再次幂等
        self.assertTrue(self.svc.submit_booking(changed, at(6, 16)).duplicate)

    def test_non_key_change_is_treated_as_duplicate(self) -> None:
        # 仅调整偏好时间窗，不进入人工确认
        booking = self.svc.submit_booking(booking_raw(), NOW).booking
        tweaked = booking_raw()
        need(tweaked, "pet")["window_start"] = f"{DAY}T05:30"
        result = self.svc.submit_booking(tweaked, at(6, 5))
        self.assertTrue(result.duplicate)
        self.assertIs(booking.status, BookingStatus.CONFIRMED)


class CancelAndAdjustTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        self.booking = self.svc.submit_booking(booking_raw(), NOW).booking
        self.bid = self.booking.booking_id

    def _verify_key(self) -> None:
        self.svc.verify_eligibility(self.bid, ServiceType.KEY_PASSENGER, True, "M-VNP", NOW)

    def test_partial_cancel_revokes_only_open_segments(self) -> None:
        self._verify_key()
        key_tasks = tasks_of(self.booking, "key_passenger")
        # 完成首段接送
        first = key_tasks[0]
        self.svc.start_task(self.bid, first.task_id, first.assignee_id, at(7, 20))
        self.svc.perform_handover(
            self.bid, first.task_id, first.assignee_id, at(7, 25), ["轮椅旅客林晚"]
        )
        self.svc.cancel(self.bid, "M-VNP", at(9, 0), "旅客改期", ServiceType.KEY_PASSENGER)
        statuses = {t.kind.value: t.status for t in key_tasks}
        self.assertIs(statuses["pickup"], TaskStatus.COMPLETED)   # 已完成，保留可追溯
        self.assertIs(statuses["transfer"], TaskStatus.CANCELLED)
        self.assertIs(statuses["delivery"], TaskStatus.CANCELLED)
        # 其他服务不受影响
        self.assertFalse(self.booking.need_for(ServiceType.PET).cancelled)
        self.assertIsNot(self.booking.status, BookingStatus.CANCELLED)

    def test_cancel_all_services_cancels_booking_but_keeps_history(self) -> None:
        self._verify_key()
        for st in ServiceType:
            self.svc.cancel(self.bid, "M-VNP", at(8, 0), "旅客放弃行程", st)
        self.assertIs(self.booking.status, BookingStatus.CANCELLED)
        # 交接记录与事件仍在
        self.assertEqual(len(self.booking.handovers), 0)
        event_types = {e.event_type for e in self.booking.events}
        self.assertIn("booking_cancelled", event_types)

    def test_completed_segment_survives_train_adjustment(self) -> None:
        self._verify_key()
        key_tasks = tasks_of(self.booking, "key_passenger")
        pickup = key_tasks[0]
        self.svc.start_task(self.bid, pickup.task_id, pickup.assignee_id, at(7, 20))
        self.svc.perform_handover(
            self.bid, pickup.task_id, pickup.assignee_id, at(7, 25), ["轮椅旅客林晚"]
        )
        completed_id = pickup.task_id

        # 列车调整：到达站改为南京南（G101 VNP->NPH）
        self.svc.adjust_train(self.bid, "M-VNP", at(7, 40),
                              main={"alight_station": "NPH"})
        surviving = next(t for t in self.booking.tasks if t.task_id == completed_id)
        self.assertIs(surviving.status, TaskStatus.COMPLETED)
        self.assertEqual(surviving.completed_by, pickup.assignee_id)
        new_key = tasks_of(self.booking, "key_passenger")
        # VNP 首段已完成；G101 到南京南仍经停 JGK，该段已在执行中故保留；
        # 新增 NPH 送达；原 HGH 送达撤销
        active = {t.station_code: t.status for t in new_key if t.status is not TaskStatus.CANCELLED}
        self.assertIs(active["VNP"], TaskStatus.COMPLETED)
        self.assertIs(active["JGK"], TaskStatus.IN_PROGRESS)
        self.assertIs(active["NPH"], TaskStatus.PLANNED)
        self.assertNotIn("HGH", active)
        revoked = {t.station_code for t in new_key if t.status is TaskStatus.CANCELLED}
        self.assertEqual(revoked, {"HGH"})
        self.assertEqual(self.booking.version, 2)

    def test_adjust_pet_separate_train_reschedules_pet_only(self) -> None:
        before = {t.station_code for t in tasks_of(self.booking, "pet")}
        self.svc.adjust_train(
            self.bid, "M-VNP", NOW,
            pet={"train_code": "G205", "board_station": "VNP", "alight_station": "NPH"},
        )
        pet_tasks = [t for t in tasks_of(self.booking, "pet") if t.status is not TaskStatus.CANCELLED]
        self.assertEqual([(t.station_code, t.train_code) for t in pet_tasks],
                         [("VNP", "G205"), ("NPH", "G205")])
        # 原 HGH 到达段被撤销
        revoked = {t.station_code for t in tasks_of(self.booking, "pet")
                   if t.status is TaskStatus.CANCELLED}
        self.assertEqual(revoked, {"HGH"})
        # 主行程和行李未受影响
        self.assertEqual(self.booking.itinerary.train_code, "G101")
        self.assertEqual(
            {t.station_code for t in tasks_of(self.booking, "luggage") if t.is_open},
            before,
        )

    def test_adjustment_violating_rules_is_rejected(self) -> None:
        from src.planning import PlanningError
        with self.assertRaises(PlanningError) as cm:
            self.svc.adjust_train(
                self.bid, "M-VNP", NOW,
                pet={"train_code": "G307", "board_station": "JGK", "alight_station": "HGH"},
            )
        self.assertIn(cm.exception.code, {"station_not_served", "train_not_served"})
        # 原任务未被污染
        self.assertTrue(all(t.is_open or t.status is TaskStatus.COMPLETED
                            for t in self.booking.tasks))


if __name__ == "__main__":
    unittest.main()
