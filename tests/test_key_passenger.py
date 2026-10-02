"""重点旅客：资格核验后才生成分段接力任务，沿停站逐段交接。"""

import unittest

from src.railway import (
    ConflictError,
    PolicyViolation,
    RailwayServiceBackend,
    ServiceStatus,
    TaskKind,
    TaskStatus,
)
from tests.railway_factory import (
    HZH,
    NKN,
    WZN,
    build_network,
    build_staff,
    dt,
    key_passenger_request,
    run_handovers,
)


class KeyPassengerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = RailwayServiceBackend(build_network())
        for staff in build_staff():
            self.backend.register_staff(staff)

    def test_no_tasks_until_eligibility_verified(self) -> None:
        result = self.backend.submit(key_passenger_request("REQ-KP-A"), dt(4, 9))
        reservation = self.backend.get(result.reservation_id)
        self.assertEqual(reservation.status, ServiceStatus.AWAITING_ELIGIBILITY)
        self.assertEqual(reservation.plan_tasks(reservation.request.service_type), [])

    def test_missing_eligibility_is_rejected(self) -> None:
        from src.railway import Person, ServiceType
        from src.railway.models import ServiceRequest, TimeWindow

        request = ServiceRequest(
            request_id="REQ-KP-BAD",
            passenger_id="P1",
            train_code="G1",
            service_type=ServiceType.KEY_PASSENGER,
            window=TimeWindow(dt(5, 6), dt(5, 8)),
            persons=(Person("P1", "无资格旅客"),),
            route=(NKN, HZH),
            target_passenger_id="P1",
        )
        with self.assertRaises(PolicyViolation):
            self.backend.submit(request, dt(4, 9))

    def test_verification_generates_segmented_relay_chain(self) -> None:
        result = self.backend.submit(key_passenger_request("REQ-KP-B", route=(NKN, WZN)), dt(4, 9))
        rid = result.reservation_id
        self.backend.verify_eligibility(rid, "客运值班员甲", dt(4, 10))
        reservation = self.backend.get(rid)
        kinds = [t.kind for t in reservation.plan_tasks(reservation.request.service_type)]
        # 南京南(接送) -> 南京南>杭州东(接力) -> 杭州东>温州南(接力) -> 温州南(送出)
        self.assertEqual(
            kinds,
            [TaskKind.ORIGIN_ESCORT, TaskKind.RELAY_LEG, TaskKind.RELAY_LEG, TaskKind.DEST_ESCORT],
        )
        self.assertEqual(reservation.status, ServiceStatus.CONFIRMED)
        verified = reservation.person("P-ELDER").eligibility
        self.assertTrue(verified.verified)
        self.assertEqual(verified.verified_by, "客运值班员甲")

    def test_handover_requires_predecessor_done(self) -> None:
        result = self.backend.submit(key_passenger_request("REQ-KP-C", verified=True), dt(4, 9))
        rid = result.reservation_id
        tasks = self.backend.get(rid).plan_tasks(self.backend.get(rid).request.service_type)
        # 跳过进站接送，直接尝试杭州东至温州南的车上接力交接。
        second_leg = tasks[2]
        self.backend.assign_task(second_leg.task_id, "S-COND", second_leg.window_start)
        with self.assertRaises(ConflictError):
            self.backend.complete_handover(second_leg.task_id, "旅客", second_leg.window_start)

    def test_completed_escort_remains_traceable_after_partial_cancel(self) -> None:
        result = self.backend.submit(key_passenger_request("REQ-KP-D", verified=True), dt(4, 9))
        rid = result.reservation_id
        reservation = self.backend.get(rid)
        tasks = reservation.plan_tasks(reservation.request.service_type)
        # 完成第一段：进站接送 + 南京南>杭州东接力。
        self.backend.assign_task(tasks[0].task_id, "S-ESC-NKN", tasks[0].window_start)
        self.backend.complete_handover(tasks[0].task_id, "张大爷", tasks[0].window_start)
        self.backend.assign_task(tasks[1].task_id, "S-COND", tasks[1].window_start)
        self.backend.complete_handover(tasks[1].task_id, "张大爷", tasks[1].window_start)
        # 旅客在杭州东到达后、继续发车前临时取消后续。
        cancelled = self.backend.cancel_reservation(rid, "张女", dt(5, 10, 2), "身体不适终止行程")
        reservation = self.backend.get(rid)
        self.assertEqual(len(cancelled), 2)  # 只撤销杭州东>温州南接力与温州南送出
        self.assertEqual(reservation.status, ServiceStatus.PARTIALLY_CANCELLED)
        self.assertEqual(tasks[0].status, TaskStatus.COMPLETED)
        self.assertEqual(tasks[1].status, TaskStatus.COMPLETED)
        self.assertEqual(tasks[2].status, TaskStatus.CANCELLED)
        # 已完成的接送交接仍可追溯。
        chain = self.backend.responsibility_chain(rid)
        completed = [s for s in chain.steps if s.status_label == "已完成"]
        self.assertEqual(len(completed), 2)
        self.assertEqual(len([h for s in chain.steps if s.handover for h in [s.handover]]), 2)
        actions = [event.action for event in chain.events]
        self.assertIn("现场交接", actions)
        self.assertIn("环节撤销", actions)


if __name__ == "__main__":
    unittest.main()
