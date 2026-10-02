"""基于标准库 ``http.server`` 的最小 JSON HTTP 适配层。

只做协议转换：解析路径与 JSON、注入操作人、调用应用服务/视图、
序列化错误。业务规则全部在下层。单进程多线程运行，时间可注入
便于演练；不鉴权，操作人由 ``X-Staff-Id`` 头或请求体给出。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from . import builder, planning, views
from .catalog import ServiceCatalog, seed_catalog
from .models import ServiceType
from .repository import BookingRepository
from .service import RailwayService, ServiceError


def _default_clock() -> datetime:
    return datetime.now()


class ApiApp:
    """持有服务实例与时钟，按 (方法, 正则) 分派。"""

    def __init__(
        self,
        service: Optional[RailwayService] = None,
        clock: Callable[[], datetime] = _default_clock,
    ):
        self.service = service or RailwayService(seed_catalog(), BookingRepository())
        self.clock = clock
        self.routes = [
            ("POST", r"^/bookings$", self.submit),
            ("GET", r"^/bookings/(?P<id>B\d+)$", self.get_booking),
            ("GET", r"^/bookings/(?P<id>B\d+)/chain$", self.get_chain),
            ("POST", r"^/bookings/(?P<id>B\d+)/eligibility$", self.verify_eligibility),
            ("POST", r"^/bookings/(?P<id>B\d+)/handovers$", self.handover),
            ("POST", r"^/bookings/(?P<id>B\d+)/tasks/(?P<task>T\d+)/start$", self.start_task),
            ("POST", r"^/bookings/(?P<id>B\d+)/tasks/(?P<task>T\d+)/complete$", self.complete_task),
            ("POST", r"^/bookings/(?P<id>B\d+)/cancel$", self.cancel),
            ("POST", r"^/bookings/(?P<id>B\d+)/adjust$", self.adjust),
            ("GET", r"^/reviews$", self.list_reviews),
            ("POST", r"^/reviews/(?P<id>R\d+)/resolve$", self.resolve_review),
            ("GET", r"^/staff/(?P<sid>[^/]+)/tasks$", self.staff_tasks),
            ("GET", r"^/stations/(?P<code>[^/]+)/dashboard$", self.station_dashboard),
            ("GET", r"^/certificates/(?P<no>[^/]+)$", self.check_certificate),
        ]

    # --- 分派 --------------------------------------------------------------

    def dispatch(self, method: str, path: str, query: dict, body: dict, actor: Optional[str]):
        for route_method, pattern, handler in self.routes:
            match = re.match(pattern, path)
            if method == route_method and match:
                return handler(query, body, actor, **match.groupdict())
        return 404, {"error": {"code": "not_found", "message": "接口不存在"}}

    # --- 端点 --------------------------------------------------------------

    def submit(self, query, body, actor):
        if actor:
            body.setdefault("operator_id", actor)
        result = self.service.submit_booking(body, self.clock())
        payload = self.booking_summary(result.booking)
        payload["duplicate"] = result.duplicate
        if result.review:
            payload["review"] = {
                "review_id": result.review.review_id,
                "changed_fields": list(result.review.changed_fields),
                "status": "pending",
            }
        return 200, payload

    def get_booking(self, query, body, actor, id):
        return 200, self.booking_summary(self.service.repo.get(id) or _missing(id))

    def get_chain(self, query, body, actor, id):
        booking = self.service.repo.get(id) or _missing(id)
        service = None
        if query.get("service"):
            service = _service_type(query["service"][0])
        return 200, views.responsibility_chain(booking, service)

    def verify_eligibility(self, query, body, actor, id):
        booking = self.service.verify_eligibility(
            id,
            _service_type(body.get("service_type", ServiceType.KEY_PASSENGER.value)),
            bool(body.get("approved", False)),
            actor or body.get("operator_id", "unknown"),
            self.clock(),
            body.get("note", ""),
        )
        return 200, self.booking_summary(booking)

    def handover(self, query, body, actor, id):
        handover = self.service.perform_handover(
            id,
            body["from_task_id"],
            actor or _missing_field("X-Staff-Id"),
            self.clock(),
            body.get("objects", []),
            body.get("summary", ""),
        )
        return 200, {
            "handover_id": handover.handover_id,
            "from_task_id": handover.task_id_from,
            "to_task_id": handover.task_id_to,
            "to_staff": handover.to_staff_id,
            "objects": list(handover.objects),
            "at": handover.at.isoformat(),
        }

    def start_task(self, query, body, actor, id, task):
        t = self.service.start_task(id, task, actor or _missing_field("X-Staff-Id"), self.clock())
        return 200, {"task_id": t.task_id, "status": t.status.value}

    def complete_task(self, query, body, actor, id, task):
        t = self.service.complete_task(
            id, task, actor or _missing_field("X-Staff-Id"), self.clock(), body.get("note", "")
        )
        return 200, {"task_id": t.task_id, "status": t.status.value}

    def cancel(self, query, body, actor, id):
        service_type = _service_type(body["service_type"]) if body.get("service_type") else None
        booking = self.service.cancel(
            id,
            actor or body.get("operator_id", "unknown"),
            self.clock(),
            body.get("reason", ""),
            service_type,
        )
        return 200, self.booking_summary(booking)

    def adjust(self, query, body, actor, id):
        booking = self.service.adjust_train(
            id,
            actor or body.get("operator_id", "unknown"),
            self.clock(),
            main=body.get("main"),
            pet=body.get("pet"),
            luggage=body.get("luggage"),
        )
        return 200, self.booking_summary(booking)

    def list_reviews(self, query, body, actor):
        cases = self.service.repo.list_reviews(include_resolved=query.get("all") == ["1"])
        return 200, {
            "reviews": [
                {
                    "review_id": case.review_id,
                    "booking_id": case.booking_id,
                    "changed_fields": list(case.changed_fields),
                    "resolution": case.resolution or "pending",
                }
                for case in cases
            ]
        }

    def resolve_review(self, query, body, actor, id):
        case = self.service.resolve_review(
            id,
            body.get("decision", ""),
            actor or body.get("operator_id", "unknown"),
            self.clock(),
            body.get("note", ""),
        )
        return 200, {"review_id": case.review_id, "resolution": case.resolution}

    def staff_tasks(self, query, body, actor, sid):
        try:
            data = views.staff_task_view(
                self.service,
                sid,
                station_code=query.get("station", [None])[0],
                include_finished=query.get("all") == ["1"],
            )
        except KeyError as exc:
            raise ServiceError("staff_not_found", str(exc), 404) from exc
        return 200, data

    def station_dashboard(self, query, body, actor, code):
        return 200, views.station_dashboard(self.service, code)

    def check_certificate(self, query, body, actor, no):
        at_raw = query.get("at", [None])[0]
        at = datetime.fromisoformat(at_raw) if at_raw else self.clock()
        return 200, self.service.verify_certificate(no, at)

    # --- 序列化 ------------------------------------------------------------

    @staticmethod
    def booking_summary(booking) -> dict:
        return {
            "booking_id": booking.booking_id,
            "version": booking.version,
            "status": booking.status.value,
            "idempotency_key": booking.idempotency_key,
            "train": booking.itinerary.train_code,
            "route": [booking.itinerary.board_station, booking.itinerary.alight_station],
            "passengers": [
                {"id": p.passenger_id, "name": p.name, "relation": p.relation, "primary": p.is_primary}
                for p in booking.passengers
            ],
            "needs": [
                {
                    "service": need.service_type.value,
                    "passenger_id": need.passenger_id,
                    "eligibility": need.eligibility.value
                    if need.service_type is ServiceType.KEY_PASSENGER
                    else "not_required",
                    "cancelled": need.cancelled,
                }
                for need in booking.needs
            ],
            "tasks": [
                {
                    "task_id": task.task_id,
                    "service": task.service_type.value,
                    "kind": task.kind.value,
                    "station": task.station_code,
                    "train": task.train_code,
                    "seq": task.seq,
                    "status": task.status.value,
                    "assignee": task.assignee_id,
                    "scheduled_start": task.scheduled_start.isoformat(),
                    "scheduled_end": task.scheduled_end.isoformat(),
                }
                for task in sorted(booking.tasks, key=lambda t: (t.service_type.value, t.seq))
            ],
        }


def _service_type(value: str) -> ServiceType:
    try:
        return ServiceType(value)
    except ValueError as exc:
        raise ServiceError("unknown_service", f"未知服务类型：{value}") from exc


def _missing(booking_id: str):
    raise ServiceError("booking_not_found", f"预约不存在：{booking_id}", 404)


def _missing_field(name: str):
    raise ServiceError("operator_required", f"缺少操作人：请提供 {name} 头", 403)


# --- HTTP 外壳 -----------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "RailwayAssistance/0.1"

    @property
    def app(self) -> ApiApp:
        return self.server.app  # type: ignore[attr-defined]

    def _write(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _serve(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length") or 0)
        body: dict = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._write(400, {"error": {"code": "bad_json", "message": "请求体不是合法 JSON"}})
                return
            if not isinstance(body, dict):
                self._write(400, {"error": {"code": "bad_body", "message": "请求体必须是 JSON 对象"}})
                return
        actor = self.headers.get("X-Staff-Id")
        try:
            status, payload = self.app.dispatch(method, parsed.path, query, body, actor)
        except (ServiceError,) as exc:
            self._write(exc.http_status, {"error": {"code": exc.code, "message": str(exc)}})
        except (builder.RequestError, planning.PlanningError) as exc:
            self._write(422, {"error": {"code": exc.code, "message": str(exc)}})
        else:
            self._write(status, payload)

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def log_message(self, fmt, *args) -> None:  # 静默标准访问日志
        return


def build_server(host: str = "127.0.0.1", port: int = 8080, catalog: Optional[ServiceCatalog] = None,
                 clock: Callable[[], datetime] = _default_clock) -> ThreadingHTTPServer:
    app = ApiApp(RailwayService(catalog or seed_catalog(), BookingRepository()), clock)
    server = ThreadingHTTPServer((host, port), _Handler)
    server.app = app  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="铁路便民服务预约与现场交接后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = build_server(args.host, args.port)
    print(f"服务已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
