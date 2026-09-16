# PostgreSQL and Redis runtime

Version 1.4 adds an opt-in production-style runtime without removing the deterministic SQLite baseline.

## Responsibility split

| Component | Responsibility | Failure behavior |
| --- | --- | --- |
| PostgreSQL | Incidents, workflow runs, checkpoints, leases, attempts, approvals, remediation keys, and ordered events | The API and Workers stop accepting state changes rather than acknowledge uncommitted work |
| Redis | Bounded wake-up hints after new or resumed work is committed | A failed publish is ignored; Workers fall back to periodic PostgreSQL polling |
| Worker replicas | Claim and execute runnable steps | `FOR UPDATE SKIP LOCKED` prevents two live Workers from claiming the same step |

Redis is deliberately not a task source of truth. A wake-up may be duplicated or lost without losing a workflow because every runnable step already exists in PostgreSQL.

## Run the production profile

```bash
docker compose -f docker-compose.production.yml up --build --scale worker=2
```

Open <http://localhost:8000>. The health endpoint exposes the selected storage and notification backends:

```json
{"status":"ok","storage":"postgresql","notifier":"redis"}
```

The Compose profile starts PostgreSQL, Redis, one API, and two Workers. It keeps deterministic Mock model and telemetry providers unless explicitly configured otherwise.
Set `INCIDENT_LAB_POSTGRES_PASSWORD` outside the repository before using this profile beyond local evaluation; the checked-in default is only a local Compose credential.

## Concurrency guarantees

- Worker claims use a PostgreSQL transaction and `FOR UPDATE SKIP LOCKED`.
- Lease owner and version checks fence late commits from stale Workers.
- Expired-lease recovery locks eligible rows with `SKIP LOCKED`, so concurrent Workers recover each lease at most once.
- Per-run advisory locks serialize event sequence allocation.
- Approval records and remediation executions retain their existing uniqueness/idempotency keys.
- Scheduling remains at-least-once; the project does not claim exactly-once external side effects.

## Configuration

```dotenv
INCIDENT_LAB_DATABASE_URL=postgresql://incident_lab:secret@postgres:5432/incident_lab
INCIDENT_LAB_REDIS_URL=redis://redis:6379/0
INCIDENT_LAB_WORK_POLL_SECONDS=1
```

If `INCIDENT_LAB_DATABASE_URL` is absent, the application uses `INCIDENT_LAB_DB_PATH` and SQLite WAL. If `INCIDENT_LAB_REDIS_URL` is absent, Workers use polling. PostgreSQL support requires the `production` dependency group.

## Verification

The PostgreSQL integration test creates an isolated schema, races four Workers for one step, verifies that exactly one claim succeeds, reopens the stores, and confirms the durable incident and workflow are still readable.

```bash
INCIDENT_LAB_TEST_POSTGRES_URL=postgresql://incident_lab:incident_lab@localhost:5432/incident_lab_test \
pytest tests/test_postgres_integration.py
```

CI runs this test against a real PostgreSQL service and separately validates the production Compose file.

On Windows, the isolated PostgreSQL and Redis verification can be run with one command. It uses temporary in-memory container storage and removes the integration stack afterward:

```powershell
.\scripts\verify-production-runtime.ps1
```
