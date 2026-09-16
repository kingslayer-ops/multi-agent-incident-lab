from __future__ import annotations

import os
from uuid import uuid4

import pytest

from incident_lab.runtime import RedisNotifier


pytestmark = pytest.mark.skipif(
    not os.getenv("INCIDENT_LAB_TEST_REDIS_URL"),
    reason="Redis integration URL is not configured",
)


def test_real_redis_wakeup_is_consumed() -> None:
    notifier = RedisNotifier(
        os.environ["INCIDENT_LAB_TEST_REDIS_URL"],
        queue_name=f"incident-lab:test:{uuid4().hex}",
    )
    notifier.notify()
    assert notifier.client.llen(notifier.queue_name) == 1
    notifier.wait(1)
    assert notifier.client.llen(notifier.queue_name) == 0
