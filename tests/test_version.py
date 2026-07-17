import json
import tomllib
from importlib.metadata import version
from pathlib import Path

from incident_lab import __version__
from incident_lab.api import create_app
from incident_lab.store import InMemoryStore


ROOT = Path(__file__).resolve().parents[1]


def test_all_runtime_versions_match_project_metadata() -> None:
    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    frontend_version = json.loads(
        (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )["version"]

    assert project_version == "1.1.1"
    assert version("multi-agent-incident-lab") == project_version
    assert __version__ == project_version
    assert create_app(store=InMemoryStore()).version == project_version
    assert frontend_version == project_version
