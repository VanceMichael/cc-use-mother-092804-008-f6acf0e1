"""行李服务：件数限制、跨站路线守卫、件数守恒交接。"""

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
    luggage_request,
    run_handovers,
)


class LuggageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = RailwayServiceBackend(build_network())
        for staff in build_staff():
            self.backend.register_staff(staff)

    def test_piece_limit_enforced(self) -> None:
        with self.assertRaises(PolicyViolation):
            self.backend.submit(
                luggage_request("REQ-LUG-MAX", item_ids=("L1", "L2", "L3", "L4", "L5", "L6")),
                dt(4, 9),
            )

    def test_route_must_follow_train_stops(self) -> None:
        # 反方向（温州南->南京南）路线非法。
        with self.assertRaises(PolicyViolation):
            self.backend.submit(luggage_request("REQ-LUG-R1", route=(WZN, NKN)), dt(4, 9))

    def test_train_not_serving_endpoint_rejected(self) -> None:
        from tests.railway_factory import SZX

        # G3 不经停苏州北。
        with self.assertRaises(PolicyViolation):
            self.backend.submit(
                luggage_request("REQ-LUG-R2", train_code="G3", route=(NKN, SZX)), dt(4, 9)
            )

    def test_unknown_and_duplicate_pieces_rejected(self) -> None:
        request = luggage_request("REQ-LUG-DUP")
        from dataclasses import replace

        bad_unknown = replace(request, item_ids=("L1", "L9"))
        with self.assertRaises(Exception):
            self.backend.submit(bad_unknown, dt(4, 9))
        bad_dup = replace(request, item_ids=("L1", "L1"))
        with self.assertRaises(Exception):
            self.backend.submit(bad_dup, dt(4, 9))

    def test_full_cross_station_chain_keeps_piece_count(self) -> None:
        result = self.backend.submit(
            luggage_request("REQ-LUG-OK", item_ids=("L1", "L2"), route=(NKN, WZN)), dt(4, 9)
        )
        reservation = self.backend.get(result.reservation_id)
        tasks = reservation.plan_tasks(reservation.request.service_type)
        kinds = [t.kind for t in tasks]
        # 取件 -> 南京南>杭州东 -> 杭州东>温州南 -> 送达，四段都携带同样两件。
        self.assertEqual(
            kinds,
            [TaskKind.LUGGAGE_PICKUP, TaskKind.LUGGAGE_TRANSIT, TaskKind.LUGGAGE_TRANSIT, TaskKind.LUGGAGE_DELIVER],
        )
        self.assertTrue(all(t.item_ids == ("L1", "L2") for t in tasks))
        handovers = run_handovers(self.backend, reservation)
        self.assertTrue(all(h.item_count == 2 for h in handovers))
        self.assertEqual(self.backend.get(result.reservation_id).status, ServiceStatus.COMPLETED)

    def test_wrong_piece_count_at_delivery_aborts(self) -> None:
        result = self.backend.submit(luggage_request("REQ-LUG-CNT", item_ids=("L1", "L2")), dt(4, 9))
        rid = result.reservation_id
        reservation = self.backend.get(rid)
        tasks = reservation.plan_tasks(reservation.request.service_type)
        # 取件正常交接 2 件。
        self.backend.assign_task(tasks[0].task_id, "S-LUG-NKN", tasks[0].window_start)
        self.backend.complete_handover(tasks[0].task_id, "李先生", tasks[0].window_start)
        # 运输段均由列车长完成。
        self.backend.assign_task(tasks[1].task_id, "S-COND", tasks[1].window_start)
        self.backend.complete_handover(tasks[1].task_id, "卫行李", tasks[1].window_start)
        self.backend.assign_task(tasks[2].task_id, "S-COND", tasks[2].window_start)
        self.backend.complete_handover(tasks[2].task_id, "沈行李", tasks[2].window_start)
        # 送达时只交出 1 件：件数不符，中止，任务保持已派工。
        self.backend.assign_task(tasks[3].task_id, "S-LUG-WZN", tasks[3].window_start)
        with self.assertRaises(PolicyViolation):
            self.backend.complete_handover(tasks[3].task_id, "李先生", tasks[3].window_start, item_count=1)
        self.assertEqual(tasks[3].status, TaskStatus.ASSIGNED)

    def test_cancel_before_pickup_only_cancels_open_tasks(self) -> None:
        result = self.backend.submit(luggage_request("REQ-LUG-CXL"), dt(4, 9))
        rid = result.reservation_id
        cancelled = self.backend.cancel_reservation(rid, "李先生", dt(4, 12), "行程取消")
        reservation = self.backend.get(rid)
        self.assertEqual(len(cancelled), 4)  # 取件+两段运输+送达全部尚未发生
        self.assertEqual(reservation.status, ServiceStatus.CANCELLED)
        self.assertTrue(all(t.status == TaskStatus.CANCELLED for t in reservation.tasks.values()))

    def test_cancel_mid_route_preserves_finished_handover_trace(self) -> None:
        result = self.backend.submit(
            luggage_request("REQ-LUG-MID", item_ids=("L1", "L2"), route=(NKN, WZN)), dt(4, 9)
        )
        rid = result.reservation_id
        reservation = self.backend.get(rid)
        tasks = reservation.plan_tasks(reservation.request.service_type)
        # 完成取件和第一段运输。
        self.backend.assign_task(tasks[0].task_id, "S-LUG-NKN", tasks[0].window_start)
        self.backend.complete_handover(tasks[0].task_id, "褚行李", tasks[0].window_start)
        self.backend.assign_task(tasks[1].task_id, "S-COND", tasks[1].window_start)
        self.backend.complete_handover(tasks[1].task_id, "卫行李", tasks[1].window_start)
        # 旅客取消：后两段尚未发生可撤销；但两件行李已在杭州东交接，必须拦截整单取消。
        with self.assertRaises(ConflictError):
            self.backend.cancel_reservation(rid, "李先生", tasks[2].window_start, "不要了")


if __name__ == "__main__":
    unittest.main()
