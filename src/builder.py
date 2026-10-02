"""把外部预约请求（纯数据）构造成领域对象，并生成幂等指纹。

校验分两层：这里做结构性校验（必填、类型、时间可解析），业务规则
（物种、件数、有效期、路线）交给 ``planning``。指纹按"关键字段"
分段计算，重复请求时可指出到底是哪一段发生了变化。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Optional

from .models import (
    Itinerary,
    KeyPassengerNeed,
    LuggageItem,
    LuggageNeed,
    Passenger,
    PetNeed,
    ServiceType,
    TempIdNeed,
)


class RequestError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _require(mapping: dict, key: str, label: str, expected: type | tuple[type, ...]) -> Any:
    if key not in mapping or mapping[key] is None:
        raise RequestError("missing_field", f"缺少必填项：{label}")
    value = mapping[key]
    bool_allowed = expected is bool or (isinstance(expected, tuple) and bool in expected)
    if isinstance(value, bool) and not bool_allowed:
        raise RequestError("bad_field", f"{label}格式不正确")
    if not isinstance(value, expected):
        raise RequestError("bad_field", f"{label}格式不正确")
    return value


def _parse_dt(value: str, label: str) -> datetime:
    if not isinstance(value, str):
        raise RequestError("bad_field", f"{label}必须是 ISO 8601 时间")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise RequestError("bad_field", f"{label}时间格式不正确") from exc


# --- 指纹：识别重复请求与关键字段变化 ------------------------------------


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def key_sections(raw: dict) -> dict[str, Any]:
    """提取参与幂等判断的关键段。窗口等偏好字段不纳入关键字段。"""
    sections: dict[str, Any] = {}
    sections["passengers"] = [
        {
            "passenger_id": p.get("passenger_id"),
            "name": p.get("name"),
            "is_primary": p.get("is_primary", False),
            "relation": p.get("relation"),
        }
        for p in raw.get("passengers", [])
    ]
    sections["itinerary"] = {
        "train_code": raw.get("train_code"),
        "board_station": raw.get("board_station"),
        "alight_station": raw.get("alight_station"),
    }
    needs: dict[str, Any] = {}
    for need in raw.get("needs", []):
        service = need.get("service_type", "")
        if service == ServiceType.KEY_PASSENGER.value:
            needs[service] = {
                "passenger_id": need.get("passenger_id"),
                "category": need.get("category"),
                "equipment": need.get("equipment"),
                "boarding_station": need.get("boarding_station"),
                "alight_station": need.get("alight_station"),
                "transfer_stations": need.get("transfer_stations", []),
            }
        elif service == ServiceType.PET.value:
            needs[service] = {
                "passenger_id": need.get("passenger_id"),
                "species": need.get("species"),
                "pet_name": need.get("pet_name"),
                "crate": [
                    need.get("crate_length_cm"),
                    need.get("crate_width_cm"),
                    need.get("crate_height_cm"),
                ],
                "weight_kg": need.get("weight_kg"),
                "separate_itinerary": need.get("separate_itinerary"),
            }
        elif service == ServiceType.LUGGAGE.value:
            needs[service] = {
                "passenger_id": need.get("passenger_id"),
                "items": need.get("items", []),
                "origin_station": need.get("origin_station"),
                "destination_station": need.get("destination_station"),
                "separate_itinerary": need.get("separate_itinerary"),
            }
        elif service == ServiceType.TEMP_ID.value:
            needs[service] = {
                "passenger_id": need.get("passenger_id"),
                "reason": need.get("reason"),
                "issue_station": need.get("issue_station"),
                "validity_hours": need.get("validity_hours"),
            }
    sections["needs"] = needs
    return sections


def fingerprint(raw: dict) -> str:
    digest = hashlib.sha256(_canonical(key_sections(raw)).encode("utf-8"))
    return digest.hexdigest()


def changed_fields(old_raw: dict, new_raw: dict) -> list[str]:
    """对比两次请求的关键段，返回变化段的中文名列表。"""
    old_sections, new_sections = key_sections(old_raw), key_sections(new_raw)
    labels = {
        "passengers": "旅客与同行关系",
        "itinerary": "车次或乘降站",
        "needs": "服务需求内容",
    }
    changed: list[str] = []
    for key, label in labels.items():
        if _canonical(old_sections.get(key)) != _canonical(new_sections.get(key)):
            changed.append(label)
    return changed


# --- 构造领域对象 --------------------------------------------------------


def build_passengers(raw: dict) -> tuple[Passenger, ...]:
    rows = _require(raw, "passengers", "旅客名单", list)
    if not rows:
        raise RequestError("missing_field", "至少需要一名旅客")
    passengers: list[Passenger] = []
    primary_count = 0
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RequestError("bad_field", "旅客信息格式不正确")
        pid = _require(row, "passenger_id", "旅客标识", str)
        if pid in seen:
            raise RequestError("duplicate_passenger", f"旅客标识重复：{pid}")
        seen.add(pid)
        name = _require(row, "name", "旅客姓名", str)
        is_primary = bool(row.get("is_primary", False))
        primary_count += is_primary
        relation = row.get("relation")
        passengers.append(Passenger(pid, name, is_primary, relation))
    if primary_count != 1:
        raise RequestError("primary_required", "预约必须恰好指定一名主预约人")
    return tuple(passengers)


def build_itinerary(raw: dict, prefix: str = "", label: str = "行程") -> Itinerary:
    """构造裸行程（时刻在服务层按车次目录补全）。"""
    train = _require(raw, f"{prefix}train_code", f"{label}车次", str)
    board = _require(raw, f"{prefix}board_station", f"{label}乘车站", str)
    alight = _require(raw, f"{prefix}alight_station", f"{label}到达站", str)
    if board == alight:
        raise RequestError("bad_route", "乘车站与到达站不能相同")
    return Itinerary(train_code=train, board_station=board, alight_station=alight)


def _window(need_raw: dict) -> tuple[datetime, datetime]:
    start = _parse_dt(_require(need_raw, "window_start", "服务时间窗开始", str), "服务时间窗开始")
    end = _parse_dt(_require(need_raw, "window_end", "服务时间窗结束", str), "服务时间窗结束")
    if start >= end:
        raise RequestError("bad_window", "服务时间窗开始必须早于结束")
    return start, end


def build_needs(raw: dict, passenger_ids: set[str]) -> list:
    rows = raw.get("needs", [])
    if not isinstance(rows, list) or not rows:
        raise RequestError("missing_field", "至少选择一项服务")
    needs: list = []
    seen_types: set[ServiceType] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RequestError("bad_field", "服务需求格式不正确")
        try:
            service_type = ServiceType(_require(row, "service_type", "服务类型", str))
        except ValueError as exc:
            raise RequestError("unknown_service", f"未知服务类型：{row.get('service_type')}") from exc
        if service_type in seen_types:
            raise RequestError("duplicate_service", f"同一预约内 {service_type.value} 服务重复")
        seen_types.add(service_type)
        passenger_id = _require(row, "passenger_id", "服务归属旅客", str)
        if passenger_id not in passenger_ids:
            raise RequestError("unknown_passenger", f"服务归属旅客不在同行名单内：{passenger_id}")
        start, end = _window(row)

        if service_type is ServiceType.KEY_PASSENGER:
            needs.append(
                KeyPassengerNeed(
                    service_type=service_type,
                    passenger_id=passenger_id,
                    window_start=start,
                    window_end=end,
                    category=_require(row, "category", "重点旅客类别", str),
                    equipment=row.get("equipment", ""),
                    boarding_station=row.get("boarding_station", ""),
                    alight_station=row.get("alight_station", ""),
                    transfer_stations=tuple(row.get("transfer_stations", [])),
                )
            )
        elif service_type is ServiceType.PET:
            own: Optional[Itinerary] = None
            if row.get("separate_itinerary"):
                own = build_itinerary(row["separate_itinerary"], label="宠物单独车次")
            needs.append(
                PetNeed(
                    service_type=service_type,
                    passenger_id=passenger_id,
                    window_start=start,
                    window_end=end,
                    species=_require(row, "species", "宠物物种", str),
                    pet_name=row.get("pet_name", ""),
                    crate_length_cm=int(_require(row, "crate_length_cm", "笼具长度", int)),
                    crate_width_cm=int(_require(row, "crate_width_cm", "笼具宽度", int)),
                    crate_height_cm=int(_require(row, "crate_height_cm", "笼具高度", int)),
                    weight_kg=float(_require(row, "weight_kg", "含笼重量", (int, float))),
                    consignor_name=row.get("consignor_name", ""),
                    itinerary=own,
                )
            )
        elif service_type is ServiceType.LUGGAGE:
            items_raw = _require(row, "items", "行李清单", list)
            items = tuple(
                LuggageItem(
                    item_id=_require(item, "item_id", "行李件标识", str),
                    kind=item.get("kind", "normal"),
                    weight_kg=float(_require(item, "weight_kg", "行李重量", (int, float))),
                    declared_value=int(item.get("declared_value", 0)),
                )
                for item in items_raw
            )
            own = None
            if row.get("separate_itinerary"):
                own = build_itinerary(row["separate_itinerary"], label="行李专用车次")
            needs.append(
                LuggageNeed(
                    service_type=service_type,
                    passenger_id=passenger_id,
                    window_start=start,
                    window_end=end,
                    items=items,
                    origin_station=row.get("origin_station", ""),
                    destination_station=row.get("destination_station", ""),
                    itinerary=own,
                    pickup_address=row.get("pickup_address", ""),
                    delivery_address=row.get("delivery_address", ""),
                )
            )
        else:
            needs.append(
                TempIdNeed(
                    service_type=service_type,
                    passenger_id=passenger_id,
                    window_start=start,
                    window_end=end,
                    reason=_require(row, "reason", "办证事由", str),
                    issue_station=_require(row, "issue_station", "制证站", str),
                    validity_hours=float(row.get("validity_hours", 24.0)),
                )
            )
    return needs
