from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import ScenarioSummary


SCENARIOS: dict[str, dict[str, Any]] = {
    "payment-pool-exhaustion": {
        "summary": ScenarioSummary(
            id="payment-pool-exhaustion",
            title="Payment API latency spike",
            service="payment-service",
            symptom="P99 latency rose from 220 ms to 4.2 s and checkout errors reached 18%.",
            severity="SEV-1",
            difficulty="medium",
        ),
        "metrics": {
            "p99_latency_ms": 4210,
            "error_rate": 0.18,
            "cpu_percent": 46,
            "db_pool_active": 30,
            "db_pool_max": 30,
            "db_pool_waiters": 84,
        },
        "logs": [
            "Timeout waiting for connection from pool after 3000ms",
            "POST /payments/authorize returned 503",
            "HikariPool-1 - Connection is not available",
        ],
        "changes": [
            {"id": "deploy-1842", "summary": "Enabled synchronous fraud audit", "minutes_before": 11},
            {"id": "cfg-992", "summary": "Reduced database pool from 60 to 30", "minutes_before": 14},
        ],
        "root_cause": "database connection pool exhaustion",
        "alternatives": ["upstream payment provider degradation", "CPU saturation"],
        "remediation": {
            "title": "Restore the payment database pool limit",
            "description": "Roll back cfg-992 and restore the pool limit from 30 to 60, then verify waiters and P99 latency.",
            "command": "opspilot rollback-config payment-service cfg-992",
            "risk": "high",
        },
    },
    "inventory-memory-leak": {
        "summary": ScenarioSummary(
            id="inventory-memory-leak",
            title="Inventory pods repeatedly restart",
            service="inventory-service",
            symptom="Memory usage climbs steadily and pods are terminated by the OOM killer every 17 minutes.",
            severity="SEV-2",
            difficulty="hard",
        ),
        "metrics": {
            "memory_percent": 97,
            "memory_growth_mb_min": 42,
            "restart_count": 7,
            "cpu_percent": 33,
            "request_rate": 280,
        },
        "logs": [
            "Container terminated with reason OOMKilled",
            "cache entries=148223 evictions=0",
            "heap allocation dominated by InventorySnapshot objects",
        ],
        "changes": [
            {"id": "deploy-2077", "summary": "Added unbounded inventory snapshot cache", "minutes_before": 48},
        ],
        "root_cause": "unbounded inventory snapshot cache",
        "alternatives": ["traffic spike", "container memory limit regression"],
        "remediation": {
            "title": "Roll back the unbounded cache release",
            "description": "Roll back deploy-2077 and cap cache size before a guarded redeployment.",
            "command": "opspilot rollback-deploy inventory-service deploy-2077",
            "risk": "high",
        },
    },
    "checkout-feature-regression": {
        "summary": ScenarioSummary(
            id="checkout-feature-regression",
            title="Checkout validation failures",
            service="checkout-service",
            symptom="Valid coupons are rejected after a feature-flag rollout, raising 422 responses to 31%.",
            severity="SEV-2",
            difficulty="easy",
        ),
        "metrics": {
            "http_422_rate": 0.31,
            "error_rate": 0.32,
            "cpu_percent": 21,
            "latency_ms": 180,
        },
        "logs": [
            "CouponValidationError: timezone-aware expiry compared with local date",
            "feature flag strict_coupon_expiry=true",
            "422 /checkout for coupon SUMMER26",
        ],
        "changes": [
            {"id": "flag-441", "summary": "Enabled strict coupon expiry validation", "minutes_before": 6},
        ],
        "root_cause": "strict coupon expiry feature flag regression",
        "alternatives": ["coupon database corruption", "clock drift"],
        "remediation": {
            "title": "Disable the faulty validation flag",
            "description": "Disable flag-441, replay a canary checkout, and monitor the 422 rate.",
            "command": "opspilot disable-flag checkout-service flag-441",
            "risk": "medium",
        },
    },
}


SCENARIOS.update(
    {
        "gateway-tls-expiry": {
            "summary": ScenarioSummary(id="gateway-tls-expiry", title="Gateway TLS failures", service="api-gateway", symptom="Inter-service requests fail TLS verification and return 502 responses.", severity="SEV-1", difficulty="easy"),
            "metrics": {"http_502_rate": 0.44, "tls_handshake_failures": 831, "cpu_percent": 19},
            "logs": ["x509: certificate has expired or is not yet valid", "upstream TLS handshake failed"],
            "changes": [{"id": "cert-2026-07", "summary": "Certificate rotation job failed", "minutes_before": 23}],
            "root_cause": "expired service TLS certificate",
            "alternatives": ["gateway CPU saturation", "upstream connection reset"],
            "remediation": {"title": "Rotate the expired certificate", "description": "Activate the staged certificate and verify the trust chain.", "command": "opspilot rotate-cert api-gateway cert-2026-07", "risk": "high"},
        },
        "redis-topology-stale": {
            "summary": ScenarioSummary(id="redis-topology-stale", title="Session cache redirect storm", service="session-service", symptom="Redis redirects and retry amplification push login latency above 3 seconds.", severity="SEV-2", difficulty="hard"),
            "metrics": {"redis_redirect_rate": 0.63, "p99_latency_ms": 3180, "retry_rate": 4.8},
            "logs": ["Redis MOVED 8412 cache-node-4:6379", "topology refresh suppressed for 600s"],
            "changes": [{"id": "cfg-redis-88", "summary": "Increased cluster topology cache TTL", "minutes_before": 31}],
            "root_cause": "stale Redis cluster topology cache",
            "alternatives": ["cache node memory pressure", "network packet loss"],
            "remediation": {"title": "Refresh Redis topology", "description": "Invalidate the client topology cache and restore the safe TTL.", "command": "opspilot rollback-config session-service cfg-redis-88", "risk": "medium"},
        },
        "order-deadlock": {
            "summary": ScenarioSummary(id="order-deadlock", title="Order transaction deadlocks", service="order-service", symptom="Order confirmations intermittently fail while database lock waits spike.", severity="SEV-1", difficulty="hard"),
            "metrics": {"deadlocks_min": 97, "lock_wait_ms": 6800, "error_rate": 0.22},
            "logs": ["deadlock detected while locking order_items", "transaction rolled back; retry limit reached"],
            "changes": [{"id": "deploy-order-52", "summary": "Parallelized inventory and order row updates", "minutes_before": 19}],
            "root_cause": "database deadlock caused by inconsistent lock ordering",
            "alternatives": ["slow read replica", "connection pool exhaustion"],
            "remediation": {"title": "Roll back parallel row updates", "description": "Restore deterministic lock ordering and replay failed transactions.", "command": "opspilot rollback-deploy order-service deploy-order-52", "risk": "high"},
        },
        "notification-rate-limit": {
            "summary": ScenarioSummary(id="notification-rate-limit", title="Notification delivery backlog", service="notification-service", symptom="Email delivery is delayed and retry queues are growing rapidly.", severity="SEV-3", difficulty="medium"),
            "metrics": {"queue_depth": 18400, "delivery_rate": 0.37, "provider_429_rate": 0.71},
            "logs": ["provider response 429: rate limit exceeded", "retry budget consumed"],
            "changes": [{"id": "campaign-711", "summary": "Launched unthrottled marketing campaign", "minutes_before": 42}],
            "root_cause": "notification provider rate-limit exhaustion",
            "alternatives": ["worker crash loop", "message broker partition"],
            "remediation": {"title": "Throttle campaign traffic", "description": "Pause campaign fan-out and drain the queue within provider quotas.", "command": "opspilot throttle-campaign notification-service campaign-711", "risk": "medium"},
        },
        "event-schema-incompatibility": {
            "summary": ScenarioSummary(id="event-schema-incompatibility", title="Order events rejected", service="analytics-consumer", symptom="New order events enter the dead-letter queue after a producer rollout.", severity="SEV-2", difficulty="medium"),
            "metrics": {"dlq_rate": 0.58, "consumer_lag": 27100, "success_rate": 0.41},
            "logs": ["schema validation failed: required field currency missing", "event version=7 unsupported"],
            "changes": [{"id": "deploy-producer-77", "summary": "Published order event schema v7", "minutes_before": 12}],
            "root_cause": "incompatible event schema deployment",
            "alternatives": ["broker storage pressure", "consumer autoscaling lag"],
            "remediation": {"title": "Restore compatible event schema", "description": "Roll back schema v7 and replay dead-lettered events.", "command": "opspilot rollback-deploy event-producer deploy-producer-77", "risk": "high"},
        },
        "service-dns-failure": {
            "summary": ScenarioSummary(id="service-dns-failure", title="Service discovery failures", service="recommendation-service", symptom="Recommendation calls time out because the profile service cannot be resolved.", severity="SEV-2", difficulty="medium"),
            "metrics": {"dns_error_rate": 0.82, "timeout_rate": 0.77, "cpu_percent": 24},
            "logs": ["DNS resolution failed for profile.service.local", "temporary failure in name resolution"],
            "changes": [{"id": "dns-zone-19", "summary": "Removed legacy profile service alias", "minutes_before": 8}],
            "root_cause": "service discovery DNS failure",
            "alternatives": ["profile service overload", "egress firewall denial"],
            "remediation": {"title": "Restore the service alias", "description": "Restore the deleted DNS alias and validate resolution from a canary pod.", "command": "opspilot rollback-config dns dns-zone-19", "risk": "high"},
        },
        "logging-disk-full": {
            "summary": ScenarioSummary(id="logging-disk-full", title="Logging nodes reject writes", service="logging-service", symptom="Audit logs are dropped as collector disks reach full capacity.", severity="SEV-1", difficulty="easy"),
            "metrics": {"disk_percent": 100, "dropped_events": 42000, "ingest_rate": 0.05},
            "logs": ["write failed: disk quota exceeded", "segment rotation blocked"],
            "changes": [{"id": "cfg-retention-9", "summary": "Raised hot-log retention from 7 to 30 days", "minutes_before": 310}],
            "root_cause": "log volume disk exhaustion",
            "alternatives": ["collector CPU overload", "invalid log payloads"],
            "remediation": {"title": "Restore safe log retention", "description": "Revert retention and compact expired segments without deleting active audit data.", "command": "opspilot rollback-config logging-service cfg-retention-9", "risk": "high"},
        },
        "auth-clock-skew": {
            "summary": ScenarioSummary(id="auth-clock-skew", title="Authentication token failures", service="auth-service", symptom="Valid tokens are rejected as not-yet-valid on a subset of nodes.", severity="SEV-1", difficulty="hard"),
            "metrics": {"auth_failure_rate": 0.34, "affected_nodes": 3, "latency_ms": 92},
            "logs": ["JWT rejected: clock skew 184 seconds", "token used before issued-at timestamp"],
            "changes": [{"id": "ntp-policy-12", "summary": "Changed NTP firewall policy", "minutes_before": 67}],
            "root_cause": "node clock skew invalidating authentication tokens",
            "alternatives": ["signing key mismatch", "token cache corruption"],
            "remediation": {"title": "Restore time synchronization", "description": "Revert the NTP policy and resynchronize affected nodes.", "command": "opspilot rollback-config auth-service ntp-policy-12", "risk": "high"},
        },
        "worker-thread-saturation": {
            "summary": ScenarioSummary(id="worker-thread-saturation", title="Report generation stalls", service="report-service", symptom="Interactive reports queue for minutes while CPU remains below 50%.", severity="SEV-2", difficulty="hard"),
            "metrics": {"queue_depth": 940, "active_threads": 32, "max_threads": 32, "cpu_percent": 43},
            "logs": ["thread pool rejected task: active=32 queued=940", "blocking export call exceeded 30s"],
            "changes": [{"id": "deploy-report-31", "summary": "Moved exports onto the interactive worker pool", "minutes_before": 27}],
            "root_cause": "worker thread pool saturation",
            "alternatives": ["CPU saturation", "database query regression"],
            "remediation": {"title": "Isolate export workloads", "description": "Roll back the shared worker pool and drain queued interactive jobs.", "command": "opspilot rollback-deploy report-service deploy-report-31", "risk": "high"},
        },
    }
)


def list_scenarios() -> list[ScenarioSummary]:
    return [deepcopy(item["summary"]) for item in SCENARIOS.values()]


def get_scenario(scenario_id: str) -> dict[str, Any]:
    try:
        return deepcopy(SCENARIOS[scenario_id])
    except KeyError as exc:
        raise KeyError(f"Unknown scenario: {scenario_id}") from exc
