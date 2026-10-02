"""视图：值班人员只看到负责的最小信息；管理者可还原完整责任链。"""

import unittest

from src.railway import RailwayServiceBackend, ServiceType
from tests.railway_factory import (
    HZH,
    NKN,
    WZN,
    build_network,
    build_staff,
    dt,
    key_passenger_request,
    luggage_request,
    pet_request,
)


class StaffViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = RailwayServiceBackend(build_network())
        for staff in build_staff():
            self.backend.register_staff(staff)

    def test_staff_sees_only_own_station_and_role(self) -> None:
        self.backend.submit(pet_request("REQ-V-PET", route=(NKN, HZH)), dt(4, 9))
        views = self.backend.staff_tasks("S-PET-NKN")
        self.assertEqual({v.station_name for v in views}, {"南京南"})
        self.assertTrue(all(v.kind_label in ("宠物发运",) for v in views))
        # 杭州东宠物托运员看到交付环节；温州南接送员什么宠物任务都看不到。
        self.assertEqual(
            {v.kind_label for v in self.backend.staff_tasks("S-PET-HZH")}, {"宠物交付"}
        )
        self.assertEqual(self.backend.staff_tasks("S-ESC-WZN"), [])

    def test_minimal_view_hides_other_passengers_and_history(self) -> None:
        self.backend.submit(
            key_passenger_request("REQ-V-KP", verified=True, route=(NKN, WZN)), dt(4, 9)
        )
        views = self.backend.staff_tasks("S-COND")
        self.assertTrue(views)
        view = views[0]
        # 最小信息：站点、时间窗、动作、件数；没有资格依据、同行人等字段。
        self.assertEqual(view.service_label, "重点旅客陪护")
        self.assertIn("车上照护", view.instruction)
        self.assertFalse(hasattr(view, "evidence"))
        self.assertFalse(hasattr(view, "relations"))
        self.assertEqual(view.piece_count, 1)

    def test_assigned_task_is_only_visible_to_assignee_when_staffed(self) -> None:
        result = self.backend.submit(
            luggage_request("REQ-V-LUG", route=(NKN, HZH)), dt(4, 9)
        )
        reservation = self.backend.get(result.reservation_id)
        pickup = reservation.plan_tasks(ServiceType.LUGGAGE)[0]
        self.backend.assign_task(pickup.task_id, "S-LUG-NKN", pickup.window_start)
        # 派工后该取件环节只属于褚行李；其他站点行李员看不到。
        nkn_views = self.backend.staff_tasks("S-LUG-NKN")
        hzh_views = self.backend.staff_tasks("S-LUG-HZH")
        self.assertIn(pickup.task_id, {v.task_id for v in nkn_views})
        self.assertNotIn(pickup.task_id, {v.task_id for v in hzh_views})

    def test_train_conductor_sees_all_open_segments(self) -> None:
        self.backend.submit(
            luggage_request("REQ-V-SEG", route=(NKN, WZN)), dt(4, 9)
        )
        views = self.backend.staff_tasks("S-COND")
        self.assertEqual(len([v for v in views if v.kind_label == "行李运输"]), 2)


class ResponsibilityChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = RailwayServiceBackend(build_network())
        for staff in build_staff():
            self.backend.register_staff(staff)

    def test_chain_reconstructs_submit_to_completion(self) -> None:
        result = self.backend.submit(pet_request("REQ-CHAIN-1", route=(NKN, HZH)), dt(4, 9))
        rid = result.reservation_id
        reservation = self.backend.get(rid)
        for task in reservation.plan_tasks(ServiceType.PET):
            staff_id = {
                "pet_clerk": "S-PET-NKN" if task.station_code == NKN else "S-PET-HZH",
                "train_conductor": "S-COND",
            }[task.role]
            self.backend.assign_task(task.task_id, staff_id, task.window_start)
            self.backend.complete_handover(task.task_id, "李先生", task.window_start)
        chain = self.backend.responsibility_chain(rid)
        self.assertEqual(chain.service_label, "宠物托运")
        self.assertEqual(chain.status_label, "已完成")
        actions = [event.action for event in chain.events]
        self.assertEqual(actions[0], "预约受理")
        self.assertIn("派工", actions)
        self.assertEqual(actions.count("现场交接"), 3)
        # 每个环节都能定位责任人与交接双方。
        for step in chain.steps:
            self.assertIsNotNone(step.assignee_name)
            self.assertIsNotNone(step.handover)
            self.assertEqual(step.handover.handed_to, "李先生")

    def test_chain_after_cancel_keeps_finished_steps_and_shows_cancels(self) -> None:
        result = self.backend.submit(
            key_passenger_request("REQ-CHAIN-2", verified=True, route=(NKN, WZN)), dt(4, 9)
        )
        rid = result.reservation_id
        reservation = self.backend.get(rid)
        tasks = reservation.plan_tasks(ServiceType.KEY_PASSENGER)
        self.backend.assign_task(tasks[0].task_id, "S-ESC-NKN", tasks[0].window_start)
        self.backend.complete_handover(tasks[0].task_id, "张大爷", tasks[0].window_start)
        self.backend.cancel_reservation(rid, "张女", tasks[1].window_start, "终止行程")
        chain = self.backend.responsibility_chain(rid)
        statuses = [(s.kind_label, s.status_label) for s in chain.steps]
        self.assertIn(("进站接送", "已完成"), statuses)
        self.assertTrue(any(label == "已撤销" for _, label in statuses))
        # 事件序列严格递增，可按时间还原责任。
        seq = [event.seq for event in chain.events]
        self.assertEqual(seq, list(range(1, len(seq) + 1)))


if __name__ == "__main__":
    unittest.main()
