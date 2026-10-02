"""列车调整：按新车次时刻重排未发生环节，已完成环节不动，路线必须被新车次覆盖。"""

import unittest

from src.railway import ConflictError, PolicyViolation, RailwayServiceBackend, ServiceStatus, TaskStatus
from tests.railway_factory import (
    HZH,
    NKN,
    WZN,
    build_network,
    build_staff,
    dt,
    key_passenger_request,
    luggage_request,
)


class TrainAdjustmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = RailwayServiceBackend(build_network())
        for staff in build_staff():
            self.backend.register_staff(staff)

    def test_adjust_train_moves_open_windows(self) -> None:
        result = self.backend.submit(
            luggage_request("REQ-ADJ-1", train_code="G1", route=(NKN, WZN)), dt(4, 9)
        )
        rid = result.reservation_id
        self.backend.adjust_train(rid, "G2", "调度员", dt(4, 14))
        reservation = self.backend.get(rid)
        pickup = reservation.plan_tasks(reservation.request.service_type)[0]
        # G2 南京南 9:30 发车，取件窗随之平移。
        self.assertEqual(pickup.window_start, dt(5, 8, 30))
        self.assertEqual(reservation.revision, 2)
        # 派工信息未派过时为空，环节仍待排班。
        self.assertEqual(pickup.status, TaskStatus.PENDING)

    def test_adjust_keeps_assignee_for_assigned_tasks(self) -> None:
        result = self.backend.submit(
            luggage_request("REQ-ADJ-2", train_code="G1", route=(NKN, HZH)), dt(4, 9)
        )
        rid = result.reservation_id
        reservation = self.backend.get(rid)
        pickup = reservation.plan_tasks(reservation.request.service_type)[0]
        self.backend.assign_task(pickup.task_id, "S-LUG-NKN", dt(5, 7, 30))
        self.backend.adjust_train(rid, "G2", "调度员", dt(4, 14))
        reservation = self.backend.get(rid)
        pickup = reservation.plan_tasks(reservation.request.service_type)[0]
        self.assertEqual(pickup.status, TaskStatus.ASSIGNED)
        self.assertEqual(pickup.assignee_id, "S-LUG-NKN")
        self.assertEqual(pickup.window_start, dt(5, 8, 30))

    def test_adjust_rejected_when_new_train_skips_route(self) -> None:
        result = self.backend.submit(
            luggage_request("REQ-ADJ-3", train_code="G1", route=(NKN, HZH)), dt(4, 9)
        )
        rid = result.reservation_id
        # G3 不停杭州东。
        with self.assertRaises(PolicyViolation):
            self.backend.adjust_train(rid, "G3", "调度员", dt(4, 14))

    def test_completed_task_is_not_rescheduled(self) -> None:
        result = self.backend.submit(
            key_passenger_request("REQ-ADJ-4", verified=True, train_code="G1", route=(NKN, WZN)),
            dt(4, 9),
        )
        rid = result.reservation_id
        reservation = self.backend.get(rid)
        tasks = reservation.plan_tasks(reservation.request.service_type)
        # 完成进站接送（G1 时间窗 7:20-8:00）。
        self.backend.assign_task(tasks[0].task_id, "S-ESC-NKN", tasks[0].window_start)
        self.backend.complete_handover(tasks[0].task_id, "张大爷", tasks[0].window_start)
        # 列车改 G2：已完成的进站接送时刻不动，其余环节按 G2 重排。
        self.backend.adjust_train(rid, "G2", "调度员", dt(5, 8, 5))
        reservation = self.backend.get(rid)
        tasks = reservation.plan_tasks(reservation.request.service_type)
        self.assertEqual(tasks[0].status, TaskStatus.COMPLETED)
        self.assertEqual(tasks[0].window_end, dt(5, 8, 0))
        self.assertTrue(tasks[1].window_start.hour >= 9)
        self.assertEqual(reservation.status, ServiceStatus.IN_PROGRESS)

    def test_adjust_blocked_while_change_pending(self) -> None:
        result = self.backend.submit(
            luggage_request("REQ-ADJ-5", train_code="G1", route=(NKN, HZH)), dt(4, 9)
        )
        rid = result.reservation_id
        self.backend.submit(
            luggage_request("REQ-ADJ-6", train_code="G2", route=(NKN, HZH)), dt(4, 9, 20)
        )
        with self.assertRaises(ConflictError):
            self.backend.adjust_train(rid, "G2", "调度员", dt(4, 14))


if __name__ == "__main__":
    unittest.main()
