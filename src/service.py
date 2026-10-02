"""应用服务层：预约受理、资格核验、现场交接、取消与改签。

所有写操作都在仓储锁内完成，保证并发的重复请求不会被受理两次。
任务一旦完成即不可变；取消和改签只回收尚未发生的环节。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from . import builder, planning
from .catalog import ServiceCatalog
from .models import (
    Booking,
    BookingStatus,
    DomainEvent,
    EligibilityStatus,
    Handover,
    ReviewCase,
    ServiceType,
    Task,
    TaskKind,
    TaskStatus,
)
from .repository import BookingRepository


class ServiceError(ValueError):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass
class SubmitResult:
    booking: Booking
    duplicate: bool = False
    review: Optional[ReviewCase] = None


class RailwayService:
    def __init__(self, catalog: ServiceCatalog, repo: Optional[BookingRepository] = None):
        self.catalog = catalog
        self.repo = repo or BookingRepository()

    # --- 事件 -------------------------------------------------------------

    @staticmethod
    def _record(booking: Booking, event_type: str, at: datetime, actor: Optional[str], **detail) -> None:
        booking.events.append(DomainEvent(at=at, event_type=event_type, detail=detail, actor_id=actor))

    # --- 指派 -------------------------------------------------------------

    def _assign(self, station_code: str, service_type: ServiceType) -> Optional[str]:
        """按当前在手任务量，把环节派给本站可承接该服务的最空闲人员。"""
        candidates = [
            member
            for member in self.catalog.staff_at(station_code, service_type)
            if member.role != "supervisor"
        ]
        if not candidates:
            return None
        load = {member.staff_id: 0 for member in candidates}
        for booking in self.repo.list_bookings():
            for task in booking.tasks:
                if task.is_open and task.assignee_id in load:
                    load[task.assignee_id] += 1
        return min(load, key=lambda sid: (load[sid], sid))

    def _create_tasks(
        self,
        booking: Booking,
        service_type: ServiceType,
        steps: list[planning.PlannedStep],
    ) -> list[Task]:
        tasks: list[Task] = []
        for seq, step in enumerate(steps):
            assignee = self._assign(step.station_code, service_type)
            tasks.append(
                Task(
                    task_id=self.repo.next_task_id(),
                    service_type=service_type,
                    kind=step.kind,
                    station_code=step.station_code,
                    scheduled_start=step.start,
                    scheduled_end=step.end,
                    seq=seq,
                    assignee_id=assignee,
                    train_code=step.train_code,
                    note="待指派" if assignee is None else "",
                )
            )
        booking.tasks.extend(tasks)
        return tasks

    def _planned_steps(self, booking: Booking, service_type: ServiceType) -> list[planning.PlannedStep]:
        need = booking.need_for(service_type)
        assert need is not None
        if service_type is ServiceType.KEY_PASSENGER:
            return planning.plan_key_passenger(self.catalog, need, booking.itinerary)
        if service_type is ServiceType.PET:
            return planning.plan_pet(self.catalog, need, booking.itinerary)
        if service_type is ServiceType.LUGGAGE:
            return planning.plan_luggage(self.catalog, need, booking.itinerary)
        return planning.plan_temp_id(self.catalog, need)

    # --- 提交预约 ----------------------------------------------------------

    def submit_booking(self, raw: dict, now: datetime) -> SubmitResult:
        with self.repo.lock:
            key = raw.get("idempotency_key")
            if not isinstance(key, str) or not key:
                raise builder.RequestError("missing_field", "缺少幂等键 idempotency_key")

            existing = self.repo.find_by_idempotency(key)
            if existing is not None:
                return self._handle_duplicate(existing, raw, now)

            passengers = builder.build_passengers(raw)
            itinerary = planning.resolve_itinerary(
                self.catalog,
                builder._require(raw, "train_code", "车次", str),
                builder._require(raw, "board_station", "乘车站", str),
                builder._require(raw, "alight_station", "到达站", str),
            )
            needs = builder.build_needs(raw, {p.passenger_id for p in passengers})
            # 宠物/行李单独车次需要在此解析并回填停靠时刻
            for need in needs:
                if getattr(need, "itinerary", None) is not None and not need.itinerary.stops:
                    need.itinerary = planning.resolve_itinerary(
                        self.catalog,
                        need.itinerary.train_code,
                        need.itinerary.board_station,
                        need.itinerary.alight_station,
                    )

            booking = Booking(
                booking_id=self.repo.next_booking_id(),
                idempotency_key=key,
                passengers=passengers,
                itinerary=itinerary,
                needs=needs,
                created_at=now,
                updated_at=now,
                request_snapshot=raw,
                key_fingerprint=builder.fingerprint(raw),
            )
            # 全量规则校验 + 为无需资格闸门的服务直接生成任务
            generated: dict[str, int] = {}
            for service_type in (ServiceType.PET, ServiceType.LUGGAGE, ServiceType.TEMP_ID):
                if booking.need_for(service_type) is not None:
                    steps = self._planned_steps(booking, service_type)
                    self._create_tasks(booking, service_type, steps)
                    generated[service_type.value] = len(steps)
            # 重点旅客：先校验可规划性，任务待资格核验后生成
            key_need = booking.need_for(ServiceType.KEY_PASSENGER)
            if key_need is not None:
                self._planned_steps(booking, ServiceType.KEY_PASSENGER)

            self.repo.save(booking)
            self._record(
                booking,
                "booking_submitted",
                now,
                raw.get("operator_id"),
                needs=[n.service_type.value for n in needs],
                tasks_generated=generated,
                awaits_eligibility=key_need is not None,
            )
            return SubmitResult(booking=booking)

    def _handle_duplicate(self, booking: Booking, raw: dict, now: datetime) -> SubmitResult:
        fingerprint = builder.fingerprint(raw)
        if fingerprint == booking.key_fingerprint:
            # 完全重复：返回同一预约，不产生任何新任务
            return SubmitResult(booking=booking, duplicate=True)

        open_review = next(
            (case for case in booking.reviews if not case.resolution), None
        )
        changes = builder.changed_fields(booking.request_snapshot, raw)
        if open_review is not None:
            # 等待确认期间再次变化：刷新为最新请求，仍只有一张复核单
            open_review.after = {"changed_fields": changes}
            open_review.new_raw = raw
            open_review.fingerprint = fingerprint
            self._record(booking, "review_updated", now, raw.get("operator_id"), changed_fields=changes)
            return SubmitResult(booking=booking, review=open_review)

        review = ReviewCase(
            review_id=self.repo.next_review_id(),
            booking_id=booking.booking_id,
            idempotency_key=booking.idempotency_key,
            changed_fields=tuple(changes),
            before={"fingerprint": booking.key_fingerprint},
            after={"changed_fields": changes},
            created_at=now,
            new_raw=raw,
            fingerprint=fingerprint,
        )
        booking.reviews.append(review)
        booking.status = BookingStatus.PENDING_REVIEW
        self.repo.save_review(review)
        self._record(booking, "review_opened", now, raw.get("operator_id"), changed_fields=changes)
        return SubmitResult(booking=booking, review=review)

    # --- 人工复核 ----------------------------------------------------------

    def resolve_review(
        self,
        review_id: str,
        decision: str,
        actor_id: str,
        now: datetime,
        note: str = "",
    ) -> ReviewCase:
        with self.repo.lock:
            review = self.repo.get_review(review_id)
            if review is None:
                raise ServiceError("review_not_found", "复核单不存在", 404)
            if review.resolution:
                raise ServiceError("review_closed", "复核单已处理")
            booking = self.repo.get(review.booking_id)
            assert booking is not None
            if decision not in ("accepted", "rejected"):
                raise ServiceError("bad_decision", "决定必须是 accepted 或 rejected")

            review.resolution = decision
            review.resolved_at = now
            review.note = note
            if decision == "accepted":
                self._rebuild(booking, review.new_raw, actor_id, now, reason="manual_review_accepted")
                self._record(
                    booking, "review_accepted", now, actor_id, changed_fields=list(review.changed_fields)
                )
            else:
                booking.status = BookingStatus.CONFIRMED
                self._record(booking, "review_rejected", now, actor_id)
            return review

    # --- 改签 / 列车调整 ----------------------------------------------------

    def adjust_train(
        self,
        booking_id: str,
        actor_id: str,
        now: datetime,
        *,
        main: Optional[dict] = None,
        pet: Optional[dict] = None,
        luggage: Optional[dict] = None,
    ) -> Booking:
        """车站侧列车调整：直接改线重排，只回收尚未发生的环节。"""
        with self.repo.lock:
            booking = self._require_booking(booking_id)
            self._require_editable(booking)
            raw = dict(booking.request_snapshot)
            if main:
                for field_name in ("train_code", "board_station", "alight_station"):
                    if field_name in main:
                        raw[field_name] = main[field_name]
            new_needs = []
            for need_raw in raw.get("needs", []):
                need_copy = dict(need_raw)
                # 重点旅客的接送站必须与主行程一致，随主行程同步调整
                if need_copy.get("service_type") == ServiceType.KEY_PASSENGER.value and main:
                    if main.get("board_station"):
                        need_copy["boarding_station"] = main["board_station"]
                    if main.get("alight_station"):
                        need_copy["alight_station"] = main["alight_station"]
                if need_copy.get("service_type") == ServiceType.PET.value and pet:
                    need_copy["separate_itinerary"] = {**need_copy.get("separate_itinerary", {}), **pet}
                if need_copy.get("service_type") == ServiceType.LUGGAGE.value and luggage:
                    need_copy["separate_itinerary"] = {**need_copy.get("separate_itinerary", {}), **luggage}
                new_needs.append(need_copy)
            raw["needs"] = new_needs
            self._rebuild(booking, raw, actor_id, now, reason="train_adjusted")
            self._record(
                booking,
                "train_adjusted",
                now,
                actor_id,
                main=main,
                pet=pet,
                luggage=luggage,
            )
            return booking

    def _rebuild(self, booking: Booking, raw: dict, actor_id: str, now: datetime, *, reason: str) -> None:
        passengers = builder.build_passengers(raw)
        itinerary = planning.resolve_itinerary(
            self.catalog,
            builder._require(raw, "train_code", "车次", str),
            builder._require(raw, "board_station", "乘车站", str),
            builder._require(raw, "alight_station", "到达站", str),
        )
        needs = builder.build_needs(raw, {p.passenger_id for p in passengers})
        for need in needs:
            if getattr(need, "itinerary", None) is not None and not need.itinerary.stops:
                need.itinerary = planning.resolve_itinerary(
                    self.catalog,
                    need.itinerary.train_code,
                    need.itinerary.board_station,
                    need.itinerary.alight_station,
                )

        # 已完成环节是事实：保留其资格状态与已出具的临时证明
        old_needs = {n.service_type: n for n in booking.needs}
        for need in needs:
            old = old_needs.get(need.service_type)
            if old is not None:
                need.eligibility = old.eligibility
                need.eligibility_note = old.eligibility_note
                need.cancelled = old.cancelled
                for attr in ("issued_at", "expires_at", "certificate_no"):
                    if hasattr(old, attr) and getattr(old, attr):
                        setattr(need, attr, getattr(old, attr))
        # 重点旅客类别变化需要重新核验资格
        old_key = old_needs.get(ServiceType.KEY_PASSENGER)
        new_key = next((n for n in needs if n.service_type is ServiceType.KEY_PASSENGER), None)
        if (
            old_key is not None
            and new_key is not None
            and old_key.category != new_key.category
            and old_key.eligibility is EligibilityStatus.VERIFIED
        ):
            new_key.eligibility = EligibilityStatus.PENDING
            new_key.eligibility_note = "类别变更，原核验结论失效"

        # 先在新方案上完成全部规则校验，任一失败则预约保持原状
        planned: dict[ServiceType, list[planning.PlannedStep]] = {}
        for service_type in (ServiceType.PET, ServiceType.LUGGAGE, ServiceType.TEMP_ID):
            new_need = next((n for n in needs if n.service_type is service_type), None)
            if new_need is None or new_need.cancelled:
                continue
            # 临时证明已出具即不可重排，证件在有效期内继续有效
            if service_type is ServiceType.TEMP_ID and new_need.issued_at is not None:
                continue
            planned[service_type] = self._plan_steps(self.catalog, new_need, itinerary, service_type)
        if new_key is not None and not new_key.cancelled:
            if new_key.eligibility is EligibilityStatus.VERIFIED:
                planned[ServiceType.KEY_PASSENGER] = planning.plan_key_passenger(
                    self.catalog, new_key, itinerary
                )
            else:
                # 待核验：只撤销尚未发生的旧接力环节，已完成的保留
                planned[ServiceType.KEY_PASSENGER] = []

        # 校验全部通过后才落账
        booking.passengers = passengers
        booking.itinerary = itinerary
        booking.needs = needs
        booking.request_snapshot = raw
        booking.key_fingerprint = builder.fingerprint(raw)
        booking.status = BookingStatus.CONFIRMED
        booking.updated_at = now
        booking.version += 1

        for service_type, steps in planned.items():
            self._merge_plan(booking, service_type, steps, now, reason)

        self.repo.save(booking)

    @staticmethod
    def _plan_steps(catalog, need, itinerary, service_type: ServiceType) -> list[planning.PlannedStep]:
        """对"尚未写入预约"的需求做规划，保证改签校验失败时不污染预约。"""
        if service_type is ServiceType.PET:
            return planning.plan_pet(catalog, need, itinerary)
        if service_type is ServiceType.LUGGAGE:
            return planning.plan_luggage(catalog, need, itinerary)
        return planning.plan_temp_id(catalog, need)

    def _merge_plan(
        self,
        booking: Booking,
        service_type: ServiceType,
        steps: list[planning.PlannedStep],
        now: datetime,
        reason: str,
    ) -> None:
        """把新方案并入预约：完成的不动，开放环节按（类型, 站点）改期或撤销。"""
        existing = booking.tasks_of(service_type)
        open_tasks = [task for task in existing if task.is_open]
        # 已完成的（类型, 站点）视为已履行，新方案不再重建该段
        finished = {(task.kind, task.station_code) for task in existing if not task.is_open}
        reused: set[str] = set()

        for seq, step in enumerate(steps):
            if (step.kind, step.station_code) in finished:
                continue  # 该段已完成交接，保留事实不重建
            candidate = next(
                (
                    task
                    for task in open_tasks
                    if task.task_id not in reused
                    and task.kind is step.kind
                    and task.station_code == step.station_code
                ),
                None,
            )
            if candidate is not None:
                reused.add(candidate.task_id)
                candidate.seq = seq
                candidate.scheduled_start = step.start
                candidate.scheduled_end = step.end
                candidate.train_code = step.train_code
                staff = self.catalog.staff_member(candidate.assignee_id or "")
                if (
                    staff is None
                    or staff.station_code != step.station_code
                    or service_type not in staff.services
                ):
                    candidate.assignee_id = self._assign(step.station_code, service_type)
                    candidate.status = TaskStatus.PLANNED
                    candidate.note = "改线后重新指派" if candidate.assignee_id else "待指派"
            else:
                self._create_tasks(booking, service_type, [step])
                booking.tasks[-1].seq = seq

        for task in open_tasks:
            if task.task_id not in reused:
                task.status = TaskStatus.CANCELLED
                task.note = f"方案调整撤销（{reason}）"

    # --- 资格核验 ----------------------------------------------------------

    def verify_eligibility(
        self,
        booking_id: str,
        service_type: ServiceType,
        approved: bool,
        actor_id: str,
        now: datetime,
        note: str = "",
    ) -> Booking:
        with self.repo.lock:
            booking = self._require_booking(booking_id)
            self._require_editable(booking)
            need = booking.need_for(service_type)
            if need is None:
                raise ServiceError("need_not_found", "该预约不含此项服务", 404)
            if need.cancelled:
                raise ServiceError("need_cancelled", "该服务已取消")
            if service_type is not ServiceType.KEY_PASSENGER:
                raise ServiceError("eligibility_not_required", "该服务不设置资格核验闸门")
            if need.eligibility is EligibilityStatus.VERIFIED:
                raise ServiceError("already_verified", "资格已核验通过")

            if approved:
                need.eligibility = EligibilityStatus.VERIFIED
                need.eligibility_note = note or "资格核验通过"
                steps = self._planned_steps(booking, service_type)
                self._merge_plan(booking, service_type, steps, now, "eligibility_verified")
                self._record(
                    booking, "eligibility_verified", now, actor_id, note=note, segments=len(steps)
                )
            else:
                need.eligibility = EligibilityStatus.REJECTED
                need.eligibility_note = note or "资格核验不通过"
                self._merge_plan(booking, service_type, [], now, "eligibility_rejected")
                self._record(booking, "eligibility_rejected", now, actor_id, note=note)
            self.repo.save(booking)
            return booking

    # --- 现场执行与交接 ----------------------------------------------------

    def _task(self, booking: Booking, task_id: str) -> Task:
        for task in booking.tasks:
            if task.task_id == task_id:
                return task
        raise ServiceError("task_not_found", "环节不存在", 404)

    @staticmethod
    def _require_open(task: Task) -> None:
        if task.status is TaskStatus.COMPLETED:
            raise ServiceError("task_completed", "环节已完成，不可变更")
        if task.status is TaskStatus.CANCELLED:
            raise ServiceError("task_cancelled", "环节已撤销")

    def start_task(self, booking_id: str, task_id: str, staff_id: str, now: datetime) -> Task:
        with self.repo.lock:
            booking = self._require_booking(booking_id)
            task = self._task(booking, task_id)
            self._require_open(task)
            if task.assignee_id != staff_id:
                raise ServiceError("not_assignee", "只有被指派人员可以开始该环节", 403)
            task.status = TaskStatus.IN_PROGRESS
            self._record(booking, "task_started", now, staff_id, task_id=task_id)
            self.repo.save(booking)
            return task

    def _next_task(self, booking: Booking, task: Task) -> Optional[Task]:
        return next(
            (
                other
                for other in booking.tasks
                if other.service_type is task.service_type
                and other.seq == task.seq + 1
                and other.status is not TaskStatus.CANCELLED
            ),
            None,
        )

    def perform_handover(
        self,
        booking_id: str,
        from_task_id: str,
        staff_id: str,
        now: datetime,
        objects: list[str],
        summary: str = "",
    ) -> Handover:
        """完成当前环节并把对象交给下一段负责人；件数/标识在此核对。"""
        with self.repo.lock:
            booking = self._require_booking(booking_id)
            task = self._task(booking, from_task_id)
            self._require_open(task)
            if task.assignee_id != staff_id:
                raise ServiceError("not_assignee", "只有当前环节负责人可以交接", 403)
            nxt = self._next_task(booking, task)
            if nxt is None:
                raise ServiceError("last_segment", "末段环节请使用完成操作")
            if not nxt.assignee_id:
                raise ServiceError("next_unassigned", "下一段尚未指派，无法交接")

            checked_objects = self._check_handover_objects(booking, task, objects)

            task.status = TaskStatus.COMPLETED
            task.completed_at = now
            task.completed_by = staff_id
            nxt.status = TaskStatus.IN_PROGRESS
            handover = Handover(
                handover_id=self.repo.next_handover_id(),
                task_id_from=task.task_id,
                task_id_to=nxt.task_id,
                station_code=nxt.station_code,
                at=now,
                from_staff_id=staff_id,
                to_staff_id=nxt.assignee_id,
                summary=summary,
                objects=tuple(checked_objects),
            )
            booking.handovers.append(handover)

            if task.service_type is ServiceType.TEMP_ID:
                need = booking.need_for(ServiceType.TEMP_ID)
                if need.issued_at is None:
                    need.issued_at = now
                    need.expires_at = now + timedelta(hours=need.validity_hours)
                    need.certificate_no = f"Z{booking.booking_id[1:]}-{self.repo.next_handover_id()}"
                    handover.objects = tuple(list(handover.objects) + [need.certificate_no])
                    self._record(
                        booking,
                        "certificate_issued",
                        now,
                        staff_id,
                        certificate_no=need.certificate_no,
                        expires_at=need.expires_at.isoformat(),
                    )

            self._record(
                booking,
                "handover",
                now,
                staff_id,
                from_task_id=task.task_id,
                to_task_id=nxt.task_id,
                objects=list(handover.objects),
            )
            self.repo.save(booking)
            return handover

    def _check_handover_objects(self, booking: Booking, task: Task, objects: list[str]) -> list[str]:
        if not objects or any(not isinstance(item, str) or not item.strip() for item in objects):
            raise ServiceError("objects_required", "交接必须列明对象清单")
        if task.service_type is ServiceType.LUGGAGE:
            need = booking.need_for(ServiceType.LUGGAGE)
            expected = {item.item_id for item in need.items}
            if set(objects) != expected or len(objects) != len(expected):
                raise ServiceError(
                    "piece_count_mismatch",
                    f"交接行李件数/标识与受理不一致：应交接 {sorted(expected)}",
                )
        return list(objects)

    def complete_task(
        self,
        booking_id: str,
        task_id: str,
        staff_id: str,
        now: datetime,
        note: str = "",
    ) -> Task:
        """完成末段（送达/交付/证明递交）。"""
        with self.repo.lock:
            booking = self._require_booking(booking_id)
            task = self._task(booking, task_id)
            self._require_open(task)
            if task.assignee_id != staff_id:
                raise ServiceError("not_assignee", "只有被指派人员可以完成该环节", 403)
            if self._next_task(booking, task) is not None:
                raise ServiceError("has_next_segment", "后续环节存在，必须先交接")

            if task.service_type is ServiceType.TEMP_ID and task.kind is TaskKind.ID_HANDOVER:
                need = booking.need_for(ServiceType.TEMP_ID)
                if need.issued_at is None:
                    raise ServiceError("not_issued", "证明尚未出具")
                if now > need.expires_at:
                    raise ServiceError("certificate_expired", "临时身份证明已过有效期，不能交付")

            task.status = TaskStatus.COMPLETED
            task.completed_at = now
            task.completed_by = staff_id
            task.note = note or task.note
            self._record(booking, "task_completed", now, staff_id, task_id=task_id)
            if all(not t.is_open for t in booking.tasks_of(task.service_type)):
                self._record(booking, "service_completed", now, staff_id, service=task.service_type.value)
            self.repo.save(booking)
            return task

    # --- 取消 --------------------------------------------------------------

    def cancel(
        self,
        booking_id: str,
        actor_id: str,
        now: datetime,
        reason: str,
        service_type: Optional[ServiceType] = None,
    ) -> Booking:
        with self.repo.lock:
            booking = self._require_booking(booking_id)
            self._require_editable(booking)
            targets = [service_type] if service_type else list(ServiceType)
            cancelled_any = False
            for target in targets:
                need = booking.need_for(target)
                if need is None or need.cancelled:
                    continue
                need.cancelled = True
                open_count = 0
                for task in booking.tasks_of(target):
                    if task.is_open:
                        task.status = TaskStatus.CANCELLED
                        task.note = f"取消撤销：{reason}"
                        open_count += 1
                cancelled_any = True
                self._record(
                    booking,
                    "service_cancelled",
                    now,
                    actor_id,
                    service=target.value,
                    open_segments_cancelled=open_count,
                    reason=reason,
                )
            if not cancelled_any:
                raise ServiceError("nothing_to_cancel", "没有可取消的有效服务")
            if all(booking.need_for(st) is None or booking.need_for(st).cancelled for st in ServiceType):
                booking.status = BookingStatus.CANCELLED
                booking.cancelled_at = now
                booking.cancel_reason = reason
                self._record(booking, "booking_cancelled", now, actor_id, reason=reason)
            self.repo.save(booking)
            return booking

    # --- 临时证明查验 ------------------------------------------------------

    def verify_certificate(self, certificate_no: str, at: datetime) -> dict:
        with self.repo.lock:
            for booking in self.repo.list_bookings():
                need = booking.need_for(ServiceType.TEMP_ID)
                if need is not None and need.certificate_no == certificate_no:
                    return {
                        "valid": need.valid_at(at),
                        "certificate_no": certificate_no,
                        "issued_at": need.issued_at.isoformat() if need.issued_at else None,
                        "expires_at": need.expires_at.isoformat() if need.expires_at else None,
                        "station": need.issue_station,
                    }
            raise ServiceError("certificate_not_found", "查无此临时身份证明", 404)

    # --- 基础校验 ----------------------------------------------------------

    def _require_booking(self, booking_id: str) -> Booking:
        booking = self.repo.get(booking_id)
        if booking is None:
            raise ServiceError("booking_not_found", "预约不存在", 404)
        return booking

    @staticmethod
    def _require_editable(booking: Booking) -> None:
        if booking.status is BookingStatus.CANCELLED:
            raise ServiceError("booking_cancelled", "预约已整单取消")
        if booking.status is BookingStatus.PENDING_REVIEW:
            raise ServiceError("pending_review", "关键字段变化待人工确认，请先处理复核单")
