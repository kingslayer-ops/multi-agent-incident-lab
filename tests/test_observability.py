from __future__ import annotations

import base64
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

import pytest

import incident_lab.observability as observability
from incident_lab.observability import (
    FallbackObservabilityProvider,
    HttpJsonClient,
    HttpJsonSettings,
    LiveObservabilityProvider,
    LokiAdapter,
    PrometheusAdapter,
    ScenarioObservabilityProvider,
    TelemetryError,
    build_observability_provider,
)
from incident_lab.scenarios import get_scenario


class ServerState:
    def __init__(self) -> None:
        self.status = 200
        self.body: bytes = b"{}"
        self.requests: list[dict[str, object]] = []


@pytest.fixture
def telemetry_server():
    state = ServerState()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            state.requests.append({
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "headers": dict(self.headers),
            })
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(state.body)

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def response(payload: dict) -> bytes:
    return json.dumps(payload).encode()


def test_scenario_provider_remains_deterministic() -> None:
    provider = ScenarioObservabilityProvider()
    scenario = get_scenario("payment-pool-exhaustion")

    metrics = provider.query_metrics(scenario)
    logs = provider.search_logs(scenario)

    assert metrics.source == "prometheus://demo"
    assert metrics.payload == scenario["metrics"]
    assert logs.source == "loki://demo"
    assert logs.payload["lines"] == scenario["logs"]


def test_service_label_value_is_escaped_before_template_substitution() -> None:
    scenario = get_scenario("auth-clock-skew").copy()
    scenario["summary"] = scenario["summary"].model_copy(update={"service": 'auth"\\edge'})

    assert observability._service_name(scenario) == 'auth\\"\\\\edge'


def test_prometheus_query_range_contract_and_bearer_auth(telemetry_server) -> None:
    telemetry_server.body = response({
        "status": "success",
        "warnings": ["partial response"],
        "data": {
            "resultType": "matrix",
            "result": [{
                "metric": {"service": "payment-service", "instance": "api-1"},
                "values": [[1700000000, "0.42"], [1700000060, "0.55"]],
            }],
        },
    })
    client = HttpJsonClient(HttpJsonSettings(
        base_url=telemetry_server.url,
        bearer_token="prom-token",
    ))
    adapter = PrometheusAdapter(client, {"error_rate": 'rate(errors{service="$service"}[5m])'})

    result = adapter.query_metrics(get_scenario("payment-pool-exhaustion"))

    request = telemetry_server.requests[0]
    assert request["path"] == "/api/v1/query_range"
    assert request["query"]["query"] == ['rate(errors{service="payment-service"}[5m])']
    assert request["query"]["step"] == ["60"]
    assert request["headers"]["Authorization"] == "Bearer prom-token"
    assert result.payload["collection"] == {"mode": "live", "provider": "prometheus"}
    assert result.payload["warnings"] == ["partial response"]
    assert "2 samples" in result.summary


def test_prometheus_vector_is_normalized(telemetry_server) -> None:
    telemetry_server.body = response({
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {"job": "api"}, "value": [1700000000, "1"]}],
        },
    })
    adapter = PrometheusAdapter(
        HttpJsonClient(HttpJsonSettings(base_url=telemetry_server.url)), {"up": "up"}
    )

    result = adapter.query_metrics(get_scenario("auth-clock-skew"))

    assert result.payload["series"][0]["samples"] == [[1700000000, "1"]]


def test_loki_query_range_contract_basic_auth_tenant_and_deduplication(telemetry_server) -> None:
    telemetry_server.body = response({
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{
                "stream": {"service_name": "checkout-service", "level": "error"},
                "values": [
                    ["1700000000000000000", "timeout contacting inventory"],
                    ["1700000000000000000", "timeout contacting inventory"],
                ],
            }],
            "stats": {"summary": {"bytesProcessedPerSecond": 10}},
        },
    })
    client = HttpJsonClient(HttpJsonSettings(
        base_url=telemetry_server.url,
        username="reader",
        password="secret",
        extra_headers={"X-Scope-OrgID": "tenant-a"},
    ))
    adapter = LokiAdapter(client, '{service_name="$service"} |= "error"', line_limit=25)

    result = adapter.search_logs(get_scenario("checkout-feature-regression"))

    request = telemetry_server.requests[0]
    expected = base64.b64encode(b"reader:secret").decode()
    assert request["path"] == "/loki/api/v1/query_range"
    assert request["query"]["query"] == ['{service_name="checkout-service"} |= "error"']
    assert request["query"]["direction"] == ["backward"]
    assert request["query"]["limit"] == ["25"]
    assert request["headers"]["Authorization"] == f"Basic {expected}"
    headers = {key.lower(): value for key, value in request["headers"].items()}
    assert headers["x-scope-orgid"] == "tenant-a"
    assert len(result.payload["lines"]) == 1
    assert result.payload["collection"]["mode"] == "live"


@pytest.mark.parametrize(
    "status,reason,retryable",
    [
        (429, "rate_limited", True),
        (503, "service_unavailable", True),
        (401, "authentication_error", False),
        (404, "http_error", False),
    ],
)
def test_http_status_is_classified_and_fallback_is_auditable(
    telemetry_server, status: int, reason: str, retryable: bool
) -> None:
    telemetry_server.status = status
    primary = LiveObservabilityProvider(
        PrometheusAdapter(
            HttpJsonClient(HttpJsonSettings(base_url=telemetry_server.url)), {"up": "up"}
        ),
        None,
    )
    provider = FallbackObservabilityProvider(primary, ScenarioObservabilityProvider())

    result = provider.query_metrics(get_scenario("auth-clock-skew"))

    assert result.source == "prometheus://fallback/demo"
    assert result.payload["collection"] == {
        "mode": "fallback",
        "provider": "prometheus",
        "reason_code": reason,
        "retryable": retryable,
    }


@pytest.mark.parametrize(
    "payload,reason",
    [
        ({"status": "error", "errorType": "bad_data", "error": "bad query"}, "query_error"),
        ({"status": "success", "data": []}, "invalid_response"),
        ({"status": "success", "data": {"resultType": "scalar", "result": []}}, "invalid_response"),
        ({"status": "success", "data": {"resultType": "matrix", "result": []}}, "empty_result"),
    ],
)
def test_prometheus_protocol_failures_degrade_with_reason(telemetry_server, payload, reason) -> None:
    telemetry_server.body = response(payload)
    primary = LiveObservabilityProvider(
        PrometheusAdapter(
            HttpJsonClient(HttpJsonSettings(base_url=telemetry_server.url)), {"up": "up"}
        ),
        None,
    )
    result = FallbackObservabilityProvider(
        primary, ScenarioObservabilityProvider()
    ).query_metrics(get_scenario("auth-clock-skew"))

    assert result.payload["collection"]["reason_code"] == reason


@pytest.mark.parametrize(
    "payload,reason",
    [
        ({"status": "success", "data": {"resultType": "matrix", "result": []}}, "invalid_response"),
        ({"status": "success", "data": {"resultType": "streams", "result": []}}, "empty_result"),
        ({
            "status": "success",
            "data": {"resultType": "streams", "result": [{"stream": {}, "values": [["only-one"]]}]},
        }, "invalid_response"),
    ],
)
def test_loki_protocol_failures_degrade_with_reason(telemetry_server, payload, reason) -> None:
    telemetry_server.body = response(payload)
    primary = LiveObservabilityProvider(
        None,
        LokiAdapter(HttpJsonClient(HttpJsonSettings(base_url=telemetry_server.url)), "{job=\"api\"}"),
    )
    result = FallbackObservabilityProvider(
        primary, ScenarioObservabilityProvider()
    ).search_logs(get_scenario("auth-clock-skew"))

    assert result.payload["collection"]["reason_code"] == reason


def test_invalid_json_and_response_size_are_rejected(telemetry_server) -> None:
    telemetry_server.body = b"not-json"
    client = HttpJsonClient(HttpJsonSettings(base_url=telemetry_server.url))
    with pytest.raises(TelemetryError, match="invalid JSON") as invalid:
        client.get("/api/v1/query", {"query": "up"})
    assert invalid.value.code == "invalid_response"

    telemetry_server.body = b"{" + (b"x" * 50)
    small = HttpJsonClient(HttpJsonSettings(base_url=telemetry_server.url, max_response_bytes=10))
    with pytest.raises(TelemetryError) as oversized:
        small.get("/api/v1/query", {"query": "up"})
    assert oversized.value.code == "response_too_large"


@pytest.mark.parametrize(
    "settings",
    [
        HttpJsonSettings(base_url="file:///tmp/metrics"),
        HttpJsonSettings(base_url="https://user:secret@example.com"),
        HttpJsonSettings(base_url="https://example.com", bearer_token="a", username="b", password="c"),
        HttpJsonSettings(base_url="https://example.com", username="reader"),
    ],
)
def test_unsafe_or_ambiguous_http_configuration_is_rejected(settings) -> None:
    with pytest.raises(TelemetryError) as error:
        HttpJsonClient(settings)
    assert error.value.code == "invalid_configuration"


@pytest.mark.parametrize(
    "raised,code,retryable",
    [
        (socket.timeout(), "timeout", True),
        (URLError("refused"), "connection_error", True),
    ],
)
def test_transport_failures_are_sanitized(monkeypatch, raised, code, retryable) -> None:
    monkeypatch.setattr(observability, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(raised))
    client = HttpJsonClient(HttpJsonSettings(base_url="https://telemetry.invalid"))

    with pytest.raises(TelemetryError) as error:
        client.get("/api/v1/query", {"query": "up"})

    assert error.value.code == code
    assert error.value.retryable is retryable
    assert "telemetry.invalid" not in str(error.value)


def test_build_provider_defaults_to_mock(monkeypatch) -> None:
    monkeypatch.delenv("INCIDENT_LAB_TELEMETRY_MODE", raising=False)
    assert isinstance(build_observability_provider(), ScenarioObservabilityProvider)


def test_live_builder_uses_environment_and_falls_back_when_unconfigured(monkeypatch) -> None:
    monkeypatch.setenv("INCIDENT_LAB_TELEMETRY_MODE", "live")
    monkeypatch.delenv("INCIDENT_LAB_PROMETHEUS_URL", raising=False)
    monkeypatch.delenv("INCIDENT_LAB_LOKI_URL", raising=False)

    provider = build_observability_provider()
    metrics = provider.query_metrics(get_scenario("auth-clock-skew"))
    logs = provider.search_logs(get_scenario("auth-clock-skew"))

    assert metrics.payload["collection"]["reason_code"] == "not_configured"
    assert logs.payload["collection"]["reason_code"] == "not_configured"


def test_live_builder_can_disable_fallback(monkeypatch) -> None:
    monkeypatch.setenv("INCIDENT_LAB_TELEMETRY_MODE", "live")
    monkeypatch.setenv("INCIDENT_LAB_TELEMETRY_FALLBACK", "false")
    monkeypatch.delenv("INCIDENT_LAB_PROMETHEUS_URL", raising=False)

    provider = build_observability_provider()
    with pytest.raises(TelemetryError) as error:
        provider.query_metrics(get_scenario("auth-clock-skew"))
    assert error.value.code == "not_configured"


@pytest.mark.parametrize(
    "name,value",
    [
        ("INCIDENT_LAB_TELEMETRY_MODE", "unknown"),
        ("INCIDENT_LAB_TELEMETRY_FALLBACK", "sometimes"),
        ("INCIDENT_LAB_PROMETHEUS_TIMEOUT", "zero"),
        ("INCIDENT_LAB_PROMETHEUS_STEP_SECONDS", "0"),
        ("INCIDENT_LAB_PROMETHEUS_QUERIES_JSON", "{"),
        ("INCIDENT_LAB_PROMETHEUS_QUERIES_JSON", "[]"),
        ("INCIDENT_LAB_PROMETHEUS_QUERIES_JSON", '{"":"up"}'),
    ],
)
def test_invalid_environment_configuration_fails_closed(monkeypatch, name, value) -> None:
    monkeypatch.setenv("INCIDENT_LAB_TELEMETRY_MODE", "live")
    monkeypatch.setenv("INCIDENT_LAB_PROMETHEUS_URL", "https://prometheus.example.com")
    monkeypatch.setenv(name, value)

    with pytest.raises(TelemetryError) as error:
        build_observability_provider()
    assert error.value.code == "invalid_configuration"


def test_custom_query_json_and_loki_tenant_are_loaded(monkeypatch) -> None:
    monkeypatch.setenv("INCIDENT_LAB_TELEMETRY_MODE", "live")
    monkeypatch.setenv("INCIDENT_LAB_PROMETHEUS_URL", "https://prom.example.com")
    monkeypatch.setenv("INCIDENT_LAB_LOKI_URL", "https://loki.example.com")
    monkeypatch.setenv("INCIDENT_LAB_LOKI_TENANT_ID", "tenant-b")
    monkeypatch.setenv("INCIDENT_LAB_PROMETHEUS_QUERIES_JSON", '{"queue":"queue_depth{service=\\"$service\\"}"}')

    provider = build_observability_provider()

    assert isinstance(provider, FallbackObservabilityProvider)
    live = provider.primary
    assert live.prometheus.queries == {"queue": 'queue_depth{service="$service"}'}
    assert live.loki.client.settings.extra_headers == {"X-Scope-OrgID": "tenant-b"}
