# End-to-end testing

v1.2.1 proves the incident lifecycle through Chromium against real API, Worker, frontend, and SQLite processes. Tests use stable `data-testid` selectors and wait for persisted states or API evidence instead of arbitrary sleeps.

## Covered flows

1. Complete incident creation, investigation, approval, remediation, recovery, and postmortem.
2. Browser refresh during investigation with durable state restoration and no duplicate trace steps.
3. Approval pause followed by resume from the remediation checkpoint, including rapid duplicate submission.
4. Approval rejection persisted as `approval.rejected`, with a `cancelled` terminal state and no action execution.
5. Real Docker Worker stop/start, lease recovery, checkpoint continuation, and browser completion.
6. Browser network outage, background progress, cursor-based SSE replay, and trace de-duplication.

## Isolated environment

`docker-compose.e2e.yml` starts:

- `api`: FastAPI with deterministic model mode and a dedicated SQLite path;
- `worker`: short leases and a controlled per-step delay so transient states are observable;
- `frontend`: Nginx serving the production React build and proxying API/SSE traffic;
- `e2e-data`: an isolated named volume removed after the run.

Run the full suite:

```bash
make e2e
```

Run only the PR smoke path:

```bash
make e2e-smoke
```

The runner builds the stack, waits for health checks, installs the pinned Chromium runtime, executes Playwright, captures Compose logs, and always removes containers and the test volume.

## Failure evidence

Playwright retains screenshots, video, and trace for failed attempts. A custom fixture attaches browser console and API/network activity. CI also captures Compose service logs and uploads `e2e/playwright-report` plus `e2e/test-results` as a GitHub Actions artifact.

Pull requests run the Chromium smoke loop. Pushes to `main` and version tags run all six flows, including Worker restart and SSE disconnect recovery, with one CI worker for reproducibility. This follows Playwright's CI guidance to favor a single worker and retain trace evidence for debugging.
