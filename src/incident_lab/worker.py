from __future__ import annotations

import argparse
import os
import socket
import time
from uuid import uuid4

from . import __version__
from .workflow import WorkflowStepExecutor, WorkflowWorker
from .workflow_store import WorkflowStore


def main() -> None:  # pragma: no cover - exercised as a real subprocess integration
    parser = argparse.ArgumentParser(description="Run the durable incident workflow worker")
    parser.add_argument("--once", action="store_true", help="Claim at most one runnable step")
    parser.add_argument("--crash-after-claim", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    args = parser.parse_args()
    store = WorkflowStore(os.getenv("INCIDENT_LAB_DB_PATH", "data/incident-lab.db"), __version__)
    worker_id = os.getenv("INCIDENT_LAB_WORKER_ID", f"{socket.gethostname()}-{uuid4().hex[:6]}")
    lease_seconds = float(os.getenv("INCIDENT_LAB_LEASE_SECONDS", "30"))
    timeout_seconds = float(os.getenv("INCIDENT_LAB_STEP_TIMEOUT_SECONDS", "20"))
    executor = WorkflowStepExecutor(
        store,
        step_delay_seconds=float(os.getenv("INCIDENT_LAB_WORKER_STEP_DELAY_MS", "0")) / 1000,
    )
    worker = WorkflowWorker(
        store,
        worker_id,
        executor=executor,
        lease_seconds=lease_seconds,
        step_timeout_seconds=timeout_seconds,
    )
    store.recover_expired()
    if args.crash_after_claim:
        claim = store.claim_next(worker_id, lease_seconds, timeout_seconds)
        os._exit(17 if claim else 18)
    if args.once:
        worker.run_once()
        return
    while True:
        if not worker.run_once():
            time.sleep(args.poll_interval)


if __name__ == "__main__":  # pragma: no cover
    main()
