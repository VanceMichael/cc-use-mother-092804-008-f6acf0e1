"""现场交接生命周期、件数核对、临时证明有效期与责任链还原。"""

import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta

from src.models import ServiceType, TaskStatus
from src.service import ServiceError
from src import views

from tests.factories import DAY, NOW, booking_raw, make_service, tasks_of


def at(hour: int, minute: int = 0) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hour:02d}:{minute:02d}:00")


class HandoverLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        self.booking = self.svc.submit_booking(booking_raw(), NOW).booking
        self.bid = self.booking.booking_id
        self.svc.verify_eligibility(self.bid, ServiceType.KEY_PASSENGER, True, "M-VNP", NOW)

    @contextmanager
    def assert_service_error(self, code: str):
        with self.assertRaises(ServiceError) as ctx:
            yield
        self.assertEqual(ctx.exception.code, code)

    def _service_tasks(self, service_type: str):
        ordered = sorted(tasks_of(self.booking, service_type), key=lambda t: t.seq)
        self.assertTrue(ordered)
        return ordered

    def test_luggage_chain_enforces_piece_conservation(self) -> None:
        pickup, delivery = self._service_tasks("luggage")
        self.svc.start_task(self.bid, pickup.task_id, pickup.assignee_id, at(6, 30))
        # 漏交一件：拒绝交接
        with self.assert_service_error("piece_count_mismatch"):
            self.svc.perform_handover(
                self.bid, pickup.task_id, pickup.assignee_id, at(6, 35), ["L1"]
            )
        # 凭空多交一件：拒绝
        with self.assert_service_error("piece_count_mismatch"):
            self.svc.perform_handover(
                self.bid, pickup.task_id, pickup.assignee_id, at(6, 36), ["L1", "L2", "L9"]
            )
        handover = self.svc.perform_handover(
            self.bid, pickup.task_id, pickup.assignee_id, at(6, 40), ["L2", "L1"]
        )
        self.assertIs(pickup.status, TaskStatus.COMPLETED)
        self.assertIs(delivery.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(handover.to_staff_id, delivery.assignee_id)
        self.svc.complete_task(self.bid, delivery.task_id, delivery.assignee_id, at(13, 50))
        self.assertIs(delivery.status, TaskStatus.COMPLETED)

    def test_only_assignee_may_act_and_middle_segment_must_handover(self) -> None:
        pickup, delivery = self._service_tasks("pet")
        with self.assert_service_error("not_assignee"):
            self.svc.start_task(self.bid, pickup.task_id, "S-VNP-99", at(7, 0))
        self.svc.start_task(self.bid, pickup.task_id, pickup.assignee_id, at(7, 0))
        # 首段后面还有环节：不能直接完成，必须交接
        with self.assert_service_error("has_next_segment"):
            self.svc.complete_task(self.bid, pickup.task_id, pickup.assignee_id, at(7, 5))
        # 末段没有下一段：不能交接，只能完成
        with self.assert_service_error("last_segment"):
            self.svc.perform_handover(
                self.bid, delivery.task_id, delivery.assignee_id, at(13, 30), ["猫:团子"]
            )

    def test_completed_task_is_immutable(self) -> None:
        pickup, _ = self._service_tasks("pet")
        self.svc.start_task(self.bid, pickup.task_id, pickup.assignee_id, at(7, 0))
        self.svc.perform_handover(
            self.bid, pickup.task_id, pickup.assignee_id, at(7, 10), ["猫:团子"]
        )
        with self.assert_service_error("task_completed"):
            self.svc.start_task(self.bid, pickup.task_id, pickup.assignee_id, at(7, 11))

    def test_key_passenger_relay_across_three_stations(self) -> None:
        pickup, transfer, delivery = self._service_tasks("key_passenger")
        self.assertEqual(transfer.station_code, "JGK")
        self.svc.start_task(self.bid, pickup.task_id, pickup.assignee_id, at(7, 20))
        h1 = self.svc.perform_handover(
            self.bid, pickup.task_id, pickup.assignee_id, at(9, 45), ["轮椅旅客林晚"]
        )
        self.assertEqual(h1.station_code, "JGK")
        self.svc.perform_handover(
            self.bid, transfer.task_id, transfer.assignee_id, at(9, 50), ["轮椅旅客林晚"]
        )
        self.svc.complete_task(self.bid, delivery.task_id, delivery.assignee_id, at(13, 45))
        statuses = [t.status for t in (pickup, transfer, delivery)]
        self.assertEqual(statuses, [TaskStatus.COMPLETED] * 3)
        self.assertEqual(len(self.booking.handovers), 2)

    def test_certificate_issued_with_expiry_and_validated(self) -> None:
        verify, handover = self._service_tasks("temp_id")
        self.svc.start_task(self.bid, verify.task_id, verify.assignee_id, at(6, 35))
        h = self.svc.perform_handover(
            self.bid, verify.task_id, verify.assignee_id, at(6, 50), ["临时身份证明"]
        )
        need = self.booking.need_for(ServiceType.TEMP_ID)
        self.assertTrue(need.certificate_no)
        self.assertEqual(need.expires_at, need.issued_at + timedelta(hours=24))
        self.assertTrue(h.objects)

        # 有效期内核验通过
        info = self.svc.verify_certificate(need.certificate_no, at(8, 0))
        self.assertTrue(info["valid"])
        # 过期后不可交付/核验
        expired_moment = need.expires_at + timedelta(minutes=1)
        info = self.svc.verify_certificate(need.certificate_no, expired_moment)
        self.assertFalse(info["valid"])
        with self.assert_service_error("certificate_expired"):
            self.svc.complete_task(
                self.bid, handover.task_id, handover.assignee_id, expired_moment
            )
        # 有效期内完成交付
        self.svc.complete_task(self.bid, handover.task_id, handover.assignee_id, at(7, 20))

    def test_unknown_certificate_returns_404(self) -> None:
        with self.assert_service_error("certificate_not_found"):
            self.svc.verify_certificate("Z000000-9", NOW)


class ResponsibilityChainTest(unittest.TestCase):
    def test_chain_reconstructs_full_accountability(self) -> None:
        svc = make_service()
        booking = svc.submit_booking(booking_raw(), NOW).booking
        svc.verify_eligibility(booking.booking_id, ServiceType.KEY_PASSENGER, True, "M-VNP", NOW)
        pickup, transfer, delivery = sorted(
            tasks_of(booking, "key_passenger"), key=lambda t: t.seq
        )
        svc.start_task(booking.booking_id, pickup.task_id, pickup.assignee_id, at(7, 20))
        svc.perform_handover(
            booking.booking_id, pickup.task_id, pickup.assignee_id, at(9, 45), ["轮椅旅客林晚"]
        )

        chain = views.responsibility_chain(booking, ServiceType.KEY_PASSENGER)
        segments = chain["chains"][0]["segments"]
        self.assertEqual([s["station"] for s in segments], ["VNP", "JGK", "HGH"])
        first = segments[0]
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["completed_by"], pickup.assignee_id)
        self.assertEqual(first["handover"]["to_staff"], transfer.assignee_id)
        self.assertEqual(first["handover"]["objects"], ["轮椅旅客林晚"])
        # 末段尚未完成
        self.assertEqual(segments[2]["status"], "planned")
        # 事件时间线可还原全过程
        events = [e["event"] for e in chain["timeline"]]
        self.assertIn("booking_submitted", events)
        self.assertIn("eligibility_verified", events)
        self.assertIn("handover", events)

    def test_staff_view_exposes_minimum_information_only(self) -> None:
        svc = make_service()
        booking = svc.submit_booking(booking_raw(), NOW).booking
        svc.verify_eligibility(booking.booking_id, ServiceType.KEY_PASSENGER, True, "M-VNP", NOW)
        pickup = sorted(tasks_of(booking, "key_passenger"), key=lambda t: t.seq)[0]
        assignee = pickup.assignee_id

        view = views.staff_task_view(svc, assignee)
        self.assertTrue(view["tasks"])
        for row in view["tasks"]:
            self.assertNotIn("passengers", row)          # 无同行名单
            self.assertNotIn("idempotency_key", row)
            self.assertIn("objects", row)
        ids = {row["task_id"] for row in view["tasks"]}
        self.assertIn(pickup.task_id, ids)
        self.assertLess(len(ids), len(booking.tasks))     # 远少于预约全部环节

        # 车站值班台视图看到本站所有开放环节，但不含旅客姓名与对象明细
        dash = views.station_dashboard(svc, "VNP")
        self.assertTrue(dash["open_tasks"])
        for row in dash["open_tasks"]:
            self.assertNotIn("passenger_name", row)
            self.assertNotIn("objects", row)


if __name__ == "__main__":
    unittest.main()
