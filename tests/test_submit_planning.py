"""预约受理、四服务同单与规划规则校验。"""

import unittest
from datetime import datetime

from src.planning import PlanningError
from src.models import EligibilityStatus, ServiceType, TaskStatus

from tests.factories import DAY, NOW, booking_raw, make_service, need, tasks_of


class SubmitPlanningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()

    def submit(self, raw=None):
        return self.svc.submit_booking(raw or booking_raw(), NOW).booking

    def assert_code(self, code: str) -> "object":
        return _AssertCode(self, PlanningError, code)

    def test_four_services_in_one_booking_share_one_id(self) -> None:
        booking = self.submit()
        self.assertEqual({n.service_type for n in booking.needs}, set(ServiceType))
        # 宠物/行李/临时证直接生成任务，重点旅客等待资格核验
        self.assertEqual(len(tasks_of(booking, "pet")), 2)
        self.assertEqual(len(tasks_of(booking, "luggage")), 2)
        self.assertEqual(len(tasks_of(booking, "temp_id")), 2)
        self.assertEqual(tasks_of(booking, "key_passenger"), [])
        key_need = booking.need_for(ServiceType.KEY_PASSENGER)
        self.assertIs(key_need.eligibility, EligibilityStatus.PENDING)

    def test_key_passenger_tasks_generated_only_after_eligibility(self) -> None:
        booking = self.submit()
        self.svc.verify_eligibility(booking.booking_id, ServiceType.KEY_PASSENGER, True, "M-VNP", NOW)
        tasks = tasks_of(booking, "key_passenger")
        kinds = [(t.kind.value, t.station_code) for t in tasks]
        self.assertEqual(kinds, [("pickup", "VNP"), ("transfer", "JGK"), ("delivery", "HGH")])
        self.assertTrue(all(t.assignee_id for t in tasks))

    def test_eligibility_rejection_creates_no_segments(self) -> None:
        booking = self.submit()
        self.svc.verify_eligibility(
            booking.booking_id, ServiceType.KEY_PASSENGER, False, "M-VNP", NOW, "类别不符"
        )
        self.assertEqual(tasks_of(booking, "key_passenger"), [])
        self.assertIs(booking.need_for(ServiceType.KEY_PASSENGER).eligibility, EligibilityStatus.REJECTED)

    # --- 宠物规则 ---------------------------------------------------------

    def test_pet_forbidden_species_rejected(self) -> None:
        raw = booking_raw()
        need(raw, "pet")["species"] = "snake"
        with self.assert_code("species_forbidden"):
            self.submit(raw)

    def test_pet_oversized_crate_rejected(self) -> None:
        raw = booking_raw()
        need(raw, "pet")["crate_length_cm"] = 200
        with self.assert_code("crate_exceeded"):
            self.submit(raw)

    def test_pet_overweight_rejected(self) -> None:
        raw = booking_raw()
        need(raw, "pet")["weight_kg"] = 60
        with self.assert_code("weight_exceeded"):
            self.submit(raw)

    def test_pet_origin_must_be_designated_station(self) -> None:
        # G307 经济南西始发，但济南西不在宠物指定车站范围
        raw = booking_raw()
        need(raw, "pet")["separate_itinerary"] = {
            "train_code": "G307", "board_station": "JGK", "alight_station": "HGH"
        }
        with self.assert_code("station_not_served"):
            self.submit(raw)

    def test_pet_designated_route_accepted(self) -> None:
        raw = booking_raw()
        need(raw, "pet")["separate_itinerary"] = {
            "train_code": "G205", "board_station": "VNP", "alight_station": "NPH"
        }
        booking = self.submit(raw)
        pet_tasks = tasks_of(booking, "pet")
        self.assertEqual([t.station_code for t in pet_tasks], ["VNP", "NPH"])

    def test_pet_travelling_alone_uses_own_train(self) -> None:
        raw = booking_raw()
        need(raw, "pet")["separate_itinerary"] = {
            "train_code": "G205", "board_station": "VNP", "alight_station": "NPH"
        }
        booking = self.submit(raw)
        trains = {t.train_code for t in tasks_of(booking, "pet")}
        self.assertEqual(trains, {"G205"})
        # 主预约人仍乘 G101
        self.assertEqual(booking.itinerary.train_code, "G101")

    # --- 行李规则 ---------------------------------------------------------

    def test_luggage_piece_count_cap_enforced(self) -> None:
        raw = booking_raw()
        need(raw, "luggage")["items"] = [
            {"item_id": f"L{i}", "weight_kg": 5} for i in range(6)
        ]
        with self.assert_code("piece_count_exceeded"):
            self.submit(raw)

    def test_luggage_weight_cap_enforced(self) -> None:
        raw = booking_raw()
        need(raw, "luggage")["items"] = [{"item_id": "L1", "weight_kg": 80}]
        with self.assert_code("weight_exceeded"):
            self.submit(raw)

    def test_luggage_route_must_be_between_two_distinct_stations(self) -> None:
        raw = booking_raw()
        need(raw, "luggage")["origin_station"] = "NPH"
        need(raw, "luggage")["destination_station"] = "NPH"
        with self.assert_code("route_forbidden"):
            self.submit(raw)

    def test_luggage_cross_station_on_own_train(self) -> None:
        raw = booking_raw()
        leg = need(raw, "luggage")
        leg["origin_station"] = "JGK"
        leg["destination_station"] = "NPH"
        leg["separate_itinerary"] = {"train_code": "G307", "board_station": "JGK", "alight_station": "NPH"}
        booking = self.submit(raw)
        luggage_tasks = tasks_of(booking, "luggage")
        self.assertEqual([(t.kind.value, t.station_code) for t in luggage_tasks],
                         [("luggage_pickup", "JGK"), ("luggage_delivery", "NPH")])
        self.assertTrue(all(t.train_code == "G307" for t in luggage_tasks))

    def test_luggage_route_must_follow_train_direction(self) -> None:
        raw = booking_raw()
        leg = need(raw, "luggage")
        leg["origin_station"] = "HGH"
        leg["destination_station"] = "NPH"
        leg["separate_itinerary"] = {"train_code": "G101", "board_station": "VNP", "alight_station": "HGH"}
        with self.assert_code("bad_route"):
            self.submit(raw)

    # --- 临时证明 ---------------------------------------------------------

    def test_temp_id_validity_cap_enforced(self) -> None:
        raw = booking_raw()
        need(raw, "temp_id")["validity_hours"] = 48
        with self.assert_code("validity_exceeded"):
            self.submit(raw)

    def test_window_outside_train_schedule_rejected(self) -> None:
        raw = booking_raw()
        need(raw, "pet")["window_start"] = f"{DAY}T09:00"  # 受理 07:12 已开始
        with self.assert_code("outside_window"):
            self.submit(raw)

    def test_bad_train_and_unknown_station_rejected(self) -> None:
        with self.assertRaises(PlanningError) as ctx:
            self.submit(booking_raw(train_code="G999"))
        self.assertEqual(ctx.exception.code, "unknown_train")
        with self.assertRaises(PlanningError) as ctx:
            self.submit(booking_raw(board_station="XXX"))
        self.assertEqual(ctx.exception.code, "unknown_station")


class _AssertCode:
    def __init__(self, test_case: unittest.TestCase, exc_cls: type, code: str):
        self.test_case = test_case
        self.exc_cls = exc_cls
        self.code = code

    def __enter__(self):
        self.ctx = self.test_case.assertRaises(self.exc_cls)
        return self.ctx.__enter__()

    def __exit__(self, *args):
        result = self.ctx.__exit__(*args)
        self.test_case.assertEqual(self.ctx.exception.code, self.code)
        return result


if __name__ == "__main__":
    unittest.main()
