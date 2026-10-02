"""应用服务：线上受理、去重与人工确认、资格核验、派工与现场交接、局部取消、过期处理。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum

from src.railway.errors import ConflictError, PolicyViolation, ValidationError
from src.railway.models import (
    Eligibility,
    HandoverRecord,
    Person,
    ServicePlan,
    ServiceRequest,
    ServiceStatus,
    ServiceType,
    Staff,
    TaskStatus,
)
from src.railway.policies import ServiceNetwork
from src.railway.reservation import Planner, Reservation
from src.railway.views import build_chain, staff_task_views


class SubmitOutcome(str, Enum):
    CREATED = "created"                  # 新预约受理
    DUPLICATE = "duplicate"              # 重复请求，返回同一预约
    PENDING_REVIEW = "pending_review"    # 关键字段变化，进入人工确认


@dataclass(frozen=True)
class SubmitResult:
    reservation_id: str
    outcome: SubmitOutcome
    changed_fields: tuple[str, ...] = ()


def _anchor(request: ServiceRequest) -> tuple:
    """跨改签仍稳定的受理锚点：同一旅客的同一类服务意图，而非车次/路线/件数。

    件数、路线、车次、笼箱尺寸等一旦变化属于关键字段变化，走人工确认，
    不能因为锚点漂移而重复受理成新预约。
    """
    if request.service_type is ServiceType.PET:
        return (ServiceType.PET, request.passenger_id, request.pet_id)
    if request.service_type is ServiceType.LUGGAGE:
        return (ServiceType.LUGGAGE, request.passenger_id)
    if request.service_type is ServiceType.KEY_PASSENGER:
        return (ServiceType.KEY_PASSENGER, request.passenger_id, request.target_passenger_id)
    return (ServiceType.TEMP_CREDENTIAL, request.passenger_id)


def _key_signature(request: ServiceRequest):
    """锚点之外、一旦变化就必须人工确认的关键字段。"""
    if request.service_type is ServiceType.PET:
        pet = next(p for p in request.pets if p.pet_id == request.pet_id)
        return (
            request.train_code,
            request.route,
            pet.species,
            pet.weight_kg,
            pet.cage.dimensions(),
            pet.unaccompanied,
            pet.receiver_id,
        )
    if request.service_type is ServiceType.LUGGAGE:
        items = {item.item_id: (item.description, item.weight_kg) for item in request.items}
        return (
            request.train_code,
            request.route,
            tuple((i, items[i]) for i in sorted(request.item_ids)),
        )
    if request.service_type is ServiceType.KEY_PASSENGER:
        target = next(p for p in request.persons if p.person_id == request.target_passenger_id)
        category = target.eligibility.category if target.eligibility else None
        evidence = target.eligibility.evidence if target.eligibility else None
        return request.train_code, request.route, category, evidence
    credential = request.credential
    return (
        request.train_code,
        credential.station_code,
        credential.valid_from,
        credential.valid_until,
    )


_KEY_FIELD_LABELS = {
    ServiceType.PET: ("车次", "运输区间", "物种", "体重", "笼箱尺寸", "是否单独出行", "接收人"),
    ServiceType.LUGGAGE: ("车次", "运输区间", "行李件"),
    ServiceType.KEY_PASSENGER: ("车次", "乘降区间", "资格类别", "核验依据"),
    ServiceType.TEMP_CREDENTIAL: ("车次", "开具站", "有效期起", "有效期止"),
}

_TERMINAL = frozenset({ServiceStatus.CANCELLED, ServiceStatus.COMPLETED, ServiceStatus.PARTIALLY_CANCELLED})


class RailwayServiceBackend:
    """内存仓储式后端；事务边界即每个公开方法。"""

    def __init__(self, network: ServiceNetwork) -> None:
        self.network = network
        self._reservations: dict[str, Reservation] = {}
        self._by_request: dict[str, str] = {}
        self._anchors: dict[tuple, str] = {}
        self._staff: dict[str, Staff] = {}

    # -- 人员与值班登记 --------------------------------------------------

    def register_staff(self, staff: Staff) -> None:
        self._staff[staff.staff_id] = staff

    # -- 受理 ------------------------------------------------------------

    def submit(self, request: ServiceRequest, at) -> SubmitResult:
        self._validate_references(request)
        # 先跑一遍策略与规划，非法需求不进入受理。
        Planner(_provisional_id(request.request_id), self.network).build(request)

        if request.request_id in self._by_request:
            reservation_id = self._by_request[request.request_id]
            return SubmitResult(reservation_id, SubmitOutcome.DUPLICATE)

        anchor = _anchor(request)
        existing_id = self._anchors.get(anchor)
        if existing_id is not None:
            existing = self._reservations[existing_id]
            if existing.status in _TERMINAL and not existing.open_tasks():
                existing_id = None  # 上一周期已终结，允许重新预约。
        if existing_id is not None:
            existing = self._reservations[existing_id]
            if _key_signature(request) == _key_signature(existing.request):
                return SubmitResult(existing_id, SubmitOutcome.DUPLICATE)
            changed = tuple(
                label
                for label, old, new in zip(
                    _KEY_FIELD_LABELS[request.service_type],
                    _key_signature(existing.request),
                    _key_signature(request),
                )
                if old != new
            )
            existing.propose_change(request, changed, at)
            return SubmitResult(existing_id, SubmitOutcome.PENDING_REVIEW, changed)

        reservation_id = "R-" + uuid.uuid4().hex[:10]
        tasks = Planner(reservation_id, self.network).build(request)
        reservation = Reservation(
            reservation_id=reservation_id,
            request=request,
            plans={request.service_type: ServicePlan(request.service_type, [t.task_id for t in tasks])},
            tasks={t.task_id: t for t in tasks},
            task_order=[t.task_id for t in tasks],
            handovers=[],
            history=[],
        )
        reservation.record(at, "预约受理", "线上渠道", f"{request.service_type.label}，请求 {request.request_id}")
        reservation._recompute_status()
        if reservation.status is ServiceStatus.AWAITING_ELIGIBILITY:
            reservation.record(at, "资格待核验", "线上渠道", "核验通过后生成分段接力任务")
        self._reservations[reservation_id] = reservation
        self._by_request[request.request_id] = reservation_id
        self._anchors[anchor] = reservation_id
        return SubmitResult(reservation_id, SubmitOutcome.CREATED)

    def _validate_references(self, request: ServiceRequest) -> None:
        if not request.persons:
            raise ValidationError("预约必须至少包含一名旅客")
        ids = {p.person_id for p in request.persons}
        if request.passenger_id not in ids:
            raise ValidationError("预约旅客不在人员资料中")
        for relation in request.relations:
            if relation.subject_id not in ids or relation.accompanies_id not in ids:
                raise ValidationError("同行关系引用了不存在的旅客")
        for pet in request.pets:
            if pet.owner_id not in ids:
                raise ValidationError(f"宠物 {pet.pet_id} 的主人不在人员资料中")
            if pet.receiver_id is not None and pet.receiver_id not in ids:
                raise ValidationError(f"宠物 {pet.pet_id} 的接收人不在人员资料中")
        if request.service_type is ServiceType.KEY_PASSENGER:
            if request.target_passenger_id not in ids:
                raise ValidationError("重点旅客服务对象不在人员资料中")
        if request.service_type is ServiceType.PET and request.pet_id not in {p.pet_id for p in request.pets}:
            raise ValidationError("托运宠物不在宠物资料中")
        unknown_items = [i for i in request.item_ids if i not in {item.item_id for item in request.items}]
        if unknown_items:
            raise ValidationError(f"托运行李引用了不存在的件：{', '.join(unknown_items)}")
        if request.credential is not None and request.credential.passenger_id not in ids:
            raise ValidationError("临时证明持证人不在人员资料中")

    # -- 人工确认 --------------------------------------------------------

    def review_change(self, reservation_id: str, approve: bool, by: str, at) -> SubmitResult:
        reservation = self._get(reservation_id)
        if reservation.pending_change is None:
            raise ConflictError("该预约没有待确认的变更")
        pending = reservation.pending_change
        if approve:
            request = pending.request
            # 确认前再跑一次策略，防止变化后的资料违规。
            Planner(reservation_id, self.network).build(request)
            reservation.confirm_change(request, self.network, by, at)
            self._by_request[request.request_id] = reservation_id
            return SubmitResult(reservation_id, SubmitOutcome.CREATED, pending.changed_fields)
        reservation.reject_change(by, at)
        return SubmitResult(reservation_id, SubmitOutcome.DUPLICATE, pending.changed_fields)

    # -- 资格核验 --------------------------------------------------------

    def verify_eligibility(self, reservation_id: str, by: str, at) -> None:
        reservation = self._get(reservation_id)
        request = reservation.request
        target_id = request.target_passenger_id
        target = reservation.person(target_id)
        if target.eligibility is None:
            raise ValidationError("该旅客没有重点旅客资格资料")
        verified_person = Person(
            person_id=target.person_id,
            name=target.name,
            eligibility=Eligibility(
                category=target.eligibility.category,
                evidence=target.eligibility.evidence,
                verified=True,
                verified_by=by,
                verified_at=at,
            ),
        )
        persons = tuple(verified_person if p.person_id == target_id else p for p in request.persons)
        new_request = replace_request(request, persons=persons)
        Planner(reservation_id, self.network).build(new_request)  # 违规即拒绝
        reservation.activate_eligibility(new_request, self.network, by, at)

    # -- 列车调整 --------------------------------------------------------

    def adjust_train(self, reservation_id: str, new_train_code: str, by: str, at) -> None:
        reservation = self._get(reservation_id)
        train = self.network.train(new_train_code)
        if request_route := reservation.request.route:
            self.network.ensure_route(train, *request_route)
        reservation.adjust_train(new_train_code, self.network, by, at)

    # -- 派工与现场交接 --------------------------------------------------

    def assign_task(self, task_id: str, staff_id: str, at) -> None:
        reservation, task = self._find_task(task_id)
        staff = self._staff.get(staff_id)
        if staff is None:
            raise ValidationError(f"现场人员不存在：{staff_id}")
        if staff.role != task.role:
            raise ConflictError(f"{staff.name}岗位与环节要求不符")
        if task.role != "train_conductor" and staff.station_code != task.station_code:
            staff_station = self.network.station(staff.station_code)
            task_station = self.network.station(task.station_code)
            raise ConflictError(f"{staff.name}隶属{staff_station.name}，不能承接{task_station.name}的环节")
        if not task.is_open:
            raise ConflictError("环节已结束，不能派工")
        if at > task.window_end:
            raise ConflictError("已超过该环节的服务时间窗")
        task.assignee_id = staff_id
        task.status = TaskStatus.ASSIGNED
        reservation.record(at, "派工", staff.name, f"承接{task.kind.label}@{task.station_code}", task.task_id)

    def complete_handover(
        self, task_id: str, counterparty: str, at, item_count: int | None = None
    ) -> HandoverRecord:
        reservation, task = self._find_task(task_id)
        if task.status is not TaskStatus.ASSIGNED:
            raise ConflictError("环节尚未派工，不能交接")
        if not task.in_window(at):
            if task.kind.value == "credential_issue":
                raise PolicyViolation("临时身份证明只能在有效期限内开具")
            raise ConflictError("交接时间不在服务时间窗内")
        self._ensure_predecessors_done(reservation, task)
        expected = len(task.item_ids) if task.item_ids else 1
        actual = expected if item_count is None else item_count
        if actual != expected:
            raise PolicyViolation(f"件数不符：登记 {expected} 件，实际交接 {actual} 件，交接中止")
        staff = self._staff[task.assignee_id]
        task.status = TaskStatus.COMPLETED
        task.completed_at = at
        handover = HandoverRecord(
            handover_id="H-" + uuid.uuid4().hex[:10],
            reservation_id=reservation.reservation_id,
            task_id=task.task_id,
            station_code=task.station_code,
            handed_by=staff.staff_id,
            handed_to=counterparty,
            at=at,
            item_count=actual,
            note=task.detail,
        )
        reservation.handovers.append(handover)
        reservation.record(
            at,
            "现场交接",
            staff.name,
            f"{task.kind.label}，{actual} 件/人，交予 {counterparty}",
            task.task_id,
        )
        reservation._recompute_status()
        if reservation.status is ServiceStatus.COMPLETED:
            reservation.record(at, "服务完成", "系统", "全部环节完成")
        return handover

    def _ensure_predecessors_done(self, reservation: Reservation, task) -> None:
        plan = reservation.plans[reservation.request.service_type]
        chain = [tid for tid in plan.task_ids if tid in reservation.tasks]
        if task.task_id not in chain:
            return
        index = chain.index(task.task_id)
        for earlier_id in chain[:index]:
            earlier = reservation.tasks[earlier_id]
            if earlier.status is not TaskStatus.COMPLETED:
                raise ConflictError(
                    f"前序环节 {earlier.kind.label}@{earlier.station_code} 尚未完成，不能交接"
                )

    # -- 取消（只撤销尚未发生的环节）------------------------------------

    def cancel_service(self, reservation_id: str, service_type: ServiceType, by: str, at, reason: str) -> list[str]:
        reservation = self._get(reservation_id)
        return reservation.cancel_service(service_type, at, f"{reason}（{by}）")

    def cancel_reservation(self, reservation_id: str, by: str, at, reason: str) -> list[str]:
        reservation = self._get(reservation_id)
        return reservation.cancel_all(at, f"{reason}（{by}）")

    # -- 临时证明过期 ----------------------------------------------------

    def sweep_expired(self, at) -> list[str]:
        expired: list[str] = []
        for reservation in self._reservations.values():
            for task in list(reservation.tasks.values()):
                if (
                    task.kind.value == "credential_issue"
                    and task.is_open
                    and at > task.window_end
                ):
                    task.status = TaskStatus.EXPIRED
                    expired.append(task.task_id)
                    reservation.record(at, "证明过期", "系统", "超过有效期限未开具", task.task_id)
            reservation._recompute_status()
        return expired

    # -- 查询 ------------------------------------------------------------

    def get(self, reservation_id: str) -> Reservation:
        return self._get(reservation_id)

    def staff_tasks(self, staff_id: str):
        staff = self._staff.get(staff_id)
        if staff is None:
            raise ValidationError(f"现场人员不存在：{staff_id}")
        tasks = []
        for reservation in self._reservations.values():
            for task_id in reservation.task_order:
                task = reservation.tasks[task_id]
                if task.assignee_id == staff_id or (
                    task.is_open and task.role == staff.role and self._station_match(staff, task)
                ):
                    tasks.append((reservation, task))
        return staff_task_views(tasks, self.network, self._staff)

    @staticmethod
    def _station_match(staff: Staff, task) -> bool:
        return staff.role == "train_conductor" or staff.station_code == task.station_code

    def responsibility_chain(self, reservation_id: str):
        reservation = self._get(reservation_id)
        return build_chain(reservation, self._staff, self.network)

    def _find_task(self, task_id: str) -> tuple[Reservation, object]:
        for reservation in self._reservations.values():
            task = reservation.tasks.get(task_id)
            if task is not None:
                return reservation, task
        raise ValidationError(f"环节不存在：{task_id}")

    def _get(self, reservation_id: str) -> Reservation:
        try:
            return self._reservations[reservation_id]
        except KeyError:
            raise ValidationError(f"预约不存在：{reservation_id}") from None


def _provisional_id(request_id: str) -> str:
    return "R-provisional"


def replace_request(request: ServiceRequest, **changes) -> ServiceRequest:
    from dataclasses import replace

    return replace(request, **changes)
