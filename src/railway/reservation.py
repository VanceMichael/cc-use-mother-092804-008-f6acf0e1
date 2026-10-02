"""预约聚合：一次性记录需求，并按服务生成现场环节；改签/确认时做任务重排。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

from src.railway.errors import ConflictError, PolicyViolation, ValidationError
from src.railway.models import (
    ROLE_DUTY_OFFICER,
    ROLE_LUGGAGE_CLERK,
    ROLE_PET_CLERK,
    ROLE_STATION_ESCORT,
    ROLE_TRAIN_CONDUCTOR,
    HistoryEvent,
    Person,
    ServicePlan,
    ServiceRequest,
    ServiceStatus,
    ServiceTask,
    ServiceType,
    TaskKind,
    TaskStatus,
)
from src.railway.policies import (
    ServiceNetwork,
    check_eligibility,
    check_luggage,
    check_pet,
)

# 现场环节相对发到时刻的提前/延后量（分钟）。
ORIGIN_LEAD_MINUTES = 40
DEST_TAIL_MINUTES = 30
CONSIGN_LEAD_MINUTES = 60
CONSIGN_CUTOFF_MINUTES = 10
CLAIM_TAIL_MINUTES = 60


@dataclass
class PendingChange:
    request: ServiceRequest
    changed_fields: tuple[str, ...]
    received_at: object


@dataclass
class Reservation:
    """一次服务预约的聚合根，保存从提交到完成的全部责任事实。"""

    reservation_id: str
    request: ServiceRequest
    plans: dict[ServiceType, ServicePlan]
    tasks: dict[str, ServiceTask]
    task_order: list[str]
    handovers: list
    history: list[HistoryEvent]
    status: ServiceStatus = ServiceStatus.CONFIRMED
    revision: int = 1
    pending_change: PendingChange | None = None

    def person(self, person_id: str) -> Person:
        for person in self.request.persons:
            if person.person_id == person_id:
                return person
        raise ValidationError(f"预约中找不到旅客：{person_id}")

    def pet(self, pet_id: str):
        for pet in self.request.pets:
            if pet.pet_id == pet_id:
                return pet
        raise ValidationError(f"预约中找不到宠物：{pet_id}")

    def record(self, at, action: str, actor: str, detail: str = "", task_id: str | None = None) -> None:
        self.history.append(
            HistoryEvent(
                seq=len(self.history) + 1,
                at=at,
                action=action,
                actor=actor,
                detail=detail,
                task_id=task_id,
            )
        )

    def plan_tasks(self, service_type: ServiceType) -> list[ServiceTask]:
        """该服务当前计划中的环节，按链路顺序返回（已撤销的也保留以便追溯）。"""
        plan = self.plans[service_type]
        return [self.tasks[task_id] for task_id in plan.task_ids]

    def open_tasks(self) -> list[ServiceTask]:
        return [self.tasks[tid] for tid in self.task_order if self.tasks[tid].is_open]

    # -- 变更与重排 -----------------------------------------------------

    def propose_change(self, request: ServiceRequest, changed_fields: tuple[str, ...], at) -> None:
        if self.pending_change is not None:
            raise ConflictError("已有变更等待人工确认")
        self.pending_change = PendingChange(request, changed_fields, at)
        self.status = ServiceStatus.PENDING_REVIEW
        self.record(at, "变更待确认", "线上渠道", f"变化字段：{', '.join(changed_fields)}")

    def reject_change(self, by: str, at) -> None:
        if self.pending_change is None:
            raise ConflictError("没有待确认的变更")
        changed = self.pending_change.changed_fields
        self.pending_change = None
        self.record(at, "变更驳回", by, f"驳回字段：{', '.join(changed)}")
        self._recompute_status()

    def confirm_change(self, request: ServiceRequest, network: ServiceNetwork, by: str, at) -> None:
        self.pending_change = None
        self._apply_request(request, network, at, reason=f"人工确认变更（{by}）")

    def adjust_train(self, new_train_code: str, network: ServiceNetwork, by: str, at) -> None:
        """列车调整：沿用路线与需求，按新车次时刻重排尚未发生的环节。"""
        if self.pending_change is not None:
            raise ConflictError("存在待人工确认的变更，不能进行列车调整")
        new_request = replace(self.request, train_code=new_train_code)
        self._apply_request(new_request, network, at, reason=f"列车调整为 {new_train_code}（{by}）")

    def activate_eligibility(self, request: ServiceRequest, network: ServiceNetwork, by: str, at) -> None:
        """资格核验通过后补生成分段接力任务。"""
        self._apply_request(request, network, at, reason=f"资格核验通过（{by}）")

    def _apply_request(self, request: ServiceRequest, network: ServiceNetwork, at, *, reason: str) -> None:
        built = Planner(self.reservation_id, network).build(request)
        built_by_id = {spec.task_id: spec for spec in built}

        # 已完成环节按既成事实保留；同语义未完成环节就地更新时刻。
        for spec in built:
            old = self.tasks.get(spec.task_id)
            if old is None:
                self.tasks[spec.task_id] = spec
                self.task_order.append(spec.task_id)
                self.record(at, "环节新增", reason, f"{spec.kind.label}@{spec.station_code}", spec.task_id)
            elif old.status == TaskStatus.COMPLETED:
                continue
            else:
                old.window_start = spec.window_start
                old.window_end = spec.window_end
                old.segment_from = spec.segment_from
                old.segment_to = spec.segment_to
                old.item_ids = spec.item_ids
                old.detail = spec.detail
                # 派工信息保留：新车次时刻已更新，仍由原责任人继续负责。

        # 旧链路中消失、且尚未发生的环节撤销；已完成的保留为历史事实。
        for old_id in list(self.task_order):
            if old_id in built_by_id:
                continue
            old = self.tasks[old_id]
            if old.is_open:
                old.status = TaskStatus.CANCELLED
                old.cancelled_at = at
                old.cancel_reason = reason
                self.record(at, "环节撤销", reason, f"{old.kind.label}@{old.station_code}", old.task_id)

        self.request = request
        self.revision += 1
        plan = self.plans[request.service_type]
        # 计划清单按新链路顺序排列，已完成但不在新链路的环节追加在末尾留痕。
        ordered = list(built_by_id)
        for old_id in self.task_order:
            if old_id not in ordered and self.tasks[old_id].status == TaskStatus.COMPLETED:
                ordered.append(old_id)
        plan.task_ids = ordered
        self.record(at, "预约重排", reason, f"版本 {self.revision}")
        self._recompute_status()

    # -- 取消（只撤销尚未发生的环节）------------------------------------

    def cancel_service(self, service_type: ServiceType, at, reason: str) -> list[str]:
        if service_type not in self.plans:
            raise ValidationError("该预约不含此服务")
        if self.pending_change is not None:
            raise ConflictError("存在待人工确认的变更，请先处理后再取消")
        cancelled = self._cancel_tasks(self.plan_tasks(service_type), at, reason)
        if cancelled:
            self.plans[service_type].cancelled = True
            self.record(at, "服务取消", "旅客", f"{service_type.label}：{reason}")
        self._recompute_status()
        return cancelled

    def cancel_all(self, at, reason: str) -> list[str]:
        if self.pending_change is not None:
            raise ConflictError("存在待人工确认的变更，请先处理后再取消")
        cancelled = self._cancel_tasks([self.tasks[tid] for tid in self.task_order], at, reason)
        if cancelled:
            self.record(at, "预约取消", "旅客", reason)
        self._recompute_status()
        return cancelled

    def _cancel_tasks(self, tasks: list[ServiceTask], at, reason: str) -> list[str]:
        chain_index = {tid: i for i, tid in enumerate(self.task_order)}
        cancelled: list[str] = []
        for task in sorted(tasks, key=lambda t: chain_index[t.task_id]):
            if not task.is_open:
                continue
            self._ensure_not_stranding(task)
            task.status = TaskStatus.CANCELLED
            task.cancelled_at = at
            task.cancel_reason = reason
            cancelled.append(task.task_id)
            self.record(at, "环节撤销", "旅客", f"{task.kind.label}：{reason}", task.task_id)
        return cancelled

    def _ensure_not_stranding(self, task: ServiceTask) -> None:
        """托运物件前序已交接时，撤销后序会造成物件失控，必须拦截。"""
        if not task.item_ids:
            return
        index = self.task_order.index(task.task_id)
        for earlier_id in self.task_order[:index]:
            earlier = self.tasks[earlier_id]
            if earlier.item_ids == task.item_ids and earlier.status == TaskStatus.COMPLETED:
                raise ConflictError(
                    f"{task.kind.label}的前序交接已完成，不能撤销（{len(task.item_ids)} 件物件需继续交接）"
                )

    # -- 状态 ------------------------------------------------------------

    def _recompute_status(self) -> None:
        if self.pending_change is not None:
            self.status = ServiceStatus.PENDING_REVIEW
            return
        awaiting = self.request.service_type == ServiceType.KEY_PASSENGER
        target_id = self.request.target_passenger_id
        if awaiting and target_id is not None:
            target = next((p for p in self.request.persons if p.person_id == target_id), None)
            if target is not None and (target.eligibility is None or not target.eligibility.verified):
                self.status = ServiceStatus.AWAITING_ELIGIBILITY
                return
        all_tasks = list(self.tasks.values())
        if not all_tasks:
            self.status = ServiceStatus.CONFIRMED
            return
        completed = [t for t in all_tasks if t.status == TaskStatus.COMPLETED]
        open_ = [t for t in all_tasks if t.is_open]
        terminal = [t for t in all_tasks if t.status in (TaskStatus.CANCELLED, TaskStatus.EXPIRED)]
        if not completed:
            self.status = ServiceStatus.CANCELLED if terminal and not open_ else ServiceStatus.CONFIRMED
        elif not open_ and len(completed) + len(terminal) == len(all_tasks):
            self.status = (
                ServiceStatus.COMPLETED if not terminal else ServiceStatus.PARTIALLY_CANCELLED
            )
        else:
            self.status = ServiceStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# 任务规划
# ---------------------------------------------------------------------------


class Planner:
    """按服务类型把预约需求展开为沿车次停站的现场环节序列。

    任务 ID 采用语义稳定编码（服务+环节+站点/区段），改签重排时同一环节可对齐，
    已完成环节不会被重新生成，未完成环节就地更新时刻。
    """

    def __init__(self, reservation_id: str, network: ServiceNetwork) -> None:
        self.reservation_id = reservation_id
        self.network = network

    def build(self, request: ServiceRequest) -> list[ServiceTask]:
        builder = {
            ServiceType.KEY_PASSENGER: self._key_passenger,
            ServiceType.PET: self._pet,
            ServiceType.LUGGAGE: self._luggage,
            ServiceType.TEMP_CREDENTIAL: self._credential,
        }[request.service_type]
        return builder(request)

    def _tid(self, request: ServiceRequest, kind: TaskKind, station: str, seg: str = "") -> str:
        suffix = f"{kind.value}@{station}"
        if seg:
            suffix += f"#{seg}"
        return f"{self.reservation_id}:{request.service_type.value}:{suffix}"

    def _route_stops(self, request: ServiceRequest):
        if request.route is None:
            raise ValidationError("缺少乘降/运输区间")
        origin, destination = request.route
        train = self.network.train(request.train_code)
        self.network.ensure_route(train, origin, destination)
        return train, origin, destination, train.between(origin, destination)

    def _key_passenger(self, request: ServiceRequest) -> list[ServiceTask]:
        train, origin, destination, stops = self._route_stops(request)
        target_id = request.target_passenger_id
        if target_id is None:
            raise ValidationError("重点旅客预约缺少服务对象")
        target = next((p for p in request.persons if p.person_id == target_id), None)
        if target is None:
            raise ValidationError(f"服务对象不在同行人员中：{target_id}")
        check_eligibility(target)
        if not target.eligibility.verified:
            return []  # 资格核验通过后才生成分段接力任务。

        tasks: list[ServiceTask] = []
        first_depart = stops[0].depart_at
        last_arrive = stops[-1].arrive_at
        tasks.append(
            ServiceTask(
                task_id=self._tid(request, TaskKind.ORIGIN_ESCORT, origin),
                kind=TaskKind.ORIGIN_ESCORT,
                station_code=origin,
                role=ROLE_STATION_ESCORT,
                window_start=first_depart - timedelta(minutes=ORIGIN_LEAD_MINUTES),
                window_end=first_depart,
                detail=f"重点旅客 {target.name}（{target.eligibility.category.label}）进站送至站台",
            )
        )
        for left, right in zip(stops, stops[1:]):
            tasks.append(
                ServiceTask(
                    task_id=self._tid(request, TaskKind.RELAY_LEG, left.station_code, f"{left.station_code}>{right.station_code}"),
                    kind=TaskKind.RELAY_LEG,
                    station_code=left.station_code,
                    role=ROLE_TRAIN_CONDUCTOR,
                    window_start=left.depart_at,
                    window_end=right.arrive_at,
                    segment_from=left.station_code,
                    segment_to=right.station_code,
                    detail=f"车上照护 {target.name}：{left.station_code}->{right.station_code}",
                )
            )
        tasks.append(
            ServiceTask(
                task_id=self._tid(request, TaskKind.DEST_ESCORT, destination),
                kind=TaskKind.DEST_ESCORT,
                station_code=destination,
                role=ROLE_STATION_ESCORT,
                window_start=last_arrive,
                window_end=last_arrive + timedelta(minutes=DEST_TAIL_MINUTES),
                detail=f"重点旅客 {target.name} 站台送出站",
            )
        )
        return tasks

    def _pet(self, request: ServiceRequest) -> list[ServiceTask]:
        train, origin, destination, stops = self._route_stops(request)
        if request.pet_id is None:
            raise ValidationError("宠物托运缺少宠物资料")
        pet = next((p for p in request.pets if p.pet_id == request.pet_id), None)
        if pet is None:
            raise ValidationError(f"宠物资料不存在：{request.pet_id}")
        check_pet(pet)
        self.network.ensure_pet_stations(origin, destination)
        if pet.unaccompanied:
            receiver = next((p for p in request.persons if p.person_id == pet.receiver_id), None)
            if receiver is None:
                raise ValidationError("单独出行宠物的接收人不在同行人员资料中")
            receiver_text = f"，到站接收人 {receiver.name}"
        else:
            receiver_text = ""

        tasks: list[ServiceTask] = []
        first_depart = stops[0].depart_at
        last_arrive = stops[-1].arrive_at
        tasks.append(
            ServiceTask(
                task_id=self._tid(request, TaskKind.PET_CONSIGN, origin),
                kind=TaskKind.PET_CONSIGN,
                station_code=origin,
                role=ROLE_PET_CLERK,
                window_start=first_depart - timedelta(minutes=CONSIGN_LEAD_MINUTES),
                window_end=first_depart - timedelta(minutes=CONSIGN_CUTOFF_MINUTES),
                item_ids=(pet.pet_id,),
                detail=f"{pet.species}发运受理（笼箱 {pet.cage.length_cm}×{pet.cage.width_cm}×{pet.cage.height_cm}cm）{receiver_text}",
            )
        )
        for left, right in zip(stops, stops[1:]):
            tasks.append(
                ServiceTask(
                    task_id=self._tid(request, TaskKind.PET_TRANSIT, left.station_code, f"{left.station_code}>{right.station_code}"),
                    kind=TaskKind.PET_TRANSIT,
                    station_code=left.station_code,
                    role=ROLE_TRAIN_CONDUCTOR,
                    window_start=left.depart_at,
                    window_end=right.arrive_at,
                    segment_from=left.station_code,
                    segment_to=right.station_code,
                    item_ids=(pet.pet_id,),
                    detail=f"宠物押运 {left.station_code}->{right.station_code}{receiver_text}",
                )
            )
        tasks.append(
            ServiceTask(
                task_id=self._tid(request, TaskKind.PET_CLAIM, destination),
                kind=TaskKind.PET_CLAIM,
                station_code=destination,
                role=ROLE_PET_CLERK,
                window_start=last_arrive,
                window_end=last_arrive + timedelta(minutes=CLAIM_TAIL_MINUTES),
                item_ids=(pet.pet_id,),
                detail=f"宠物到达交付{receiver_text}",
            )
        )
        return tasks

    def _luggage(self, request: ServiceRequest) -> list[ServiceTask]:
        train, origin, destination, stops = self._route_stops(request)
        known = {item.item_id for item in request.items}
        if not request.item_ids:
            raise ValidationError("行李服务缺少托运件")
        unknown = [item_id for item_id in request.item_ids if item_id not in known]
        if unknown:
            raise ValidationError(f"行李件不存在：{', '.join(unknown)}")
        if len(set(request.item_ids)) != len(request.item_ids):
            raise ValidationError("行李件重复登记")
        check_luggage(len(request.item_ids), train, origin, destination)

        tasks: list[ServiceTask] = []
        first_depart = stops[0].depart_at
        last_arrive = stops[-1].arrive_at
        piece_ids = tuple(request.item_ids)
        tasks.append(
            ServiceTask(
                task_id=self._tid(request, TaskKind.LUGGAGE_PICKUP, origin),
                kind=TaskKind.LUGGAGE_PICKUP,
                station_code=origin,
                role=ROLE_LUGGAGE_CLERK,
                window_start=first_depart - timedelta(minutes=CONSIGN_LEAD_MINUTES),
                window_end=first_depart - timedelta(minutes=CONSIGN_CUTOFF_MINUTES),
                item_ids=piece_ids,
                detail=f"行李取件 {len(piece_ids)} 件",
            )
        )
        for left, right in zip(stops, stops[1:]):
            tasks.append(
                ServiceTask(
                    task_id=self._tid(request, TaskKind.LUGGAGE_TRANSIT, left.station_code, f"{left.station_code}>{right.station_code}"),
                    kind=TaskKind.LUGGAGE_TRANSIT,
                    station_code=left.station_code,
                    role=ROLE_TRAIN_CONDUCTOR,
                    window_start=left.depart_at,
                    window_end=right.arrive_at,
                    segment_from=left.station_code,
                    segment_to=right.station_code,
                    item_ids=piece_ids,
                    detail=f"行李运输 {len(piece_ids)} 件：{left.station_code}->{right.station_code}",
                )
            )
        tasks.append(
            ServiceTask(
                task_id=self._tid(request, TaskKind.LUGGAGE_DELIVER, destination),
                kind=TaskKind.LUGGAGE_DELIVER,
                station_code=destination,
                role=ROLE_LUGGAGE_CLERK,
                window_start=last_arrive,
                window_end=last_arrive + timedelta(minutes=CLAIM_TAIL_MINUTES),
                item_ids=piece_ids,
                detail=f"行李送达 {len(piece_ids)} 件",
            )
        )
        return tasks

    def _credential(self, request: ServiceRequest) -> list[ServiceTask]:
        credential = request.credential
        if credential is None:
            raise ValidationError("临时身份证明服务缺少证明资料")
        train = self.network.train(request.train_code)
        if not train.serves(credential.station_code):
            raise PolicyViolation(f"车次 {train.code} 不经停开具站 {credential.station_code}")
        holder = next((p for p in request.persons if p.person_id == credential.passenger_id), None)
        if holder is None:
            raise ValidationError("临时证明持证人不在预约人员中")
        stop = next(s for s in train.stops if s.station_code == credential.station_code)
        if stop.depart_at is None:
            raise ValidationError("终到站不办理临时身份证明")
        window_end = min(credential.valid_until, stop.depart_at)
        if credential.valid_from >= window_end:
            raise PolicyViolation("临时身份证明有效期无法覆盖开具时间")
        if credential.valid_until < stop.depart_at:
            raise PolicyViolation("临时身份证明有效期不能覆盖乘车时刻")
        return [
            ServiceTask(
                task_id=self._tid(request, TaskKind.CREDENTIAL_ISSUE, credential.station_code),
                kind=TaskKind.CREDENTIAL_ISSUE,
                station_code=credential.station_code,
                role=ROLE_DUTY_OFFICER,
                window_start=credential.valid_from,
                window_end=window_end,
                detail=f"为 {holder.name} 开具临时身份证明（有效期至 {credential.valid_until:%Y-%m-%d %H:%M}）",
            )
        ]
