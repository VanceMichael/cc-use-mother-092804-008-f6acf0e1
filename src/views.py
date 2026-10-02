"""面向值班人员与管理人员的只读视图。

值班视图遵循最小信息原则：只返回本人负责环节、服务必需的信息
（车站、时间、服务类型、对象摘要），不暴露其他同行人和无关服务。
管理视图则可还原一次服务从预约到完成的完整责任链。
"""

from __future__ import annotations

from typing import Optional

from .models import Booking, ServiceType, Task, TaskStatus


SERVICE_LABELS = {
    ServiceType.KEY_PASSENGER: "重点旅客陪护",
    ServiceType.PET: "宠物托运",
    ServiceType.LUGGAGE: "行李服务",
    ServiceType.TEMP_ID: "临时身份证明",
}

TASK_LABELS = {
    "pickup": "进站接送",
    "transfer": "分段交接",
    "boarding": "送上车",
    "onboard": "车上看护",
    "disembark": "下车接站",
    "delivery": "出站送达",
    "pet_dropoff": "宠物受理",
    "pet_pickup": "宠物到达交付",
    "luggage_pickup": "行李收件",
    "luggage_delivery": "行李送达",
    "id_verify": "证明核验出具",
    "id_handover": "证明现场交付",
}


def _dt(value) -> Optional[str]:
    return value.isoformat() if value else None


def _object_summary(booking: Booking, task: Task) -> list[str]:
    """值班人员交接时需要核对的最小对象清单。"""
    if task.service_type is ServiceType.LUGGAGE:
        need = booking.need_for(ServiceType.LUGGAGE)
        return [f"{item.item_id}({item.kind})" for item in need.items]
    if task.service_type is ServiceType.PET:
        need = booking.need_for(ServiceType.PET)
        return [f"{need.species}:{need.pet_name or '未命名'}"]
    if task.service_type is ServiceType.TEMP_ID:
        need = booking.need_for(ServiceType.TEMP_ID)
        if need.certificate_no:
            return [f"证明 {need.certificate_no}"]
        return ["临时身份证明（待出具）"]
    need = booking.need_for(ServiceType.KEY_PASSENGER)
    return [f"{need.category}" + (f"/{need.equipment}" if need.equipment else "")]


def staff_task_view(
    service,
    staff_id: str,
    *,
    station_code: Optional[str] = None,
    include_finished: bool = False,
) -> dict:
    """值班人员视角：只含本人被指派环节的最小信息。

    ``service`` 为 :class:`RailwayService`，以参数注入避免循环导入。
    """
    staff = service.catalog.staff_member(staff_id)
    if staff is None:
        raise KeyError(f"人员不存在：{staff_id}")

    rows = []
    for booking, task in service.repo.list_tasks_for_staff(staff_id):
        if station_code and task.station_code != station_code:
            continue
        if not include_finished and task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            continue
        # 值班人员只看到直接服务的旅客姓名，不看到同行名单
        passenger = booking.passenger(task_passenger_id(booking, task))
        rows.append(
            {
                "task_id": task.task_id,
                "booking_id": booking.booking_id,
                "service": SERVICE_LABELS[task.service_type],
                "action": TASK_LABELS[task.kind.value],
                "station": task.station_code,
                "train": task.train_code,
                "scheduled_start": _dt(task.scheduled_start),
                "scheduled_end": _dt(task.scheduled_end),
                "status": task.status.value,
                "passenger_name": passenger.name if passenger else None,
                "objects": _object_summary(booking, task),
            }
        )
    return {
        "staff_id": staff_id,
        "station": staff.station_code,
        "open_count": len(rows),
        "tasks": rows,
    }


def task_passenger_id(booking: Booking, task: Task) -> str:
    need = booking.need_for(task.service_type)
    return need.passenger_id if need else booking.primary_passenger().passenger_id


def station_dashboard(service, station_code: str) -> dict:
    """车站值班台视图：本站各服务的待办环节（不含旅客身份明细）。"""
    rows = []
    for booking in service.repo.list_bookings():
        for task in booking.tasks:
            if task.station_code != station_code or not task.is_open:
                continue
            rows.append(
                {
                    "task_id": task.task_id,
                    "booking_id": booking.booking_id,
                    "service": SERVICE_LABELS[task.service_type],
                    "action": TASK_LABELS[task.kind.value],
                    "scheduled_start": _dt(task.scheduled_start),
                    "assignee": task.assignee_id,
                    "train": task.train_code,
                    "status": task.status.value,
                }
            )
    return {"station": station_code, "open_tasks": sorted(rows, key=lambda r: r["scheduled_start"])}


def responsibility_chain(booking: Booking, service_type: Optional[ServiceType] = None) -> dict:
    """管理视图：按服务还原 预约 → 核验 → 环节 → 交接 → 完成 的责任链。"""
    services = [service_type] if service_type else list(ServiceType)
    chains = []
    for st in services:
        need = booking.need_for(st)
        if need is None:
            continue
        tasks = sorted(booking.tasks_of(st), key=lambda t: t.seq)
        handovers = {h.task_id_from: h for h in booking.handovers if _handover_belongs(tasks, h)}
        chain = {
            "service": SERVICE_LABELS[st],
            "cancelled": need.cancelled,
            "eligibility": need.eligibility.value
            if st is ServiceType.KEY_PASSENGER
            else "not_required",
            "segments": [],
        }
        for task in tasks:
            row = {
                "seq": task.seq,
                "task_id": task.task_id,
                "action": TASK_LABELS[task.kind.value],
                "station": task.station_code,
                "train": task.train_code,
                "scheduled_start": _dt(task.scheduled_start),
                "scheduled_end": _dt(task.scheduled_end),
                "assignee": task.assignee_id,
                "status": task.status.value,
                "completed_at": _dt(task.completed_at),
                "completed_by": task.completed_by,
            }
            handover = handovers.get(task.task_id)
            if handover is not None:
                row["handover"] = {
                    "handover_id": handover.handover_id,
                    "at": _dt(handover.at),
                    "to_task": handover.task_id_to,
                    "to_staff": handover.to_staff_id,
                    "objects": list(handover.objects),
                    "summary": handover.summary,
                }
            chain["segments"].append(row)
        if st is ServiceType.TEMP_ID:
            chain["certificate"] = {
                "certificate_no": need.certificate_no,
                "issued_at": _dt(need.issued_at),
                "expires_at": _dt(need.expires_at),
            }
        chains.append(chain)

    return {
        "booking_id": booking.booking_id,
        "version": booking.version,
        "status": booking.status.value,
        "train": booking.itinerary.train_code,
        "route": [booking.itinerary.board_station, booking.itinerary.alight_station],
        "chains": chains,
        "timeline": [
            {
                "at": _dt(event.at),
                "event": event.event_type,
                "actor": event.actor_id,
                "detail": event.detail,
            }
            for event in booking.events
        ],
        "reviews": [
            {
                "review_id": r.review_id,
                "changed_fields": list(r.changed_fields),
                "resolution": r.resolution or "pending",
                "resolved_at": _dt(r.resolved_at),
                "note": r.note,
            }
            for r in booking.reviews
        ],
    }


def _handover_belongs(tasks: list[Task], handover) -> bool:
    ids = {task.task_id for task in tasks}
    return handover.task_id_from in ids and handover.task_id_to in ids
