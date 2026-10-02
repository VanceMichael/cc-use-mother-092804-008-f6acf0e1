"""铁路便民服务领域模型：旅客需求、同行关系、车次、资格、托运物件、站点、时间窗、交接记录。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ServiceType(str, Enum):
    """四类便民服务。"""

    KEY_PASSENGER = "key_passenger"  # 重点旅客陪护
    PET = "pet"                      # 爱宠行（宠物托运）
    LUGGAGE = "luggage"              # 轻装行（行李搬运/跨站运输）
    TEMP_CREDENTIAL = "temp_credential"  # 临时身份证明

    @property
    def label(self) -> str:
        return _SERVICE_LABELS[self]


_SERVICE_LABELS = {
    ServiceType.KEY_PASSENGER: "重点旅客陪护",
    ServiceType.PET: "宠物托运",
    ServiceType.LUGGAGE: "行李服务",
    ServiceType.TEMP_CREDENTIAL: "临时身份证明",
}


class EligibilityCategory(str, Enum):
    """重点旅客资格类别。"""

    ELDERLY = "elderly"                    # 老人
    PREGNANT = "pregnant"                  # 孕妇
    ASSISTIVE_DEVICE = "assistive_device"  # 需要辅助设备（轮椅、担架等）

    @property
    def label(self) -> str:
        return {
            EligibilityCategory.ELDERLY: "老人",
            EligibilityCategory.PREGNANT: "孕妇",
            EligibilityCategory.ASSISTIVE_DEVICE: "辅助设备",
        }[self]


class RelationKind(str, Enum):
    COMPANION = "companion"    # 同行旅客
    PET_OWNER = "pet_owner"    # 宠物主人
    RECEIVER = "receiver"      # 到站接收人（宠物单独出行时）


class TaskKind(str, Enum):
    ORIGIN_ESCORT = "origin_escort"        # 进站接送至站台
    RELAY_LEG = "relay_leg"                # 分段接力（站间/车上）
    DEST_ESCORT = "dest_escort"            # 站台送出站
    PET_CONSIGN = "pet_consign"            # 宠物发运交接
    PET_TRANSIT = "pet_transit"            # 宠物运输段
    PET_CLAIM = "pet_claim"                # 宠物到达领取
    LUGGAGE_PICKUP = "luggage_pickup"      # 行李取件
    LUGGAGE_TRANSIT = "luggage_transit"    # 行李跨站运输段
    LUGGAGE_DELIVER = "luggage_deliver"    # 行李送达
    CREDENTIAL_ISSUE = "credential_issue"  # 临时身份证明开具

    @property
    def label(self) -> str:
        return _TASK_LABELS[self]


_TASK_LABELS = {
    TaskKind.ORIGIN_ESCORT: "进站接送",
    TaskKind.RELAY_LEG: "分段接力",
    TaskKind.DEST_ESCORT: "出站送达",
    TaskKind.PET_CONSIGN: "宠物发运",
    TaskKind.PET_TRANSIT: "宠物运输",
    TaskKind.PET_CLAIM: "宠物交付",
    TaskKind.LUGGAGE_PICKUP: "行李取件",
    TaskKind.LUGGAGE_TRANSIT: "行李运输",
    TaskKind.LUGGAGE_DELIVER: "行李送达",
    TaskKind.CREDENTIAL_ISSUE: "开具临时证明",
}


# 任务所需岗位
ROLE_STATION_ESCORT = "station_escort"      # 车站接送员
ROLE_TRAIN_CONDUCTOR = "train_conductor"    # 列车长
ROLE_PET_CLERK = "pet_clerk"                # 宠物托运员
ROLE_LUGGAGE_CLERK = "luggage_clerk"        # 行李承运员
ROLE_DUTY_OFFICER = "duty_officer"          # 客运值班员

ROLE_LABELS = {
    ROLE_STATION_ESCORT: "车站接送员",
    ROLE_TRAIN_CONDUCTOR: "列车长",
    ROLE_PET_CLERK: "宠物托运员",
    ROLE_LUGGAGE_CLERK: "行李承运员",
    ROLE_DUTY_OFFICER: "客运值班员",
}

TRANSIT_KINDS = frozenset({TaskKind.RELAY_LEG, TaskKind.PET_TRANSIT, TaskKind.LUGGAGE_TRANSIT})
FIRST_MILE_KINDS = frozenset(
    {TaskKind.ORIGIN_ESCORT, TaskKind.PET_CONSIGN, TaskKind.LUGGAGE_PICKUP, TaskKind.CREDENTIAL_ISSUE}
)


class TaskStatus(str, Enum):
    PENDING = "pending"      # 已生成，待排班
    ASSIGNED = "assigned"    # 已明确现场人员
    COMPLETED = "completed"  # 已完成（接送/交接已发生，永久可追溯）
    CANCELLED = "cancelled"  # 仅撤销了尚未发生的环节
    EXPIRED = "expired"      # 临时证明超过有效期未开具

    @property
    def label(self) -> str:
        return {
            TaskStatus.PENDING: "待排班",
            TaskStatus.ASSIGNED: "已派工",
            TaskStatus.COMPLETED: "已完成",
            TaskStatus.CANCELLED: "已撤销",
            TaskStatus.EXPIRED: "已过期",
        }[self]


class ServiceStatus(str, Enum):
    AWAITING_ELIGIBILITY = "awaiting_eligibility"  # 重点旅客资格核验中，暂不生成接力
    PENDING_REVIEW = "pending_review"  # 关键字段变化，等待人工确认
    CONFIRMED = "confirmed"            # 资格核验通过、任务已生成
    IN_PROGRESS = "in_progress"        # 已有现场环节完成
    COMPLETED = "completed"            # 全部环节完成
    CANCELLED = "cancelled"            # 全部环节撤销（无已完成环节）
    PARTIALLY_CANCELLED = "partially_cancelled"  # 部分已完成、其余撤销，链路仍可追溯

    @property
    def label(self) -> str:
        return {
            ServiceStatus.AWAITING_ELIGIBILITY: "资格待核验",
            ServiceStatus.PENDING_REVIEW: "待人工确认",
            ServiceStatus.CONFIRMED: "已确认",
            ServiceStatus.IN_PROGRESS: "进行中",
            ServiceStatus.COMPLETED: "已完成",
            ServiceStatus.CANCELLED: "已取消",
            ServiceStatus.PARTIALLY_CANCELLED: "部分撤销",
        }[self]


@dataclass(frozen=True)
class Eligibility:
    """服务资格：类别与核验依据（不记录真实证件号）。"""

    category: EligibilityCategory
    evidence: str
    verified: bool = False
    verified_by: str | None = None
    verified_at: datetime | None = None


@dataclass(frozen=True)
class Person:
    person_id: str
    name: str
    eligibility: Eligibility | None = None


@dataclass(frozen=True)
class Relation:
    """同行关系：subject 在 kind 语义下与 accompanies 关联。"""

    subject_id: str
    accompanies_id: str
    kind: RelationKind


@dataclass(frozen=True)
class Cage:
    length_cm: float
    width_cm: float
    height_cm: float

    def dimensions(self) -> tuple[float, float, float]:
        return self.length_cm, self.width_cm, self.height_cm


@dataclass(frozen=True)
class Pet:
    pet_id: str
    species: str
    weight_kg: float
    cage: Cage
    owner_id: str
    unaccompanied: bool = False  # 宠物单独出行（主人不乘同一车次）
    receiver_id: str | None = None  # 单独出行时的到站接收人


@dataclass(frozen=True)
class LuggageItem:
    item_id: str
    description: str
    weight_kg: float


@dataclass(frozen=True)
class Station:
    code: str
    name: str
    pet_service: bool = True  # 该站是否开办宠物托运


@dataclass(frozen=True)
class Staff:
    """现场交接人员，归属站点与岗位。"""

    staff_id: str
    name: str
    station_code: str
    role: str


@dataclass(frozen=True)
class Stop:
    station_code: str
    arrive_at: datetime | None  # 始发站为 None
    depart_at: datetime | None  # 终到站为 None


@dataclass(frozen=True)
class Train:
    code: str
    stops: tuple[Stop, ...]

    def station_index(self, station_code: str) -> int:
        for index, stop in enumerate(self.stops):
            if stop.station_code == station_code:
                return index
        raise KeyError(station_code)

    def serves(self, station_code: str) -> bool:
        return any(stop.station_code == station_code for stop in self.stops)

    def between(self, from_code: str, to_code: str) -> tuple[Stop, ...]:
        """返回两站之间（含两端）的停站序列，要求发站在前、到站在后。"""
        start = self.station_index(from_code)
        end = self.station_index(to_code)
        if start >= end:
            raise ValueError("乘降区间方向错误")
        return self.stops[start : end + 1]


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError("服务时间窗无效")

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment <= self.end


@dataclass(frozen=True)
class TempCredential:
    """临时身份证明：只在有效期限内使用。"""

    credential_id: str
    passenger_id: str
    station_code: str
    valid_from: datetime
    valid_until: datetime

    def valid_at(self, moment: datetime) -> bool:
        return self.valid_from <= moment <= self.valid_until


@dataclass
class ServiceRequest:
    """一次线上预约提交的完整需求资料。"""

    request_id: str
    passenger_id: str
    train_code: str
    service_type: ServiceType
    window: TimeWindow
    persons: tuple[Person, ...]
    route: tuple[str, str] | None = None              # 乘降/托运/运输区间 (发站, 到站)
    target_passenger_id: str | None = None            # 重点旅客本人或被陪护人
    relations: tuple[Relation, ...] = ()
    pets: tuple[Pet, ...] = ()
    pet_id: str | None = None
    items: tuple[LuggageItem, ...] = ()
    item_ids: tuple[str, ...] = ()
    credential: TempCredential | None = None
    note: str = ""


@dataclass
class ServiceTask:
    """现场最小执行环节：一个站点、一个时间窗、一名责任人。"""

    task_id: str
    kind: TaskKind
    station_code: str
    role: str
    window_start: datetime
    window_end: datetime
    segment_from: str | None = None
    segment_to: str | None = None
    item_ids: tuple[str, ...] = ()
    assignee_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    detail: str = ""
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None

    @property
    def started(self) -> bool:
        return self.status in (TaskStatus.ASSIGNED, TaskStatus.COMPLETED)

    @property
    def is_open(self) -> bool:
        return self.status in (TaskStatus.PENDING, TaskStatus.ASSIGNED)

    def in_window(self, moment: datetime) -> bool:
        return self.window_start <= moment <= self.window_end


@dataclass
class ServicePlan:
    """一类服务的环节清单与撤销标记。"""

    service_type: ServiceType
    task_ids: list[str] = field(default_factory=list)
    cancelled: bool = False


@dataclass
class HandoverRecord:
    """现场交接凭证：谁、在何站、把何物/何人、交给谁、件数多少。"""

    handover_id: str
    reservation_id: str
    task_id: str
    station_code: str
    handed_by: str
    handed_to: str
    at: datetime
    item_count: int
    note: str = ""


@dataclass
class HistoryEvent:
    """责任链上的不可变事实。"""

    seq: int
    at: datetime
    action: str
    actor: str
    detail: str = ""
    task_id: str | None = None


SERVICE_LABELS = _SERVICE_LABELS
