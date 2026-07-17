from __future__ import annotations

from typing import Any, Callable

from .observability import ObservabilityProvider, ScenarioObservabilityProvider, ToolResult


class ScenarioToolbox:
    """Allow-listed incident tools with a pluggable read-only observability source."""

    def __init__(
        self,
        scenario: dict[str, Any],
        observability: ObservabilityProvider | None = None,
    ) -> None:
        self._scenario = scenario
        self._observability = observability or ScenarioObservabilityProvider()
        self._tools: dict[str, Callable[[], ToolResult]] = {
            "query_metrics": self.query_metrics,
            "search_logs": self.search_logs,
            "list_recent_changes": self.list_recent_changes,
        }

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def call(self, name: str) -> ToolResult:
        if name not in self._tools:
            raise ValueError(f"Tool is not allow-listed: {name}")
        return self._tools[name]()

    def query_metrics(self) -> ToolResult:
        return self._observability.query_metrics(self._scenario)

    def search_logs(self) -> ToolResult:
        return self._observability.search_logs(self._scenario)

    def list_recent_changes(self) -> ToolResult:
        changes = self._scenario["changes"]
        return ToolResult(
            source="deployments://demo",
            summary=f"Found {len(changes)} change(s) inside the incident window.",
            payload={"changes": changes},
        )


class RemediationExecutor:
    """A sandbox executor: it records an action but never touches real systems."""

    SAFE_PREFIX = "incident-lab "

    def execute(self, command_preview: str, idempotency_key: str | None = None) -> str:
        if not command_preview.startswith(self.SAFE_PREFIX):
            raise PermissionError("Only Incident Lab sandbox commands are permitted")
        suffix = f" Idempotency key: {idempotency_key}." if idempotency_key else ""
        return f"Sandbox action completed; synthetic telemetry returned to baseline.{suffix}"
