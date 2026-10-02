"""视图：值班人员的最小执行信息，与管理者的责任链还原。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.railway.models import (
    ROLE_LABELS,
    HandoverRecord,
    HistoryEvent,
    ServiceStatus,
    ServiceTask,
    ServiceType,
    TaskStatus,
)


@dataclass(frozen=True)
class StaffTaskView:
    """值班人员只看到与本岗位本环节有关的最小信息：

    不含其他旅客资料、其他服务、资格依据与完整历史；
    仅保留执行所必需的站点、时间窗、动作说明与交接件数。
    """

    reservation_id: str
    task_id: str
    service_label: str
    kind_label: str
    station_name: str
    window_start: datetime
    window_end: datetime
    status_label: str
    piece_count: int
    instruction: str
    assignee_name: str | None


@dataclass(frozen=True)
class ChainStep:
    task_id: str
    kind_label: str
    station_name: str
    role_label: str
    window_start: datetime
    window_end: datetime
    status_label: str
    assignee_name: str | None
    handover: HandoverRecord | None


@dataclass(frozen=True)
class ChainEvent:
    seq: int
    at: datetime
    action: str
    actor: str
    detail: str
    task_id: str | None


@dataclass(frozen=True)
class ReservationChain:
    """管理者视角：一次服务从预约到完成的完整责任链。"""

    reservation_id: str
    service_label: str
    status_label: str
    revision: int
    steps: tuple[ChainStep, ...]
    events: tuple[ChainEvent, ...]


def staff_task_views(pairs, network, staff_index) -> list[StaffTaskView]:
    views: list[StaffTaskView] = []
    for reservation, task in pairs:
        station = network.station(task.station_code)
        assignee = staff_index.get(task.assignee_id) if task.assignee_id else None
        views.append(
            StaffTaskView(
                reservation_id=reservation.reservation_id,
                task_id=task.task_id,
                service_label=reservation.request.service_type.label,
                kind_label=task.kind.label,
                station_name=station.name,
                window_start=task.window_start,
                window_end=task.window_end,
                status_label=task.status.label,
                piece_count=len(task.item_ids) if task.item_ids else 1,
                instruction=task.detail,
                assignee_name=assignee.name if assignee else None,
            )
        )
    views.sort(key=lambda v: (v.window_start, v.reservation_id))
    return views


def build_chain(reservation, staff_index, network) -> ReservationChain:
    handover_by_task: dict[str, HandoverRecord] = {}
    for handover in reservation.handovers:
        handover_by_task[handover.task_id] = handover

    steps: list[ChainStep] = []
    ordered_ids = sorted(
        reservation.task_order,
        key=lambda tid: (reservation.tasks[tid].window_start, tid),
    )
    for task_id in ordered_ids:
        task: ServiceTask = reservation.tasks[task_id]
        assignee = staff_index.get(task.assignee_id) if task.assignee_id else None
        steps.append(
            ChainStep(
                task_id=task.task_id,
                kind_label=task.kind.label,
                station_name=network.station(task.station_code).name,
                role_label=ROLE_LABELS.get(task.role, task.role),
                window_start=task.window_start,
                window_end=task.window_end,
                status_label=task.status.label,
                assignee_name=assignee.name if assignee else None,
                handover=handover_by_task.get(task_id),
            )
        )

    events = tuple(
        ChainEvent(
            seq=event.seq,
            at=event.at,
            action=event.action,
            actor=event.actor,
            detail=event.detail,
            task_id=event.task_id,
        )
        for event in reservation.history
    )
    return ReservationChain(
        reservation_id=reservation.reservation_id,
        service_label=reservation.request.service_type.label,
        status_label=reservation.status.label,
        revision=reservation.revision,
        steps=tuple(steps),
        events=events,
    )
