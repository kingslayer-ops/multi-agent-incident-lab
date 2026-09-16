# Workflow reliability

## Guarantee

v1.2.0 implements **at-least-once scheduling with idempotent step commits**. It does not claim exactly-once execution. A step may be attempted more than once after a timeout or process crash, while persisted output is accepted only once from the current lease owner and version.

## Atomic claim and fencing

`WorkflowStore.claim_next` starts `BEGIN IMMEDIATE`, recovers expired leases, selects one available step, and atomically writes:

- `status=running`
- `lease_owner` and `lease_expires_at`
- heartbeat and timeout timestamps
- incremented attempt count and row version
- a `workflow_step_attempts` record
- run/incident state and a `step.started` event

Completion and failure both compare step ID, owner, version, status, and lease expiry. A late worker therefore cannot overwrite a step recovered by another worker.

## Recovery

On startup and before each claim, the worker scans expired leases. The old attempt becomes `abandoned`, a `step.lease_expired` event is appended, and the step returns to `queued`. The next worker receives the same stable step and idempotency key with a higher attempt count and version.

The test suite launches a real Python worker subprocess in a crash-after-claim mode, verifies its non-zero exit, waits for lease expiry, and completes the recovered step with a new worker.

## Transactions and events

State changes, incident JSON checkpoints, and workflow events share one SQLite or PostgreSQL transaction. Event sequence numbers are monotonically increasing per run. A forced event-insert failure is tested to prove the corresponding step completion rolls back.

The SSE endpoint reads this durable log. Browsers reconnect with `Last-Event-ID`; the API replays only later events and sends heartbeat comments while idle.

## Retries and cancellation

Transient failures use bounded exponential backoff. Exhausted steps terminate the run as `failed`; a manual retry clears the failure and requeues only that step within a limit. Cancellation is immediate for queued or paused work. Running work becomes `cancel_requested` and terminates at its next safe commit boundary. An expired lease on a cancel-requested run is finalized as cancelled rather than requeued.

## Approval and action idempotency

`approval_records.workflow_run_id` is unique. The first approval is final; the same request is idempotent and conflicting later decisions are rejected. Remediation uses a unique key derived from run and action. A recovered reservation suppresses a second sandbox command, and a completed result is reused.

## SQLite concurrency model

SQLite runs in WAL mode with a busy timeout. `BEGIN IMMEDIATE` serializes the short claim/transition transactions; agent and model work happens outside the database transaction. This is suitable for the repository's single-host lab scope.

The orchestration boundary does not expose a database to agents or API routes. The v1.4 production adapter maps the same operations to PostgreSQL row locking without changing step business logic. Redis is only a wake-up optimization; durable runnable work remains in PostgreSQL.

## PostgreSQL concurrency model

The production profile claims rows with `FOR UPDATE SKIP LOCKED`, fences stale commits with the same owner/version checks, locks expired leases during recovery, and allocates event sequence numbers under a per-run advisory lock. Multiple Worker replicas can therefore make progress without treating Redis delivery as durable scheduling.

## Current boundaries

- Celery, Kafka, or Redis-backed durable queue semantics
- Prometheus/Loki writes, alert management, or unbounded queries; v1.3 provides bounded read-only range-query adapters
- remote shell, Kubernetes, or cloud mutations
- exactly-once external side effects
- open-ended dynamic workflow graphs

The project deliberately deepens reliability and evidence ingestion without claiming to be a production distributed control plane.
