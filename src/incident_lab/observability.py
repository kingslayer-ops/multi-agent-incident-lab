from __future__ import annotations

import base64
import json
import os
import socket
import ssl
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .models import utc_now


@dataclass(frozen=True)
class ToolResult:
    source: str
    summary: str
    payload: dict[str, Any]


class ObservabilityProvider(Protocol):
    def query_metrics(self, scenario: dict[str, Any]) -> ToolResult: ...

    def search_logs(self, scenario: dict[str, Any]) -> ToolResult: ...


class TelemetryError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class EmptyTelemetryResult(TelemetryError):
    def __init__(self, provider: str) -> None:
        super().__init__("empty_result", f"{provider} returned no matching telemetry")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise TelemetryError("invalid_configuration", f"{name} must be true or false")


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise TelemetryError("invalid_configuration", f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise TelemetryError(
            "invalid_configuration", f"{name} must be between {minimum:g} and {maximum:g}"
        )
    return value


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise TelemetryError("invalid_configuration", f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise TelemetryError(
            "invalid_configuration", f"{name} must be between {minimum} and {maximum}"
        )
    return value


@dataclass(frozen=True)
class HttpJsonSettings:
    base_url: str
    timeout_seconds: float = 5
    bearer_token: str | None = None
    username: str | None = None
    password: str | None = None
    extra_headers: Mapping[str, str] | None = None
    max_response_bytes: int = 2_000_000


class HttpJsonClient:
    def __init__(self, settings: HttpJsonSettings) -> None:
        parsed = urlparse(settings.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise TelemetryError("invalid_configuration", "Telemetry URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise TelemetryError("invalid_configuration", "Credentials must not be embedded in telemetry URLs")
        if settings.bearer_token and (settings.username or settings.password):
            raise TelemetryError("invalid_configuration", "Choose either bearer or basic authentication")
        if bool(settings.username) != bool(settings.password):
            raise TelemetryError("invalid_configuration", "Basic authentication requires username and password")
        self.settings = settings

    def get(self, path: str, params: Mapping[str, str | int | float]) -> dict[str, Any]:
        url = f"{self.settings.base_url.rstrip('/')}/{path.lstrip('/')}?{urlencode(params)}"
        headers = {"Accept": "application/json", "User-Agent": "multi-agent-incident-lab/1.3"}
        if self.settings.extra_headers:
            headers.update(self.settings.extra_headers)
        if self.settings.bearer_token:
            headers["Authorization"] = f"Bearer {self.settings.bearer_token}"
        elif self.settings.username and self.settings.password:
            credential = base64.b64encode(
                f"{self.settings.username}:{self.settings.password}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {credential}"
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(
                request, timeout=self.settings.timeout_seconds, context=ssl.create_default_context()
            ) as response:
                body = response.read(self.settings.max_response_bytes + 1)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                code, retryable = "authentication_error", False
            elif exc.code == 429:
                code, retryable = "rate_limited", True
            elif exc.code >= 500:
                code, retryable = "service_unavailable", True
            else:
                code, retryable = "http_error", False
            raise TelemetryError(
                code, f"Telemetry endpoint returned HTTP {exc.code}", retryable=retryable
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise TelemetryError("timeout", "Telemetry request timed out", retryable=True) from exc
        except (URLError, OSError) as exc:
            raise TelemetryError("connection_error", "Telemetry endpoint is unavailable", retryable=True) from exc
        if len(body) > self.settings.max_response_bytes:
            raise TelemetryError("response_too_large", "Telemetry response exceeded the configured limit")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelemetryError("invalid_response", "Telemetry endpoint returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise TelemetryError("invalid_response", "Telemetry response must be a JSON object")
        return payload


class ScenarioObservabilityProvider:
    """Deterministic telemetry used by default and as the live-provider fallback."""

    def query_metrics(self, scenario: dict[str, Any]) -> ToolResult:
        metrics = scenario["metrics"]
        abnormal = [f"{key}={value}" for key, value in metrics.items()]
        return ToolResult(
            source="prometheus://demo",
            summary="Abnormal service indicators: " + ", ".join(abnormal),
            payload=metrics,
        )

    def search_logs(self, scenario: dict[str, Any]) -> ToolResult:
        logs = scenario["logs"]
        return ToolResult(
            source="loki://demo",
            summary=f"Found {len(logs)} correlated error signatures.",
            payload={"lines": logs},
        )


DEFAULT_PROMETHEUS_QUERIES = {
    "request_rate": 'sum(rate(http_requests_total{service="$service"}[5m]))',
    "error_rate": (
        'sum(rate(http_requests_total{service="$service",status=~"5.."}[5m])) '
        '/ clamp_min(sum(rate(http_requests_total{service="$service"}[5m])), 1)'
    ),
    "p95_latency_seconds": (
        'histogram_quantile(0.95, sum by (le) '
        '(rate(http_request_duration_seconds_bucket{service="$service"}[5m])))'
    ),
}


def _queries_from_env() -> dict[str, str]:
    raw = os.getenv("INCIDENT_LAB_PROMETHEUS_QUERIES_JSON")
    if not raw:
        return DEFAULT_PROMETHEUS_QUERIES.copy()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TelemetryError("invalid_configuration", "Prometheus queries must be valid JSON") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise TelemetryError("invalid_configuration", "Prometheus queries must be a non-empty object")
    queries: dict[str, str] = {}
    for name, query in parsed.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(query, str) or not query.strip():
            raise TelemetryError("invalid_configuration", "Prometheus query names and expressions must be strings")
        queries[name] = query
    return queries


def _service_name(scenario: dict[str, Any]) -> str:
    value = str(scenario["summary"].service)
    return value.replace("\\", "\\\\").replace('"', '\\"')


class PrometheusAdapter:
    def __init__(
        self,
        client: HttpJsonClient,
        queries: Mapping[str, str],
        *,
        lookback_seconds: int = 900,
        step_seconds: int = 60,
        series_limit: int = 100,
    ) -> None:
        self.client = client
        self.queries = dict(queries)
        self.lookback_seconds = lookback_seconds
        self.step_seconds = step_seconds
        self.series_limit = series_limit

    def query_metrics(self, scenario: dict[str, Any]) -> ToolResult:
        end = utc_now()
        start = end - timedelta(seconds=self.lookback_seconds)
        normalized: list[dict[str, Any]] = []
        warnings: list[str] = []
        sample_count = 0
        for name, template in self.queries.items():
            query = template.replace("$service", _service_name(scenario))
            response = self.client.get(
                "/api/v1/query_range",
                {
                    "query": query,
                    "start": start.timestamp(),
                    "end": end.timestamp(),
                    "step": self.step_seconds,
                    "limit": self.series_limit,
                },
            )
            data = _successful_data(response, "Prometheus")
            result_type = data.get("resultType")
            result = data.get("result")
            if result_type not in {"matrix", "vector"} or not isinstance(result, list):
                raise TelemetryError("invalid_response", "Prometheus returned an unsupported result type")
            for series in result:
                if not isinstance(series, dict) or not isinstance(series.get("metric"), dict):
                    raise TelemetryError("invalid_response", "Prometheus returned a malformed series")
                samples = series.get("values")
                if samples is None and "value" in series:
                    samples = [series["value"]]
                if not isinstance(samples, list):
                    raise TelemetryError("invalid_response", "Prometheus series has no samples")
                sample_count += len(samples)
                normalized.append({"query": name, "labels": series["metric"], "samples": samples})
            response_warnings = response.get("warnings", [])
            if isinstance(response_warnings, list):
                warnings.extend(str(item) for item in response_warnings)
        if not normalized or sample_count == 0:
            raise EmptyTelemetryResult("Prometheus")
        return ToolResult(
            source=f"{self.client.settings.base_url.rstrip('/')}/api/v1/query_range",
            summary=f"Prometheus returned {len(normalized)} series and {sample_count} samples.",
            payload={
                "window": {"start": start.isoformat(), "end": end.isoformat()},
                "series": normalized,
                "warnings": warnings,
                "collection": {"mode": "live", "provider": "prometheus"},
            },
        )


class LokiAdapter:
    def __init__(
        self,
        client: HttpJsonClient,
        query_template: str,
        *,
        lookback_seconds: int = 900,
        line_limit: int = 100,
    ) -> None:
        if not query_template.strip():
            raise TelemetryError("invalid_configuration", "Loki query template must not be empty")
        self.client = client
        self.query_template = query_template
        self.lookback_seconds = lookback_seconds
        self.line_limit = line_limit

    def search_logs(self, scenario: dict[str, Any]) -> ToolResult:
        end = utc_now()
        start = end - timedelta(seconds=self.lookback_seconds)
        query = self.query_template.replace("$service", _service_name(scenario))
        response = self.client.get(
            "/loki/api/v1/query_range",
            {
                "query": query,
                "start": int(start.timestamp() * 1_000_000_000),
                "end": int(end.timestamp() * 1_000_000_000),
                "limit": self.line_limit,
                "direction": "backward",
            },
        )
        data = _successful_data(response, "Loki")
        if data.get("resultType") != "streams" or not isinstance(data.get("result"), list):
            raise TelemetryError("invalid_response", "Loki returned an unsupported result type")
        lines: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for stream in data["result"]:
            if not isinstance(stream, dict) or not isinstance(stream.get("stream"), dict):
                raise TelemetryError("invalid_response", "Loki returned a malformed stream")
            values = stream.get("values")
            if not isinstance(values, list):
                raise TelemetryError("invalid_response", "Loki stream has no values")
            labels = stream["stream"]
            label_key = json.dumps(labels, sort_keys=True)
            for value in values:
                if not isinstance(value, list) or len(value) != 2:
                    raise TelemetryError("invalid_response", "Loki returned a malformed log entry")
                timestamp, line = str(value[0]), str(value[1])
                key = (timestamp, line, label_key)
                if key in seen:
                    continue
                seen.add(key)
                lines.append({"timestamp_ns": timestamp, "line": line, "labels": labels})
        if not lines:
            raise EmptyTelemetryResult("Loki")
        return ToolResult(
            source=f"{self.client.settings.base_url.rstrip('/')}/loki/api/v1/query_range",
            summary=f"Loki returned {len(lines)} correlated log lines.",
            payload={
                "window": {"start": start.isoformat(), "end": end.isoformat()},
                "lines": lines,
                "stats": data.get("stats", {}),
                "collection": {"mode": "live", "provider": "loki"},
            },
        )


def _successful_data(response: dict[str, Any], provider: str) -> dict[str, Any]:
    if response.get("status") != "success":
        error_type = str(response.get("errorType", "query_error"))
        raise TelemetryError("query_error", f"{provider} rejected the query ({error_type})")
    data = response.get("data")
    if not isinstance(data, dict):
        raise TelemetryError("invalid_response", f"{provider} response has no data object")
    return data


class LiveObservabilityProvider:
    def __init__(self, prometheus: PrometheusAdapter | None, loki: LokiAdapter | None) -> None:
        self.prometheus = prometheus
        self.loki = loki

    def query_metrics(self, scenario: dict[str, Any]) -> ToolResult:
        if self.prometheus is None:
            raise TelemetryError("not_configured", "Prometheus URL is not configured")
        return self.prometheus.query_metrics(scenario)

    def search_logs(self, scenario: dict[str, Any]) -> ToolResult:
        if self.loki is None:
            raise TelemetryError("not_configured", "Loki URL is not configured")
        return self.loki.search_logs(scenario)


class FallbackObservabilityProvider:
    def __init__(self, primary: ObservabilityProvider, fallback: ObservabilityProvider) -> None:
        self.primary = primary
        self.fallback = fallback

    def query_metrics(self, scenario: dict[str, Any]) -> ToolResult:
        return self._call("prometheus", self.primary.query_metrics, self.fallback.query_metrics, scenario)

    def search_logs(self, scenario: dict[str, Any]) -> ToolResult:
        return self._call("loki", self.primary.search_logs, self.fallback.search_logs, scenario)

    @staticmethod
    def _call(provider: str, primary, fallback, scenario: dict[str, Any]) -> ToolResult:
        try:
            return primary(scenario)
        except TelemetryError as exc:
            result = fallback(scenario)
            payload = dict(result.payload)
            payload["collection"] = {
                "mode": "fallback",
                "provider": provider,
                "reason_code": exc.code,
                "retryable": exc.retryable,
            }
            return replace(
                result,
                source=f"{provider}://fallback/demo",
                summary=f"{result.summary} Live {provider} unavailable; deterministic fallback used.",
                payload=payload,
            )


def _client_from_env(prefix: str, *, extra_headers: Mapping[str, str] | None = None) -> HttpJsonClient | None:
    base_url = os.getenv(f"INCIDENT_LAB_{prefix}_URL", "").strip()
    if not base_url:
        return None
    return HttpJsonClient(HttpJsonSettings(
        base_url=base_url,
        timeout_seconds=_env_float(
            f"INCIDENT_LAB_{prefix}_TIMEOUT", 5, minimum=0.1, maximum=60
        ),
        bearer_token=os.getenv(f"INCIDENT_LAB_{prefix}_BEARER_TOKEN") or None,
        username=os.getenv(f"INCIDENT_LAB_{prefix}_USERNAME") or None,
        password=os.getenv(f"INCIDENT_LAB_{prefix}_PASSWORD") or None,
        extra_headers=extra_headers,
    ))


def build_observability_provider() -> ObservabilityProvider:
    mode = os.getenv("INCIDENT_LAB_TELEMETRY_MODE", "mock").strip().lower()
    mock = ScenarioObservabilityProvider()
    if mode == "mock":
        return mock
    if mode != "live":
        raise TelemetryError("invalid_configuration", "INCIDENT_LAB_TELEMETRY_MODE must be mock or live")
    prometheus_client = _client_from_env("PROMETHEUS")
    tenant = os.getenv("INCIDENT_LAB_LOKI_TENANT_ID", "").strip()
    loki_client = _client_from_env("LOKI", extra_headers={"X-Scope-OrgID": tenant} if tenant else None)
    primary = LiveObservabilityProvider(
        PrometheusAdapter(
            prometheus_client,
            _queries_from_env(),
            lookback_seconds=_env_int(
                "INCIDENT_LAB_PROMETHEUS_LOOKBACK_SECONDS", 900, minimum=60, maximum=86400
            ),
            step_seconds=_env_int(
                "INCIDENT_LAB_PROMETHEUS_STEP_SECONDS", 60, minimum=1, maximum=3600
            ),
            series_limit=_env_int(
                "INCIDENT_LAB_PROMETHEUS_SERIES_LIMIT", 100, minimum=1, maximum=1000
            ),
        ) if prometheus_client else None,
        LokiAdapter(
            loki_client,
            os.getenv(
                "INCIDENT_LAB_LOKI_QUERY_TEMPLATE",
                '{service_name="$service"} |~ "(?i)(error|exception|timeout|failed)"',
            ) or '{service_name="$service"} |~ "(?i)(error|exception|timeout|failed)"',
            lookback_seconds=_env_int(
                "INCIDENT_LAB_LOKI_LOOKBACK_SECONDS", 900, minimum=60, maximum=86400
            ),
            line_limit=_env_int("INCIDENT_LAB_LOKI_LINE_LIMIT", 100, minimum=1, maximum=1000),
        ) if loki_client else None,
    )
    if _env_bool("INCIDENT_LAB_TELEMETRY_FALLBACK", True):
        return FallbackObservabilityProvider(primary, mock)
    return primary
