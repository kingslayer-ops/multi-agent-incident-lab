from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Diagnosis:
    root_cause: str
    confidence: float
    rationale: str
    token_estimate: int
    provider: str
    fallback: tuple[str, str, str] | None = None


class IntelligenceProvider(Protocol):
    name: str

    def diagnose(self, scenario: dict[str, Any], evidence: list[dict[str, Any]]) -> Diagnosis: ...


class MockIntelligenceProvider:
    """Deterministic provider used for demos, tests, and offline evaluation."""

    name = "deterministic-mock-v1"

    SIGNATURES = {
        "connection from pool": "database connection pool exhaustion",
        "unbounded": "unbounded inventory snapshot cache",
        "strict_coupon_expiry": "strict coupon expiry feature flag regression",
        "certificate has expired": "expired service TLS certificate",
        "redis moved": "stale Redis cluster topology cache",
        "deadlock detected": "database deadlock caused by inconsistent lock ordering",
        "rate limit exceeded": "notification provider rate-limit exhaustion",
        "schema validation failed": "incompatible event schema deployment",
        "dns resolution failed": "service discovery DNS failure",
        "disk quota exceeded": "log volume disk exhaustion",
        "clock skew": "node clock skew invalidating authentication tokens",
        "thread pool rejected": "worker thread pool saturation",
    }

    def diagnose(self, scenario: dict[str, Any], evidence: list[dict[str, Any]]) -> Diagnosis:
        evidence_kinds = {item["kind"] for item in evidence}
        required = {"metric", "log", "change"}
        coverage = len(required & evidence_kinds) / len(required)
        confidence = round(0.58 + 0.36 * coverage, 2)
        searchable = json.dumps(evidence, ensure_ascii=False).lower()
        root_cause = next(
            (cause for signature, cause in self.SIGNATURES.items() if signature in searchable),
            "insufficient evidence for a reliable diagnosis",
        )
        return Diagnosis(
            root_cause=root_cause,
            confidence=confidence,
            rationale="The leading hypothesis is supported by correlated metrics, logs, and a recent change.",
            token_estimate=186,
            provider=self.name,
        )


class OpenAICompatibleProvider:
    """Minimal OpenAI-compatible structured diagnosis client with no SDK dependency."""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = 12.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.name = f"openai-compatible:{model}"

    def diagnose(self, scenario: dict[str, Any], evidence: list[dict[str, Any]]) -> Diagnosis:
        prompt = {
            "incident": scenario["summary"].model_dump(mode="json"),
            "evidence": evidence,
            "instruction": "Return JSON with root_cause, confidence (0..1), and rationale. Use only cited evidence.",
        }
        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            }
        ).encode()
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
            content = payload["choices"][0]["message"]["content"]
            result = json.loads(content)
            root_cause = str(result["root_cause"]).strip()
            confidence = float(result["confidence"])
            rationale = str(result["rationale"]).strip()
            if not root_cause or not rationale or not 0 <= confidence <= 1:
                raise ValueError("invalid structured diagnosis")
            return Diagnosis(root_cause, confidence, rationale, 300, self.name)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            raise RuntimeError(f"model diagnosis failed: {type(exc).__name__}") from exc


class FallbackProvider:
    def __init__(self, primary: IntelligenceProvider, fallback: IntelligenceProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name} -> {fallback.name}"

    def diagnose(self, scenario: dict[str, Any], evidence: list[dict[str, Any]]) -> Diagnosis:
        try:
            return self.primary.diagnose(scenario, evidence)
        except Exception as exc:
            diagnosis = self.fallback.diagnose(scenario, evidence)
            return Diagnosis(
                diagnosis.root_cause,
                diagnosis.confidence,
                diagnosis.rationale,
                diagnosis.token_estimate,
                diagnosis.provider,
                (self.primary.name, self.fallback.name, str(exc)),
            )


def build_provider() -> IntelligenceProvider:
    if os.getenv("INCIDENT_LAB_LLM_MODE", "mock").lower() != "openai-compatible":
        return MockIntelligenceProvider()
    primary = OpenAICompatibleProvider(
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.getenv("OPENAI_API_KEY", ""),
        model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        timeout=float(os.getenv("INCIDENT_LAB_LLM_TIMEOUT", "12")),
    )
    return FallbackProvider(primary, MockIntelligenceProvider())
