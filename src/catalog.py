"""车站、车次、值班人员与服务政策目录。

目录数据全部为虚构种子数据，不对应真实车次时刻或真实人员。规划器
（``planning``）只通过目录判断"哪些站、哪趟车、什么尺寸、多少件"
是否被允许，业务规则不散落在适配层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .models import Itinerary, ServiceType, TrainStop


@dataclass(frozen=True)
class Station:
    code: str
    name: str


@dataclass(frozen=True)
class Staff:
    """车站现场值班人员。``services`` 限定其可承接的服务类型。"""

    staff_id: str
    name: str
    station_code: str
    role: str  # attendant / pet_clerk / luggage_clerk / clerk / supervisor
    services: frozenset[ServiceType]


@dataclass(frozen=True)
class TrainInfo:
    train_code: str
    stops: tuple[TrainStop, ...]

    def as_itinerary(self, board: str, alight: str) -> Itinerary:
        codes = [stop.station_code for stop in self.stops]
        if board not in codes or alight not in codes:
            raise ValueError(f"{self.train_code} 不停靠 {board} 或 {alight}")
        start, end = codes.index(board), codes.index(alight)
        if start >= end:
            raise ValueError(f"{self.train_code} 乘降站顺序错误")
        return Itinerary(
            train_code=self.train_code,
            board_station=board,
            alight_station=alight,
            stops=self.stops[start : end + 1],
        )


@dataclass(frozen=True)
class PetPolicy:
    """宠物托运的物种与笼具尺寸限制（按车次约束）。"""

    allowed_species: frozenset[str]
    pet_stations: frozenset[str]
    pet_trains: frozenset[str]
    max_crate_cm: tuple[int, int, int]  # 长、宽、高上限
    max_weight_kg: float

    def species_allowed(self, species: str) -> bool:
        return species in self.allowed_species

    def crate_within_limits(self, length: int, width: int, height: int) -> bool:
        limit_l, limit_w, limit_h = self.max_crate_cm
        return (length, width, height) <= (limit_l, limit_w, limit_h)

    def train_allowed(self, train_code: str) -> bool:
        return train_code in self.pet_trains

    def stations_allowed(self, origin: str, destination: str) -> bool:
        return origin in self.pet_stations and destination in self.pet_stations


@dataclass(frozen=True)
class LuggagePolicy:
    """行李服务的件数与路线约束。"""

    max_items_per_booking: int
    max_weight_per_item_kg: float
    service_stations: frozenset[str]

    def piece_count_allowed(self, count: int) -> bool:
        return 0 < count <= self.max_items_per_booking

    def weight_allowed(self, weight_kg: float) -> bool:
        return 0 < weight_kg <= self.max_weight_per_item_kg

    def route_allowed(self, origin: str, destination: str) -> bool:
        return (
            origin in self.service_stations
            and destination in self.service_stations
            and origin != destination
        )


@dataclass(frozen=True)
class KeyPassengerPolicy:
    """重点旅客类别目录与资格说明。"""

    categories: dict[str, str]  # 代码 -> 说明

    def category_known(self, category: str) -> bool:
        return category in self.categories


@dataclass
class ServiceCatalog:
    stations: dict[str, Station]
    trains: dict[str, TrainInfo]
    staff: list[Staff]
    pet_policy: PetPolicy
    luggage_policy: LuggagePolicy
    key_passenger_policy: KeyPassengerPolicy
    # 临时身份证明最长有效期
    temp_id_max_validity: timedelta = timedelta(hours=24)

    def station(self, code: str) -> Optional[Station]:
        return self.stations.get(code)

    def train(self, code: str) -> Optional[TrainInfo]:
        return self.trains.get(code)

    def staff_at(
        self, station_code: str, service_type: Optional[ServiceType] = None
    ) -> list[Staff]:
        result = [member for member in self.staff if member.station_code == station_code]
        if service_type is not None:
            result = [member for member in result if service_type in member.services]
        return result

    def staff_member(self, staff_id: str) -> Optional[Staff]:
        for member in self.staff:
            if member.staff_id == staff_id:
                return member
        return None


# --- 虚构种子数据 -------------------------------------------------------


def _stop(station: str, arrive: str, depart: str) -> TrainStop:
    return TrainStop(
        station_code=station,
        arrive_at=datetime.fromisoformat(arrive),
        depart_at=datetime.fromisoformat(depart),
    )


def seed_catalog() -> ServiceCatalog:
    """构造一份完全虚构、自洽的目录，供本地运行与测试使用。"""
    stations = {
        code: Station(code, name)
        for code, name in (
            ("VNP", "北京南站"),
            ("JGK", "济南西站"),
            ("NPH", "南京南站"),
            ("HGH", "杭州东站"),
        )
    }

    day = "2026-10-08"
    trains = {
        "G101": TrainInfo(
            "G101",
            (
                _stop("VNP", f"{day}T08:00", f"{day}T08:12"),
                _stop("JGK", f"{day}T09:40", f"{day}T09:43"),
                _stop("NPH", f"{day}T12:05", f"{day}T12:09"),
                _stop("HGH", f"{day}T13:20", f"{day}T13:20"),
            ),
        ),
        "G205": TrainInfo(
            "G205",
            (
                _stop("VNP", f"{day}T10:00", f"{day}T10:10"),
                _stop("NPH", f"{day}T14:00", f"{day}T14:04"),
                _stop("HGH", f"{day}T15:10", f"{day}T15:10"),
            ),
        ),
        "G307": TrainInfo(
            "G307",
            (
                _stop("JGK", f"{day}T07:30", f"{day}T07:38"),
                _stop("NPH", f"{day}T10:20", f"{day}T10:24"),
                _stop("HGH", f"{day}T11:40", f"{day}T11:40"),
            ),
        ),
    }

    staff = [
        # 北京南
        Staff("S-VNP-01", "安楠", "VNP", "attendant", frozenset({ServiceType.KEY_PASSENGER})),
        Staff("S-VNP-02", "白璐", "VNP", "attendant", frozenset({ServiceType.KEY_PASSENGER})),
        Staff("S-VNP-03", "蔡进", "VNP", "pet_clerk", frozenset({ServiceType.PET})),
        Staff("S-VNP-04", "邓肯", "VNP", "luggage_clerk", frozenset({ServiceType.LUGGAGE})),
        Staff("S-VNP-05", "方澄", "VNP", "clerk", frozenset({ServiceType.TEMP_ID})),
        # 济南西
        Staff("S-JGK-01", "高航", "JGK", "attendant", frozenset({ServiceType.KEY_PASSENGER})),
        Staff("S-JGK-02", "韩静", "JGK", "attendant", frozenset({ServiceType.KEY_PASSENGER})),
        Staff("S-JGK-03", "蒋恺", "JGK", "luggage_clerk", frozenset({ServiceType.LUGGAGE})),
        Staff("S-JGK-04", "孔雯", "JGK", "clerk", frozenset({ServiceType.TEMP_ID})),
        # 南京南
        Staff("S-NPH-01", "李牧", "NPH", "attendant", frozenset({ServiceType.KEY_PASSENGER})),
        Staff("S-NPH-02", "梅然", "NPH", "attendant", frozenset({ServiceType.KEY_PASSENGER})),
        Staff("S-NPH-03", "曲芃", "NPH", "pet_clerk", frozenset({ServiceType.PET})),
        Staff("S-NPH-04", "阮晴", "NPH", "luggage_clerk", frozenset({ServiceType.LUGGAGE})),
        Staff("S-NPH-05", "沈拓", "NPH", "clerk", frozenset({ServiceType.TEMP_ID})),
        # 杭州东
        Staff("S-HGH-01", "汤妍", "HGH", "attendant", frozenset({ServiceType.KEY_PASSENGER})),
        Staff("S-HGH-02", "童磊", "HGH", "pet_clerk", frozenset({ServiceType.PET})),
        Staff("S-HGH-03", "魏岚", "HGH", "luggage_clerk", frozenset({ServiceType.LUGGAGE})),
        Staff("S-HGH-04", "薛冰", "HGH", "clerk", frozenset({ServiceType.TEMP_ID})),
        # 主管（每站一名，可查看本站全部服务）
        Staff("M-VNP", "袁主管", "VNP", "supervisor", frozenset(ServiceType)),
        Staff("M-HGH", "邹主管", "HGH", "supervisor", frozenset(ServiceType)),
    ]

    pet_policy = PetPolicy(
        allowed_species=frozenset({"dog", "cat"}),
        pet_stations=frozenset({"VNP", "NPH", "HGH"}),
        pet_trains=frozenset({"G101", "G205"}),
        max_crate_cm=(120, 70, 80),
        max_weight_kg=50.0,
    )
    luggage_policy = LuggagePolicy(
        max_items_per_booking=5,
        max_weight_per_item_kg=50.0,
        service_stations=frozenset({"VNP", "JGK", "NPH", "HGH"}),
    )
    key_policy = KeyPassengerPolicy(
        categories={
            "elderly": "年满六十周岁且需要站车帮扶",
            "pregnant": "孕妇旅客",
            "assisted_device": "需要轮椅、担架等辅助设备",
            "post_illness": "术后或大病初愈行动受限",
        }
    )
    return ServiceCatalog(
        stations=stations,
        trains=trains,
        staff=staff,
        pet_policy=pet_policy,
        luggage_policy=luggage_policy,
        key_passenger_policy=key_policy,
        temp_id_max_validity=timedelta(hours=24),
    )
