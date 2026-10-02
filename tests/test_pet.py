"""宠物托运：物种与尺寸限制、单独出行接收人、禁运站点、交接件数。"""

import unittest

from src.railway import (
    Cage,
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
    cat,
    cat_unaccompanied,
    dog,
    dt,
    oversized_cat,
    pet_receiver,
    pet_request,
    run_handovers,
    snake,
)


class PetPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = RailwayServiceBackend(build_network())
        for staff in build_staff():
            self.backend.register_staff(staff)

    def test_forbidden_species_rejected(self) -> None:
        with self.assertRaises(PolicyViolation):
            self.backend.submit(pet_request("REQ-PET-F1", pet=snake()), dt(4, 9))

    def test_oversized_cage_rejected(self) -> None:
        with self.assertRaises(PolicyViolation):
            self.backend.submit(pet_request("REQ-PET-F2", pet=oversized_cat()), dt(4, 9))

    def test_weight_limit_rejected(self) -> None:
        heavy_dog = dog()
        from dataclasses import replace

        heavy = replace(heavy_dog, weight_kg=25.0)
        with self.assertRaises(PolicyViolation):
            self.backend.submit(pet_request("REQ-PET-F3", pet=heavy), dt(4, 9))

    def test_station_without_pet_service_rejected(self) -> None:
        with self.assertRaises(PolicyViolation):
            self.backend.submit(pet_request("REQ-PET-F4", route=(NKN, WZN)), dt(4, 9))

    def test_unaccompanied_pet_requires_receiver(self) -> None:
        pet = cat_unaccompanied()
        from dataclasses import replace

        no_receiver = replace(pet, unaccompanied=True, receiver_id=None)
        with self.assertRaises(PolicyViolation):
            self.backend.submit(pet_request("REQ-PET-F5", pet=no_receiver), dt(4, 9))

    def test_unaccompanied_pet_full_chain_names_receiver(self) -> None:
        result = self.backend.submit(pet_request("REQ-PET-OK", pet=cat_unaccompanied()), dt(4, 9))
        reservation = self.backend.get(result.reservation_id)
        tasks = reservation.plan_tasks(reservation.request.service_type)
        self.assertEqual([t.kind for t in tasks], [TaskKind.PET_CONSIGN, TaskKind.PET_TRANSIT, TaskKind.PET_CLAIM])
        self.assertIn("王女士", tasks[0].detail)
        self.assertIn("王女士", tasks[1].detail)
        handovers = run_handovers(self.backend, reservation, counterparty="王女士")
        self.assertEqual(len(handovers), 3)
        self.assertTrue(all(h.item_count == 1 for h in handovers))
        self.assertEqual(self.backend.get(result.reservation_id).status, ServiceStatus.COMPLETED)

    def test_handover_count_mismatch_blocks_claim(self) -> None:
        result = self.backend.submit(pet_request("REQ-PET-CNT", pet=cat()), dt(4, 9))
        reservation = self.backend.get(result.reservation_id)
        tasks = reservation.plan_tasks(reservation.request.service_type)
        self.backend.assign_task(tasks[0].task_id, "S-PET-NKN", tasks[0].window_start)
        with self.assertRaises(PolicyViolation):
            self.backend.complete_handover(tasks[0].task_id, "李先生", tasks[0].window_start, item_count=2)
        self.assertEqual(tasks[0].status, TaskStatus.ASSIGNED)

    def test_consignment_cannot_be_cancelled_after_handover(self) -> None:
        result = self.backend.submit(pet_request("REQ-PET-CXL", pet=cat()), dt(4, 9))
        rid = result.reservation_id
        reservation = self.backend.get(rid)
        tasks = reservation.plan_tasks(reservation.request.service_type)
        self.backend.assign_task(tasks[0].task_id, "S-PET-NKN", tasks[0].window_start)
        self.backend.complete_handover(tasks[0].task_id, "李先生", tasks[0].window_start)
        # 发运已交接，宠物在途，不能撤销整个宠物服务。
        from src.railway import ConflictError

        with self.assertRaises(ConflictError):
            self.backend.cancel_reservation(rid, "李先生", tasks[1].window_start, "不想托运了")

    def test_multiple_pets_each_get_own_reservation(self) -> None:
        """两只猫同时托运：锚点按 pet_id 区分，不能合并受理。"""
        first = self.backend.submit(pet_request("REQ-PET-M1", pet=cat()), dt(4, 9))
        other_cat = cat()
        from dataclasses import replace

        second_pet = replace(other_cat, pet_id="PET-CAT-2")
        second = self.backend.submit(pet_request("REQ-PET-M2", pet=second_pet), dt(4, 9, 5))
        self.assertNotEqual(first.reservation_id, second.reservation_id)


if __name__ == "__main__":
    unittest.main()
