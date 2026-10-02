"""综合场景：多服务并存、终结后再约、时间窗派工、确认后请求归一。"""

import unittest

from src.railway import (
    ConflictError,
    RailwayServiceBackend,
    ServiceType,
    ServiceStatus,
    SubmitOutcome,
)
from tests.railway_factory import (
    HZH,
    NKN,
    WZN,
    build_network,
    build_staff,
    credential_request,
    dt,
    key_passenger_request,
    luggage_request,
    pet_request,
    run_handovers,
)


class CombinedServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = RailwayServiceBackend(build_network())
        for staff in build_staff():
            self.backend.register_staff(staff)

    def test_same_passenger_four_services_are_separate_reservations(self) -> None:
        """同一旅客假期同时需要四类服务：按服务锚点分别受理，互不重复、互不混淆。"""
        kp = self.backend.submit(key_passenger_request("REQ-MIX-KP", verified=True), dt(4, 9))
        pet = self.backend.submit(pet_request("REQ-MIX-PET", route=(NKN, HZH)), dt(4, 9, 1))
        luggage = self.backend.submit(
            luggage_request("REQ-MIX-LUG", item_ids=("L1", "L2"), route=(NKN, WZN)), dt(4, 9, 2)
        )
        credential = self.backend.submit(credential_request("REQ-MIX-CRED"), dt(4, 9, 3))
        ids = [kp.reservation_id, pet.reservation_id, luggage.reservation_id, credential.reservation_id]
        self.assertEqual(len(set(ids)), 4)
        for result, service_type in zip(
            (kp, pet, luggage, credential),
            (ServiceType.KEY_PASSENGER, ServiceType.PET, ServiceType.LUGGAGE, ServiceType.TEMP_CREDENTIAL),
        ):
            self.assertEqual(
                self.backend.get(result.reservation_id).request.service_type, service_type
            )
        # 任一类服务重复提交仍回到各自的同一预约。
        again = self.backend.submit(luggage_request("REQ-MIX-LUG-2", item_ids=("L1", "L2"), route=(NKN, WZN)), dt(4, 9, 4))
        self.assertEqual(again.outcome, SubmitOutcome.DUPLICATE)
        self.assertEqual(again.reservation_id, luggage.reservation_id)

    def test_completed_reservation_allows_rebooking_with_same_anchor(self) -> None:
        first = self.backend.submit(pet_request("REQ-REBOOK-1", route=(NKN, HZH)), dt(4, 9))
        run_handovers(self.backend, self.backend.get(first.reservation_id), counterparty="李先生")
        self.assertEqual(self.backend.get(first.reservation_id).status, ServiceStatus.COMPLETED)
        second = self.backend.submit(pet_request("REQ-REBOOK-2", route=(NKN, HZH)), dt(6, 9))
        self.assertEqual(second.outcome, SubmitOutcome.CREATED)
        self.assertNotEqual(second.reservation_id, first.reservation_id)

    def test_assign_after_window_closed_rejected(self) -> None:
        result = self.backend.submit(pet_request("REQ-WIN-1", route=(NKN, HZH)), dt(4, 9))
        reservation = self.backend.get(result.reservation_id)
        consign = reservation.plan_tasks(ServiceType.PET)[0]
        # 发运窗 7:00-7:50；7:51 派工被拒。
        with self.assertRaises(ConflictError):
            self.backend.assign_task(consign.task_id, "S-PET-NKN", dt(5, 7, 51))

    def test_approved_change_maps_new_request_to_same_reservation(self) -> None:
        first = self.backend.submit(pet_request("REQ-MAP-1", train_code="G1"), dt(4, 9))
        self.backend.submit(pet_request("REQ-MAP-2", train_code="G2"), dt(4, 9, 20))
        self.backend.review_change(first.reservation_id, True, "主管乙", dt(4, 10))
        # 变更确认后，用户用新请求号再点一次，不再产生第三张预约。
        again = self.backend.submit(pet_request("REQ-MAP-2", train_code="G2"), dt(4, 10, 1))
        self.assertEqual(again.outcome, SubmitOutcome.DUPLICATE)
        self.assertEqual(again.reservation_id, first.reservation_id)

    def test_key_passenger_duplicate_before_and_after_verification(self) -> None:
        first = self.backend.submit(key_passenger_request("REQ-KP-DUP-1"), dt(4, 9))
        retry = self.backend.submit(key_passenger_request("REQ-KP-DUP-2"), dt(4, 9, 5))
        self.assertEqual(retry.outcome, SubmitOutcome.DUPLICATE)
        self.backend.verify_eligibility(first.reservation_id, "值班员", dt(4, 10))
        # 核验后同一资料再提交仍是同一预约，且接力任务只生成一次。
        again = self.backend.submit(key_passenger_request("REQ-KP-DUP-3", verified=True), dt(4, 10, 5))
        self.assertEqual(again.outcome, SubmitOutcome.DUPLICATE)
        reservation = self.backend.get(first.reservation_id)
        self.assertEqual(len(reservation.plan_tasks(ServiceType.KEY_PASSENGER)), 4)


if __name__ == "__main__":
    unittest.main()
