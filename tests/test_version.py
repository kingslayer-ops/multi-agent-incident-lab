import json
import tomllib
from importlib.metadata import version
from pathlib import Path

from incident_lab import __version__
from incident_lab.api import create_app
from incident_lab.store import SQLiteStore
from incident_lab.workflow_store import WorkflowStore


ROOT = Path(__file__).resolve().parents[1]


def test_all_runtime_versions_match_project_metadata(tmp_path) -> None:
    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    frontend_version = json.loads(
        (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )["version"]
    e2e_version = json.loads(
        (ROOT / "e2e" / "package.json").read_text(encoding="utf-8")
    )["version"]

    assert project_version == "1.3.2"
    assert version("multi-agent-incident-lab") == project_version
    assert __version__ == project_version
    database = tmp_path / "versions.db"
    store = SQLiteStore(database)
    assert create_app(store=store, workflow_store=WorkflowStore(database, project_version)).version == project_version
    assert frontend_version == project_version
    assert e2e_version == project_version
