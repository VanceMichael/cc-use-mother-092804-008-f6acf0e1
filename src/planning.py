"""把四类服务需求规划为分段接力任务，并执行静态业务规则校验。

规划器是纯函数：输入目录与需求，输出有序的 ``PlannedStep``；
任务编号、指派、持久化由应用服务层负责。资格核验（重点旅客）也是
服务层的闸门——未通过核验不会调用这里的规划函数。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .catalog import ServiceCatalog
from .models import (
    Itinerary,
    KeyPassengerNeed,
    LuggageNeed,
    PetNeed,
    ServiceType,
    TaskKind,
    TempIdNeed,
)


class PlanningError(ValueError):
    """规则校验失败。``code`` 供适配层稳定识别，消息面向值班人员。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PlannedStep:
    kind: TaskKind
    station_code: str
    start: datetime
    end: datetime
    train_code: Optional[str] = None


# 各环节相对列车到发时刻的提前量/延后量（分钟）
PICKUP_LEAD_MIN = 45
PICKUP_TAIL_MIN = 5
TRANSFER_TAIL_MIN = 5
DELIVERY_SPAN_MIN = 30
PET_DROPOFF_LEAD_MIN = 60
PET_DROPOFF_TAIL_MIN = 10
PET_PICKUP_SPAN_MIN = 40
LUGGAGE_PICKUP_LEAD_MIN = 90
LUGGAGE_PICKUP_TAIL_MIN = 20
LUGGAGE_DELIVERY_LAG_MIN = 20
LUGGAGE_DELIVERY_SPAN_MIN = 100


# --- 通用校验 -----------------------------------------------------------


def _require_station(catalog: ServiceCatalog, code: str, label: str) -> None:
    if not code or catalog.station(code) is None:
        raise PlanningError("unknown_station", f"{label}不在服务站目录内：{code or '（空）'}")


def _require_window(need, step: PlannedStep) -> None:
    """环节时间窗必须落在旅客预约的时间窗内。"""
    if need.window_start >= need.window_end:
        raise PlanningError("bad_window", "服务时间窗开始必须早于结束")
    if step.start < need.window_start or step.end > need.window_end:
        raise PlanningError(
            "outside_window",
            f"{step.kind.value}环节时间 {step.start:%m-%d %H:%M}-{step.end:%H:%M}"
            f" 超出旅客预约窗口",
        )


def resolve_itinerary(
    catalog: ServiceCatalog,
    train_code: str,
    board: str,
    alight: str,
) -> Itinerary:
    """按车次目录解析行程，拒绝不停靠或乘降顺序错误。"""
    train = catalog.train(train_code)
    if train is None:
        raise PlanningError("unknown_train", f"车次 {train_code} 不在目录内")
    _require_station(catalog, board, "乘车站")
    _require_station(catalog, alight, "到达站")
    try:
        return train.as_itinerary(board, alight)
    except ValueError as exc:
        raise PlanningError("bad_route", str(exc)) from exc


def _itinerary_for(
    catalog: ServiceCatalog,
    own: Optional[Itinerary],
    fallback: Itinerary,
    origin: str,
    destination: str,
    *,
    allowed_trains: Optional[frozenset[str]] = None,
) -> Itinerary:
    """宠物/行李单独出行时使用自己的车次，否则随主预约人行程。"""
    itinerary = own or fallback
    if allowed_trains is not None and itinerary.train_code not in allowed_trains:
        raise PlanningError(
            "train_not_served",
            f"车次 {itinerary.train_code} 不承担该类托运",
        )
    rank_origin = itinerary.station_rank(origin)
    rank_dest = itinerary.station_rank(destination)
    if rank_origin is None or rank_dest is None:
        raise PlanningError(
            "route_not_covered",
            f"{itinerary.train_code} 未按路线停靠 {origin} 至 {destination}",
        )
    if rank_origin >= rank_dest:
        raise PlanningError("bad_route", "托运起终站方向与车次运行方向不符")
    return itinerary


# --- 重点旅客：分段接力 --------------------------------------------------


def plan_key_passenger(
    catalog: ServiceCatalog,
    need: KeyPassengerNeed,
    itinerary: Itinerary,
) -> list[PlannedStep]:
    """进站接送 → （换乘站分段交接）→ 出站送达。

    换乘站必须在乘降站之间、按运行方向排列。换乘点把责任链切成多段，
    取消时只回收尚未发生的段。
    """
    if not catalog.key_passenger_policy.category_known(need.category):
        raise PlanningError("unknown_category", f"重点旅客类别未备案：{need.category}")
    _require_station(catalog, need.boarding_station, "进站接送站")
    _require_station(catalog, need.alight_station, "出站送达站")
    if need.boarding_station != itinerary.board_station:
        raise PlanningError("board_mismatch", "进站接送站必须与乘车出发站一致")
    if need.alight_station != itinerary.alight_station:
        raise PlanningError("alight_mismatch", "出站送达站必须与乘车到达站一致")

    board_stop = itinerary.stop_at(need.boarding_station)
    alight_stop = itinerary.stop_at(need.alight_station)
    assert board_stop is not None and alight_stop is not None

    ranks = itinerary.station_sequence()
    last_rank = -1
    for code in need.transfer_stations:
        _require_station(catalog, code, "换乘站")
        rank = itinerary.station_rank(code)
        if rank is None or not (0 < rank < len(ranks) - 1):
            raise PlanningError("bad_transfer", f"换乘站 {code} 不在行程区间内")
        if rank <= last_rank:
            raise PlanningError("bad_transfer", "换乘站必须按车次运行方向排列")
        last_rank = rank

    steps: list[PlannedStep] = [
        PlannedStep(
            TaskKind.PICKUP,
            need.boarding_station,
            board_stop.depart_at - timedelta(minutes=PICKUP_LEAD_MIN),
            board_stop.depart_at - timedelta(minutes=PICKUP_TAIL_MIN),
            itinerary.train_code,
        )
    ]
    for code in need.transfer_stations:
        stop = itinerary.stop_at(code)
        assert stop is not None and stop.arrive_at is not None
        steps.append(
            PlannedStep(
                TaskKind.TRANSFER,
                code,
                stop.arrive_at,
                stop.depart_at + timedelta(minutes=TRANSFER_TAIL_MIN),
                itinerary.train_code,
            )
        )
    steps.append(
        PlannedStep(
            TaskKind.DELIVERY,
            need.alight_station,
            alight_stop.arrive_at,
            alight_stop.arrive_at + timedelta(minutes=DELIVERY_SPAN_MIN),
            itinerary.train_code,
        )
    )
    for step in steps:
        _require_window(need, step)
    return steps


# --- 宠物托运：物种与尺寸限制 -------------------------------------------


def plan_pet(
    catalog: ServiceCatalog,
    need: PetNeed,
    passenger_itinerary: Itinerary,
) -> list[PlannedStep]:
    policy = catalog.pet_policy
    if not need.species:
        raise PlanningError("species_required", "宠物物种不能为空")
    if not policy.species_allowed(need.species):
        raise PlanningError("species_forbidden", f"物种 {need.species} 不在托运允许范围")
    if need.weight_kg <= 0:
        raise PlanningError("bad_weight", "宠物体重必须为正数")
    if need.weight_kg > policy.max_weight_kg:
        raise PlanningError("weight_exceeded", f"含笼重量超过 {policy.max_weight_kg:g}kg 上限")
    dims = (need.crate_length_cm, need.crate_width_cm, need.crate_height_cm)
    if any(dim <= 0 for dim in dims):
        raise PlanningError("bad_crate", "笼具长宽高必须为正数")
    if not policy.crate_within_limits(*dims):
        raise PlanningError(
            "crate_exceeded",
            f"笼具尺寸 {dims[0]}x{dims[1]}x{dims[2]}cm 超过限制 "
            f"{policy.max_crate_cm[0]}x{policy.max_crate_cm[1]}x{policy.max_crate_cm[2]}cm",
        )

    origin = passenger_itinerary.board_station if need.itinerary is None else need.itinerary.board_station
    destination = (
        passenger_itinerary.alight_station
        if need.itinerary is None
        else need.itinerary.alight_station
    )
    if not policy.stations_allowed(origin, destination):
        raise PlanningError("station_not_served", "宠物托运起终站不在指定车站范围")
    itinerary = _itinerary_for(
        catalog,
        need.itinerary,
        passenger_itinerary,
        origin,
        destination,
        allowed_trains=policy.pet_trains,
    )

    origin_stop = itinerary.stop_at(origin)
    dest_stop = itinerary.stop_at(destination)
    assert origin_stop is not None and dest_stop is not None
    steps = [
        PlannedStep(
            TaskKind.PET_DROPOFF,
            origin,
            origin_stop.depart_at - timedelta(minutes=PET_DROPOFF_LEAD_MIN),
            origin_stop.depart_at - timedelta(minutes=PET_DROPOFF_TAIL_MIN),
            itinerary.train_code,
        ),
        PlannedStep(
            TaskKind.PET_PICKUP,
            destination,
            dest_stop.arrive_at,
            dest_stop.arrive_at + timedelta(minutes=PET_PICKUP_SPAN_MIN),
            itinerary.train_code,
        ),
    ]
    for step in steps:
        _require_window(need, step)
    return steps


# --- 行李服务：件数与路线 -----------------------------------------------


def plan_luggage(
    catalog: ServiceCatalog,
    need: LuggageNeed,
    passenger_itinerary: Itinerary,
) -> list[PlannedStep]:
    policy = catalog.luggage_policy
    count = len(need.items)
    if not policy.piece_count_allowed(count):
        raise PlanningError(
            "piece_count_exceeded",
            f"行李件数 {count} 超出每单 {policy.max_items_per_booking} 件上限",
        )
    seen: set[str] = set()
    for item in need.items:
        if not item.item_id or item.item_id in seen:
            raise PlanningError("bad_item", "行李件标识缺失或重复")
        seen.add(item.item_id)
        if not policy.weight_allowed(item.weight_kg):
            raise PlanningError(
                "weight_exceeded",
                f"行李 {item.item_id} 重量 {item.weight_kg:g}kg 超出 "
                f"{policy.max_weight_per_item_kg:g}kg 限制",
            )

    origin = need.origin_station or passenger_itinerary.board_station
    destination = need.destination_station or passenger_itinerary.alight_station
    if not policy.route_allowed(origin, destination):
        raise PlanningError("route_forbidden", "行李路线必须在两个不同的服务站之间")
    itinerary = _itinerary_for(
        catalog, need.itinerary, passenger_itinerary, origin, destination
    )

    origin_stop = itinerary.stop_at(origin)
    dest_stop = itinerary.stop_at(destination)
    assert origin_stop is not None and dest_stop is not None
    steps = [
        PlannedStep(
            TaskKind.LUGGAGE_PICKUP,
            origin,
            origin_stop.depart_at - timedelta(minutes=LUGGAGE_PICKUP_LEAD_MIN),
            origin_stop.depart_at - timedelta(minutes=LUGGAGE_PICKUP_TAIL_MIN),
            itinerary.train_code,
        ),
        PlannedStep(
            TaskKind.LUGGAGE_DELIVERY,
            destination,
            dest_stop.arrive_at + timedelta(minutes=LUGGAGE_DELIVERY_LAG_MIN),
            dest_stop.arrive_at + timedelta(minutes=LUGGAGE_DELIVERY_SPAN_MIN),
            itinerary.train_code,
        ),
    ]
    for step in steps:
        _require_window(need, step)
    return steps


# --- 临时身份证明：有效期限 ---------------------------------------------


def plan_temp_id(catalog: ServiceCatalog, need: TempIdNeed) -> list[PlannedStep]:
    _require_station(catalog, need.issue_station, "制证站")
    if need.window_start >= need.window_end:
        raise PlanningError("bad_window", "服务时间窗开始必须早于结束")
    if need.validity_hours <= 0:
        raise PlanningError("bad_validity", "临时身份证明有效期必须为正数")
    validity = timedelta(hours=need.validity_hours)
    if validity > catalog.temp_id_max_validity:
        raise PlanningError(
            "validity_exceeded",
            f"临时身份证明有效期不得超过 {catalog.temp_id_max_validity}",
        )
    # 核验出具与现场交付必须在旅客预约窗口内连续完成
    verify_end = need.window_start + (need.window_end - need.window_start) / 2
    steps = [
        PlannedStep(
            TaskKind.ID_VERIFY,
            need.issue_station,
            need.window_start,
            verify_end,
        ),
        PlannedStep(
            TaskKind.ID_HANDOVER,
            need.issue_station,
            verify_end,
            need.window_end,
        ),
    ]
    return steps


PLANNERS = {
    ServiceType.KEY_PASSENGER: plan_key_passenger,
    ServiceType.PET: plan_pet,
    ServiceType.LUGGAGE: plan_luggage,
    ServiceType.TEMP_ID: plan_temp_id,
}
