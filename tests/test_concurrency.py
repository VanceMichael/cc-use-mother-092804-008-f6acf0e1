"""并发重复提交：同一幂等键在多线程下只能受理一次。"""

import threading
import unittest

from src.models import ServiceType

from tests.factories import NOW, booking_raw, make_service


class ConcurrentSubmitTest(unittest.TestCase):
    def test_same_idempotency_key_accepted_once_under_concurrency(self) -> None:
        svc = make_service()
        results: list = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                results.append(svc.submit_booking(booking_raw("CONC-1"), NOW).booking)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        booking_ids = {b.booking_id for b in results}
        self.assertEqual(len(booking_ids), 1)
        self.assertEqual(len(svc.repo.list_bookings()), 1)
        booking = results[0]
        # 每个服务的任务总数不随并发增加
        self.assertEqual(
            sum(1 for t in booking.tasks if t.service_type is ServiceType.PET), 2
        )


if __name__ == "__main__":
    unittest.main()
