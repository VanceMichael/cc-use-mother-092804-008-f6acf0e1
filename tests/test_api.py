"""HTTP 适配层端到端测试：JSON 进出、错误码与操作人头。"""

import json
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from urllib import request as urlrequest
from urllib.error import HTTPError

from src.api import ApiApp, _Handler
from src.catalog import seed_catalog
from src.repository import BookingRepository
from src.service import RailwayService

from tests.factories import DAY, NOW, booking_raw


class Clock:
    def __init__(self, moment: datetime):
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class HttpSession:
    def __init__(self, clock: Clock):
        service = RailwayService(seed_catalog(), BookingRepository())
        self.app = ApiApp(service, clock)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method: str, path: str, body=None, staff=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urlrequest.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if staff:
            req.add_header("X-Staff-Id", staff)
        try:
            with urlrequest.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock(NOW)
        self.http = HttpSession(self.clock)

    def tearDown(self) -> None:
        self.http.stop()

    def test_full_journey_submit_duplicate_review_execute(self) -> None:
        raw = booking_raw("HTTP-1")
        status, payload = self.http.call("POST", "/bookings", raw, staff="M-VNP")
        self.assertEqual(status, 200, payload)
        booking_id = payload["booking_id"]
        self.assertFalse(payload["duplicate"])

        # 完全重复：同一预约
        _, again = self.http.call("POST", "/bookings", booking_raw("HTTP-1"))
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["booking_id"], booking_id)

        # 关键字段变化：产生复核单
        changed = booking_raw("HTTP-1", alight_station="NPH")
        _, review_payload = self.http.call("POST", "/bookings", changed)
        self.assertEqual(review_payload["review"]["status"], "pending")
        review_id = review_payload["review"]["review_id"]

        # 待复核期间改签被拒（400）
        status, err = self.http.call(
            "POST", f"/bookings/{booking_id}/adjust", {"main": {"alight_station": "HGH"}}, "M-VNP"
        )
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "pending_review")

        # 驳回复核，原行程保持
        status, _ = self.http.call(
            "POST", f"/reviews/{review_id}/resolve", {"decision": "rejected"}, "M-VNP"
        )
        self.assertEqual(status, 200)

        # 资格核验通过
        status, payload = self.http.call(
            "POST",
            f"/bookings/{booking_id}/eligibility",
            {"service_type": "key_passenger", "approved": True},
            "M-VNP",
        )
        self.assertEqual(status, 200)
        pickup = next(
            t for t in payload["tasks"]
            if t["service"] == "key_passenger" and t["kind"] == "pickup"
        )

        # 值班人员开始并交接首段
        self.clock.moment = datetime.fromisoformat(f"{DAY}T07:25:00")
        status, _ = self.http.call(
            "POST", f"/bookings/{booking_id}/tasks/{pickup['task_id']}/start", {}, pickup["assignee"]
        )
        self.assertEqual(status, 200)
        status, handover = self.http.call(
            "POST",
            f"/bookings/{booking_id}/handovers",
            {"from_task_id": pickup["task_id"], "objects": ["轮椅旅客林晚"]},
            pickup["assignee"],
        )
        self.assertEqual(status, 200, handover)
        self.assertEqual(handover["to_staff"][:6], "S-JGK-")

        # 责任链视图可查
        status, chain = self.http.call(
            "GET", f"/bookings/{booking_id}/chain?service=key_passenger"
        )
        self.assertEqual(status, 200)
        self.assertEqual(chain["chains"][0]["segments"][0]["status"], "completed")

        # 值班视图只返回本人任务（默认开放任务，已完成任务带 all=1 可追溯）
        status, open_mine = self.http.call("GET", f"/staff/{pickup['assignee']}/tasks")
        self.assertEqual(status, 200)
        status, mine = self.http.call("GET", f"/staff/{pickup['assignee']}/tasks?all=1")
        self.assertEqual(status, 200)
        completed_ids = {t["task_id"] for t in mine["tasks"] if t["status"] == "completed"}
        self.assertIn(pickup["task_id"], completed_ids)
        for row in mine["tasks"]:
            self.assertNotIn("passengers", row)  # 不含同行名单
            self.assertIn("objects", row)

    def test_rule_violation_returns_422(self) -> None:
        raw = booking_raw("HTTP-2")
        raw["needs"][1]["species"] = "snake"  # pet
        status, err = self.http.call("POST", "/bookings", raw)
        self.assertEqual(status, 422)
        self.assertEqual(err["error"]["code"], "species_forbidden")

    def test_bad_json_and_missing_booking(self) -> None:
        req = urlrequest.Request(self.http.base + "/bookings", data=b"{not json", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urlrequest.urlopen(req)
            self.fail("应当返回 400")
        except HTTPError as exc:
            self.assertEqual(exc.code, 400)

        status, err = self.http.call("GET", "/bookings/B999999")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "booking_not_found")


if __name__ == "__main__":
    unittest.main()
