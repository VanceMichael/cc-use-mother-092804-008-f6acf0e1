"""测试用请求工厂与组装助手。数据全部虚构。"""

from __future__ import annotations

from datetime import datetime

from src.catalog import seed_catalog
from src.repository import BookingRepository
from src.service import RailwayService

DAY = "2026-10-08"
NOW = datetime.fromisoformat(f"{DAY}T06:00:00")


def make_service() -> RailwayService:
    return RailwayService(seed_catalog(), BookingRepository())


def booking_raw(key: str = "K-0001", **overrides) -> dict:
    """一份四服务同单、窗口宽松的合法请求。"""
    raw = {
        "idempotency_key": key,
        "train_code": "G101",
        "board_station": "VNP",
        "alight_station": "HGH",
        "passengers": [
            {"passenger_id": "P1", "name": "林晚", "is_primary": True},
            {"passenger_id": "P2", "name": "同行成人", "relation": "同行成人"},
        ],
        "needs": [
            {
                "service_type": "key_passenger",
                "passenger_id": "P1",
                "category": "elderly",
                "equipment": "轮椅",
                "boarding_station": "VNP",
                "alight_station": "HGH",
                "transfer_stations": ["JGK"],
                "window_start": f"{DAY}T06:00",
                "window_end": f"{DAY}T17:00",
            },
            {
                "service_type": "pet",
                "passenger_id": "P1",
                "species": "cat",
                "pet_name": "团子",
                "crate_length_cm": 60,
                "crate_width_cm": 45,
                "crate_height_cm": 50,
                "weight_kg": 12.0,
                "window_start": f"{DAY}T06:00",
                "window_end": f"{DAY}T17:00",
            },
            {
                "service_type": "luggage",
                "passenger_id": "P1",
                "items": [
                    {"item_id": "L1", "kind": "normal", "weight_kg": 18.0},
                    {"item_id": "L2", "kind": "fragile", "weight_kg": 8.0},
                ],
                "window_start": f"{DAY}T06:00",
                "window_end": f"{DAY}T17:00",
            },
            {
                "service_type": "temp_id",
                "passenger_id": "P2",
                "reason": "证件遗忘",
                "issue_station": "VNP",
                "validity_hours": 24,
                "window_start": f"{DAY}T06:30",
                "window_end": f"{DAY}T07:30",
            },
        ],
    }
    raw.update(overrides)
    return raw


def need(raw: dict, service_type: str) -> dict:
    for item in raw["needs"]:
        if item["service_type"] == service_type:
            return item
    raise KeyError(service_type)


def tasks_of(booking, service_type: str):
    return [t for t in booking.tasks if t.service_type.value == service_type]
