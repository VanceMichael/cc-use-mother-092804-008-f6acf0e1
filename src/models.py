"""铁路便民服务后端的领域模型。

模型只表达领域事实与状态，不做输入校验和持久化。时间统一使用
naive ``datetime``，由上层保证口径一致（车站本地时间）。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional


# --- 基础枚举 -----------------------------------------------------------


class ServiceType(str, enum.Enum):
    """平台提供的四类便民服务。"""

    KEY_PASSENGER = "key_passenger"  # 重点旅客陪护（重点旅客预约）
    PET = "pet"                      # 爱宠行（宠物托运）
    LUGGAGE = "luggage"              # 轻装行（行李搬运/托运）
    TEMP_ID = "temp_id"              # 临时身份证明


class BookingStatus(str, enum.Enum):
    PENDING_REVIEW = "pending_review"  # 关键字段变更，等待人工确认
    CONFIRMED = "confirmed"            # 已受理，任务已生成
    CANCELLED = "cancelled"            # 整单取消（已完成环节仍保留）


class TaskKind(str, enum.Enum):
    """接力任务的环节类型。"""

    PICKUP = "pickup"            # 进站接送（首段）
    TRANSFER = "transfer"        # 站内分段交接
    BOARDING = "boarding"        # 送上车/车长交接
    ONBOARD = "onboard"          # 车上看护或看护移交
    DISEMBARK = "disembark"      # 下车接站
    DELIVERY = "delivery"        # 出站送达/目的地交付
    PET_DROPOFF = "pet_dropoff"  # 宠物托运受理
    PET_PICKUP = "pet_pickup"    # 宠物到达交付
    LUGGAGE_PICKUP = "luggage_pickup"  # 行李上门/进站收件
    LUGGAGE_DELIVERY = "luggage_delivery"  # 行李送达
    ID_VERIFY = "id_verify"      # 临时身份证明核验出具
    ID_HANDOVER = "id_handover"  # 临时身份证明现场交付


class TaskStatus(str, enum.Enum):
    PLANNED = "planned"      # 尚未发生，等待执行
    ASSIGNED = "assigned"    # 已指派值班人员，未开始
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"  # 已完成，取消与改签均不可撤销
    CANCELLED = "cancelled"  # 仅尚未发生的环节可进入此状态


class EligibilityStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


# --- 旅客与同行关系 ------------------------------------------------------


@dataclass(frozen=True)
class Passenger:
    """预约中的一名旅客。``primary`` 标识预约本人。"""

    passenger_id: str
    name: str
    is_primary: bool = False
    # 与主预约人的同行关系，如 本人/同行成人/随行儿童；本人为空
    relation: Optional[str] = None


# --- 行程 / 车次 --------------------------------------------------------


@dataclass(frozen=True)
class TrainStop:
    """车次在某站的到发时刻。"""

    station_code: str
    arrive_at: Optional[datetime] = None
    depart_at: Optional[datetime] = None


@dataclass
class Itinerary:
    """一次乘车行程。宠物或行李可与旅客不同车次（单独出行/跨站运输）。"""

    train_code: str
    board_station: str
    alight_station: str
    stops: tuple[TrainStop, ...] = ()

    def station_sequence(self) -> list[str]:
        """按运行方向给出停靠站序列。"""
        return [stop.station_code for stop in self.stops]

    def stop_at(self, station_code: str) -> Optional[TrainStop]:
        for stop in self.stops:
            if stop.station_code == station_code:
                return stop
        return None

    def includes(self, station_code: str) -> bool:
        return self.stop_at(station_code) is not None

    def station_rank(self, station_code: str) -> Optional[int]:
        for rank, code in enumerate(self.station_sequence()):
            if code == station_code:
                return rank
        return None


# --- 四类服务需求 -------------------------------------------------------


@dataclass
class ServiceNeed:
    """单项服务需求的基类，持有服务类型与所属旅客。"""

    service_type: ServiceType
    passenger_id: str
    # 旅客期望的服务时间窗 [window_start, window_end]
    window_start: datetime
    window_end: datetime
    eligibility: EligibilityStatus = EligibilityStatus.PENDING
    eligibility_note: str = ""
    cancelled: bool = False  # 单项服务被局部取消

    def as_dict(self) -> dict:
        raise NotImplementedError


@dataclass
class KeyPassengerNeed(ServiceNeed):
    """重点旅客陪护：按旅客类别核验资格，再分段接力。"""

    category: str = ""          # elderly / pregnant / assisted_device 等目录内类别
    equipment: str = ""         # 需要的辅助设备，如 轮椅
    boarding_station: str = ""  # 进站接送站
    alight_station: str = ""    # 出站送达站
    transfer_stations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.service_type = ServiceType.KEY_PASSENGER


@dataclass
class PetNeed(ServiceNeed):
    """宠物托运：物种与尺寸受车次/车站政策约束，可与主人不同车次。"""

    species: str = ""           # dog / cat / ...
    pet_name: str = ""
    # 运载笼具尺寸（厘米）与重量（千克）
    crate_length_cm: int = 0
    crate_width_cm: int = 0
    crate_height_cm: int = 0
    weight_kg: float = 0.0
    # 宠物单独出行时使用独立车次；为空则随主预约人车次
    consignor_name: str = ""
    itinerary: Optional[Itinerary] = None


@dataclass
class LuggageItem:
    """一件行李。"""

    item_id: str
    kind: str               # normal / fragile / oversized，目录约束可扩展
    weight_kg: float = 0.0
    declared_value: int = 0


@dataclass
class LuggageNeed(ServiceNeed):
    """行李服务：守住件数与路线，允许跨站运输（起终点异于旅客行程）。"""

    items: tuple[LuggageItem, ...] = ()
    origin_station: str = ""
    destination_station: str = ""
    # 行李跨站运输可走单独车次
    itinerary: Optional[Itinerary] = None
    pickup_address: str = ""
    delivery_address: str = ""


@dataclass
class TempIdNeed(ServiceNeed):
    """临时身份证明：出具后只能在有效期限内使用。"""

    reason: str = ""
    issue_station: str = ""
    validity_hours: float = 24.0  # 申请的有效时长，受目录上限约束
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    certificate_no: str = ""

    def valid_at(self, moment: datetime) -> bool:
        if self.issued_at is None or self.expires_at is None:
            return False
        return self.issued_at <= moment <= self.expires_at

    @property
    def validity(self) -> Optional[timedelta]:
        if self.issued_at is None or self.expires_at is None:
            return None
        return self.expires_at - self.issued_at


# --- 接力任务与现场交接 --------------------------------------------------


@dataclass
class Task:
    """服务链上的一个分段环节。

    相邻环节在中间站通过交接记录串联。任务一旦 ``COMPLETED`` 即不可
    撤销——取消或改签只影响尚未发生的环节。
    """

    task_id: str
    service_type: ServiceType
    kind: TaskKind
    station_code: str
    scheduled_start: datetime
    scheduled_end: datetime
    seq: int
    assignee_id: Optional[str] = None
    status: TaskStatus = TaskStatus.PLANNED
    completed_at: Optional[datetime] = None
    completed_by: Optional[str] = None
    note: str = ""
    # 宠物/行李环节对应的车次，便于车站核对单独出行的对象
    train_code: Optional[str] = None

    @property
    def is_finished(self) -> bool:
        return self.status is TaskStatus.COMPLETED

    @property
    def is_open(self) -> bool:
        return self.status in (
            TaskStatus.PLANNED,
            TaskStatus.ASSIGNED,
            TaskStatus.IN_PROGRESS,
        )


@dataclass
class Handover:
    """两个相邻环节之间的现场交接记录，构成可追溯责任链。"""

    handover_id: str
    task_id_from: str
    task_id_to: str
    station_code: str
    at: datetime
    from_staff_id: str
    to_staff_id: str
    summary: str = ""
    # 交接对象快照（宠物笼/行李件数/旅客状态/证明编号）
    objects: tuple[str, ...] = ()


# --- 人工复核单 ---------------------------------------------------------


@dataclass
class ReviewCase:
    """重复请求命中已存在预约、但关键字段发生变化时产生。"""

    review_id: str
    booking_id: str
    idempotency_key: str
    changed_fields: tuple[str, ...]
    before: dict
    after: dict
    created_at: datetime
    new_raw: dict = field(default_factory=dict)
    fingerprint: str = ""
    resolved_at: Optional[datetime] = None
    resolution: str = ""  # accepted / rejected
    note: str = ""


# --- 预约 ---------------------------------------------------------------


@dataclass
class Booking:
    """一次便民服务预约，可同时携带四类服务需求。"""

    booking_id: str
    idempotency_key: str
    passengers: tuple[Passenger, ...]
    itinerary: Itinerary
    needs: list[ServiceNeed] = field(default_factory=list)
    status: BookingStatus = BookingStatus.CONFIRMED
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # 最近一次已确认请求体快照，供关键字段变化对比
    request_snapshot: dict = field(default_factory=dict)
    key_fingerprint: str = ""
    cancelled_at: Optional[datetime] = None
    cancel_reason: str = ""
    version: int = 1  # 每次改签/确认 +1
    tasks: list[Task] = field(default_factory=list)
    handovers: list[Handover] = field(default_factory=list)
    reviews: list[ReviewCase] = field(default_factory=list)
    events: list["DomainEvent"] = field(default_factory=list)

    def primary_passenger(self) -> Passenger:
        for passenger in self.passengers:
            if passenger.is_primary:
                return passenger
        return self.passengers[0]

    def passenger(self, passenger_id: str) -> Optional[Passenger]:
        for passenger in self.passengers:
            if passenger.passenger_id == passenger_id:
                return passenger
        return None

    def need_for(self, service_type: ServiceType) -> Optional[ServiceNeed]:
        for need in self.needs:
            if need.service_type is service_type:
                return need
        return None

    def tasks_of(self, service_type: ServiceType) -> list[Task]:
        return [task for task in self.tasks if task.service_type is service_type]

    def open_tasks(self, service_type: Optional[ServiceType] = None) -> list[Task]:
        tasks = self.tasks if service_type is None else self.tasks_of(service_type)
        return [task for task in tasks if task.is_open]


@dataclass
class DomainEvent:
    """追加式领域事件，用于还原从预约到完成的全过程。"""

    at: datetime
    event_type: str
    detail: dict = field(default_factory=dict)
    actor_id: Optional[str] = None
