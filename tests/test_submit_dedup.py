"""受理幂等性：重复请求返回同一预约，关键字段变化进入人工确认。"""

import unittest

from src.railway import RailwayServiceBackend, ServiceStatus, SubmitOutcome
from tests.railway_factory import (
    HZH,
    NKN,
    WZN,
    build_network,
    build_staff,
    dt,
    luggage_request,
    pet_request,
)


class SubmitDedupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = RailwayServiceBackend(build_network())
        for staff in build_staff():
            self.backend.register_staff(staff)

    def test_same_request_id_returns_same_reservation(self) -> None:
        request = pet_request("REQ-DUP-1")
        first = self.backend.submit(request, dt(4, 9))
        second = self.backend.submit(request, dt(4, 9, 5))
        self.assertEqual(first.outcome, SubmitOutcome.CREATED)
        self.assertEqual(second.outcome, SubmitOutcome.DUPLICATE)
        self.assertEqual(first.reservation_id, second.reservation_id)

    def test_identical_retry_with_new_request_id_still_deduped(self) -> None:
        first = self.backend.submit(pet_request("REQ-DUP-2"), dt(4, 9))
        retry = self.backend.submit(pet_request("REQ-DUP-2-RETRY"), dt(4, 9, 10))
        self.assertEqual(retry.outcome, SubmitOutcome.DUPLICATE)
        self.assertEqual(retry.reservation_id, first.reservation_id)
        self.assertEqual(len(self.backend.get(first.reservation_id).tasks), 3)

    def test_train_change_enters_manual_review_and_names_field(self) -> None:
        first = self.backend.submit(pet_request("REQ-CHG-1", train_code="G1"), dt(4, 9))
        changed = self.backend.submit(pet_request("REQ-CHG-2", train_code="G2"), dt(4, 9, 20))
        self.assertEqual(changed.outcome, SubmitOutcome.PENDING_REVIEW)
        self.assertEqual(changed.reservation_id, first.reservation_id)
        self.assertEqual(changed.changed_fields, ("车次",))
        reservation = self.backend.get(first.reservation_id)
        self.assertEqual(reservation.status, ServiceStatus.PENDING_REVIEW)
        # 未确认前仍按原 G1 计划执行：发运窗在 7:50 截止，而非 G2 的 9:20。
        consign = reservation.plan_tasks(reservation.request.service_type)[0]
        self.assertEqual(consign.window_end, dt(5, 7, 50))

    def test_second_change_while_pending_is_rejected(self) -> None:
        self.backend.submit(pet_request("REQ-CHG-3", train_code="G1"), dt(4, 9))
        self.backend.submit(pet_request("REQ-CHG-4", train_code="G2"), dt(4, 9, 20))
        from src.railway import ConflictError

        with self.assertRaises(ConflictError):
            self.backend.submit(pet_request("REQ-CHG-5", train_code="G2"), dt(4, 9, 30))

    def test_approve_change_reschedules_and_bumps_revision(self) -> None:
        first = self.backend.submit(pet_request("REQ-CHG-6", train_code="G1"), dt(4, 9))
        self.backend.submit(pet_request("REQ-CHG-7", train_code="G2"), dt(4, 9, 20))
        self.backend.review_change(first.reservation_id, True, "主管乙", dt(4, 10))
        reservation = self.backend.get(first.reservation_id)
        self.assertEqual(reservation.request.train_code, "G2")
        self.assertEqual(reservation.revision, 2)
        self.assertEqual(reservation.status, ServiceStatus.CONFIRMED)
        # 发运受理窗随 G2 的 9:30 发车移动到 8:30 开始。
        consign = reservation.plan_tasks(reservation.request.service_type)[0]
        self.assertEqual(consign.window_start, dt(5, 8, 30))

    def test_reject_change_keeps_original_plan(self) -> None:
        first = self.backend.submit(pet_request("REQ-CHG-8", train_code="G1"), dt(4, 9))
        self.backend.submit(pet_request("REQ-CHG-9", train_code="G2"), dt(4, 9, 20))
        self.backend.review_change(first.reservation_id, False, "主管乙", dt(4, 10))
        reservation = self.backend.get(first.reservation_id)
        self.assertEqual(reservation.request.train_code, "G1")
        self.assertEqual(reservation.status, ServiceStatus.CONFIRMED)

    def test_luggage_piece_change_is_manual_review_not_new_reservation(self) -> None:
        first = self.backend.submit(luggage_request("REQ-LC-1", item_ids=("L1", "L2")), dt(4, 9))
        changed = self.backend.submit(luggage_request("REQ-LC-2", item_ids=("L1", "L2", "L3")), dt(4, 9, 20))
        self.assertEqual(changed.outcome, SubmitOutcome.PENDING_REVIEW)
        self.assertEqual(changed.reservation_id, first.reservation_id)
        self.assertIn("行李件", changed.changed_fields)


if __name__ == "__main__":
    unittest.main()
