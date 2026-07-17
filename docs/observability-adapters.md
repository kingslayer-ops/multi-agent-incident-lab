# Real observability adapters

Version 1.3.0 adds read-only Prometheus and Loki adapters behind the same allow-listed tools used by deterministic scenarios. The workflow, approval gate, remediation sandbox, and UI do not change.

## Modes

`INCIDENT_LAB_TELEMETRY_MODE=mock` is the default and remains the baseline for unit tests, evaluation, Playwright, and offline demonstrations.

`INCIDENT_LAB_TELEMETRY_MODE=live` makes the Worker query the documented [Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/) and [Loki HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/):

- Prometheus `GET /api/v1/query_range`
- Loki `GET /loki/api/v1/query_range`

Only the Worker requires these settings. The API still creates durable work and returns `202` without waiting for telemetry.

## Prometheus

Set at least:

```dotenv
INCIDENT_LAB_TELEMETRY_MODE=live
INCIDENT_LAB_PROMETHEUS_URL=https://prometheus.example.com
```

The default query set expects a `service` label and common HTTP metric names. Production installations should provide their own named PromQL expressions:

```dotenv
INCIDENT_LAB_PROMETHEUS_QUERIES_JSON={"queue_depth":"max(queue_depth{service=\"$service\"})","error_rate":"sum(rate(http_requests_total{service=\"$service\",status=~\"5..\"}[5m]))"}
```

`$service` is replaced with the scenario service after Prometheus label-value escaping. The adapter normalizes matrix and vector responses into named series, labels, and samples. Query warnings remain attached to the evidence payload.

Optional controls:

| Variable | Default | Bounds / purpose |
| --- | ---: | --- |
| `INCIDENT_LAB_PROMETHEUS_TIMEOUT` | `5` | HTTP timeout, 0.1-60 seconds |
| `INCIDENT_LAB_PROMETHEUS_LOOKBACK_SECONDS` | `900` | Query window, 60-86400 seconds |
| `INCIDENT_LAB_PROMETHEUS_STEP_SECONDS` | `60` | Range resolution, 1-3600 seconds |
| `INCIDENT_LAB_PROMETHEUS_SERIES_LIMIT` | `100` | Maximum returned series, 1-1000 |

## Loki

Set at least:

```dotenv
INCIDENT_LAB_LOKI_URL=https://loki.example.com
INCIDENT_LAB_LOKI_QUERY_TEMPLATE={service_name="$service"} |~ "(?i)(error|exception|timeout|failed)"
```

The adapter queries a bounded time window in backward order, normalizes stream labels and nanosecond timestamps, and removes duplicate entries. Configure the query template for the labels used by your log pipeline.

Optional controls:

| Variable | Default | Bounds / purpose |
| --- | ---: | --- |
| `INCIDENT_LAB_LOKI_TIMEOUT` | `5` | HTTP timeout, 0.1-60 seconds |
| `INCIDENT_LAB_LOKI_LOOKBACK_SECONDS` | `900` | Query window, 60-86400 seconds |
| `INCIDENT_LAB_LOKI_LINE_LIMIT` | `100` | Maximum returned lines, 1-1000 |
| `INCIDENT_LAB_LOKI_TENANT_ID` | empty | Sends `X-Scope-OrgID` for multi-tenant Loki |

## Authentication

Each adapter supports one authentication method:

- bearer: `..._BEARER_TOKEN`
- basic: paired `..._USERNAME` and `..._PASSWORD`

Bearer and Basic credentials cannot be combined. Credentials embedded in URLs are rejected. TLS certificates are verified with the operating system trust store; there is deliberately no insecure-skip-verify option. Loki itself does not provide authentication, so its [official authentication guidance](https://grafana.com/docs/loki/latest/operations/authentication/) recommends an authenticating reverse proxy; multi-tenant requests use `X-Scope-OrgID`.

## Failure and fallback semantics

`INCIDENT_LAB_TELEMETRY_FALLBACK=true` is the default. These conditions fall back independently to deterministic metrics or logs:

- missing live endpoint configuration
- connection failure (`connection_error`) or timeout (`timeout`)
- HTTP `429` rate limit (`rate_limited`) or `5xx` response (`service_unavailable`)
- HTTP `401/403` (`authentication_error`) or another HTTP error (`http_error`)
- invalid JSON or response envelope
- unsupported result shape
- empty result
- response larger than 2 MB

Fallback evidence uses a source such as `prometheus://fallback/demo` and contains:

```json
{
  "collection": {
    "mode": "fallback",
    "provider": "prometheus",
    "reason_code": "timeout",
    "retryable": true
  }
}
```

Secrets and endpoint URLs are not copied into error messages. Set `INCIDENT_LAB_TELEMETRY_FALLBACK=false` when an investigation must fail or retry instead of using synthetic evidence; the existing durable Worker retry policy then applies.

## Verification boundary

Contract tests run against an in-process HTTP server and verify paths, encoded query parameters, authentication headers, response normalization, error classification, and fallback metadata. The deterministic E2E environment explicitly sets telemetry mode to `mock`, so CI does not depend on external observability infrastructure.

The adapters are read-only. v1.3.0 does not add remote remediation, Redis/PostgreSQL coordination, authentication for the Incident Lab UI, or a production deployment claim.
