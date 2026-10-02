"""临时身份证明：只能在有效期限内开具，过期环节自动失效。"""

import unittest
from datetime import timedelta

from src.railway import PolicyViolation, RailwayServiceBackend, ServiceStatus, TaskStatus
from tests.railway_factory import (
    HZH,
    NKN,
    build_network,
    build_staff,
    credential_request,
    dt,
)


class TempCredentialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = RailwayServiceBackend(build_network())
        for staff in build_staff():
            self.backend.register_staff(staff)

    def test_issue_within_validity_succeeds(self) -> None:
        result = self.backend.submit(credential_request("REQ-CRED-OK"), dt(4, 9))
        rid = result.reservation_id
        reservation = self.backend.get(rid)
        task = reservation.plan_tasks(reservation.request.service_type)[0]
        self.backend.assign_task(task.task_id, "S-DUTY-NKN", dt(5, 7, 0))
        handover = self.backend.complete_handover(task.task_id, "李先生", dt(5, 7, 30))
        self.assertEqual(handover.item_count, 1)
        self.assertEqual(self.backend.get(rid).status, ServiceStatus.COMPLETED)

    def test_issue_after_validity_refused(self) -> None:
        request = credential_request(
            "REQ-CRED-EXP",
            valid_from=dt(5, 6, 0),
            valid_until=dt(5, 8, 0),  # 与 8:00 发车同时截止
        )
        result = self.backend.submit(request, dt(4, 9))
        rid = result.reservation_id
        reservation = self.backend.get(rid)
        task = reservation.plan_tasks(reservation.request.service_type)[0]
        # 派工在窗口内完成，交接拖到 8:01 被拒。
        self.backend.assign_task(task.task_id, "S-DUTY-NKN", dt(5, 7, 0))
        with self.assertRaises(PolicyViolation):
            self.backend.complete_handover(task.task_id, "李先生", dt(5, 8, 1))
        self.assertEqual(task.status, TaskStatus.ASSIGNED)

    def test_validity_must_cover_departure(self) -> None:
        # G1 南京南 8:00 发车，证明 7:00 即失效，不受理。
        with self.assertRaises(PolicyViolation):
            self.backend.submit(
                credential_request(
                    "REQ-CRED-SHORT",
                    valid_from=dt(5, 6, 0),
                    valid_until=dt(5, 7, 0),
                ),
                dt(4, 9),
            )

    def test_sweep_marks_unissued_credential_expired(self) -> None:
        result = self.backend.submit(
            credential_request("REQ-CRED-SWEEP", valid_until=dt(5, 8, 0)), dt(4, 9)
        )
        rid = result.reservation_id
        expired = self.backend.sweep_expired(dt(5, 8, 1))
        task = self.backend.get(rid).plan_tasks(self.backend.get(rid).request.service_type)[0]
        self.assertIn(task.task_id, expired)
        self.assertEqual(task.status, TaskStatus.EXPIRED)
        self.assertEqual(self.backend.get(rid).status, ServiceStatus.CANCELLED)

    def test_issued_credential_is_not_swept(self) -> None:
        result = self.backend.submit(credential_request("REQ-CRED-DONE"), dt(4, 9))
        rid = result.reservation_id
        task = self.backend.get(rid).plan_tasks(self.backend.get(rid).request.service_type)[0]
        self.backend.assign_task(task.task_id, "S-DUTY-NKN", dt(5, 7, 0))
        self.backend.complete_handover(task.task_id, "李先生", dt(5, 7, 30))
        self.assertEqual(self.backend.sweep_expired(dt(6, 0, 0)), [])


if __name__ == "__main__":
    unittest.main()
