from typing import Any

import json

from incident_lab.provider import (
    Diagnosis,
    FallbackProvider,
    MockIntelligenceProvider,
    OpenAICompatibleProvider,
    build_provider,
)


class BrokenProvider:
    name = "broken-provider"

    def diagnose(self, scenario: dict[str, Any], evidence: list[dict[str, Any]]) -> Diagnosis:
        raise TimeoutError("upstream timed out")


def test_fallback_provider_records_degradation() -> None:
    provider = FallbackProvider(BrokenProvider(), MockIntelligenceProvider())
    diagnosis = provider.diagnose(
        {"summary": {}},
        [
            {"kind": "metric", "payload": {}},
            {"kind": "log", "payload": {"line": "DNS resolution failed"}},
            {"kind": "change", "payload": {}},
        ],
    )
    assert diagnosis.root_cause == "service discovery DNS failure"
    assert diagnosis.fallback is not None
    assert diagnosis.fallback[0] == "broken-provider"


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        content = json.dumps({"root_cause": "cache stampede", "confidence": 0.87, "rationale": "Correlated evidence."})
        return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


def test_openai_compatible_provider_parses_structured_response(monkeypatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())
    provider = OpenAICompatibleProvider(base_url="https://model.local/v1", api_key="test", model="test-model")
    diagnosis = provider.diagnose({"summary": MockSummary()}, [])
    assert diagnosis.root_cause == "cache stampede"
    assert diagnosis.confidence == 0.87


class MockSummary:
    def model_dump(self, mode: str):
        return {"title": "Synthetic incident"}


def test_build_provider_defaults_to_offline_mock(monkeypatch) -> None:
    monkeypatch.delenv("INCIDENT_LAB_LLM_MODE", raising=False)
    assert isinstance(build_provider(), MockIntelligenceProvider)
