"""预约与复核单的存储。

默认进程内字典实现（带锁，可被 ``api`` 的多线程 HTTP 服务直接使用），
接口刻意保持简单，便于后续替换为数据库实现。
"""

from __future__ import annotations

import threading
from typing import Optional

from .models import Booking, ReviewCase


class BookingRepository:
    def __init__(self) -> None:
        self._bookings: dict[str, Booking] = {}
        self._by_idempotency: dict[str, str] = {}
        self._reviews: dict[str, ReviewCase] = {}
        self._lock = threading.RLock()
        self._booking_seq = 0
        self._task_seq = 0
        self._handover_seq = 0
        self._review_seq = 0

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def next_booking_id(self) -> str:
        with self._lock:
            self._booking_seq += 1
            return f"B{self._booking_seq:06d}"

    def next_task_id(self) -> str:
        with self._lock:
            self._task_seq += 1
            return f"T{self._task_seq:06d}"

    def next_handover_id(self) -> str:
        with self._lock:
            self._handover_seq += 1
            return f"H{self._handover_seq:06d}"

    def next_review_id(self) -> str:
        with self._lock:
            self._review_seq += 1
            return f"R{self._review_seq:06d}"

    def save(self, booking: Booking) -> None:
        with self._lock:
            self._bookings[booking.booking_id] = booking
            self._by_idempotency[booking.idempotency_key] = booking.booking_id

    def get(self, booking_id: str) -> Optional[Booking]:
        with self._lock:
            return self._bookings.get(booking_id)

    def find_by_idempotency(self, key: str) -> Optional[Booking]:
        with self._lock:
            booking_id = self._by_idempotency.get(key)
            return self._bookings.get(booking_id) if booking_id else None

    def list_bookings(self) -> list[Booking]:
        with self._lock:
            return sorted(self._bookings.values(), key=lambda b: b.created_at)

    def save_review(self, review: ReviewCase) -> None:
        with self._lock:
            self._reviews[review.review_id] = review

    def get_review(self, review_id: str) -> Optional[ReviewCase]:
        with self._lock:
            return self._reviews.get(review_id)

    def list_reviews(self, include_resolved: bool = False) -> list[ReviewCase]:
        with self._lock:
            cases = sorted(self._reviews.values(), key=lambda r: r.created_at)
            if include_resolved:
                return cases
            return [case for case in cases if not case.resolution]

    def list_tasks_for_staff(self, staff_id: str) -> list[tuple[Booking, "object"]]:
        """返回指派给某人的（预约, 任务），按计划开始时间排序。"""
        with self._lock:
            result = []
            for booking in self._bookings.values():
                for task in booking.tasks:
                    if task.assignee_id == staff_id:
                        result.append((booking, task))
            return sorted(result, key=lambda pair: pair[1].scheduled_start)
