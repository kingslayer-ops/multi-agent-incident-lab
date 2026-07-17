from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolResult:
    source: str
    summary: str
    payload: dict[str, Any]


class ScenarioToolbox:
    """Read-only incident tools backed by deterministic demo telemetry."""

    def __init__(self, scenario: dict[str, Any]) -> None:
        self._scenario = scenario
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
        metrics = self._scenario["metrics"]
        abnormal = [f"{key}={value}" for key, value in metrics.items()]
        return ToolResult(
            source="prometheus://demo",
            summary="Abnormal service indicators: " + ", ".join(abnormal),
            payload=metrics,
        )

    def search_logs(self) -> ToolResult:
        logs = self._scenario["logs"]
        return ToolResult(
            source="loki://demo",
            summary=f"Found {len(logs)} correlated error signatures.",
            payload={"lines": logs},
        )

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

    def execute(self, command_preview: str) -> str:
        if not command_preview.startswith(self.SAFE_PREFIX):
            raise PermissionError("Only Incident Lab sandbox commands are permitted")
        return "Sandbox action completed; synthetic telemetry returned to baseline."
