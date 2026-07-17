from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .engine import IncidentEngine, dashboard_snapshot
from .models import ApprovalRequest, CreateIncidentRequest, EvaluationReport, Incident, ScenarioSummary
from .scenarios import list_scenarios
from .store import IncidentStore, SQLiteStore


def create_app(store: IncidentStore | None = None) -> FastAPI:
    app = FastAPI(
        title="OpsPilot API",
        version="1.0.0",
        description="Evidence-driven multi-agent incident response workbench.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    durable_store = store or SQLiteStore(os.getenv("OPSPILOT_DB_PATH", "data/opspilot.db"))
    engine = IncidentEngine(store=durable_store)
    app.state.engine = engine

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "provider": engine.provider.name}

    @app.get("/api/scenarios", response_model=list[ScenarioSummary])
    def scenarios() -> list[ScenarioSummary]:
        return list_scenarios()

    @app.post("/api/incidents", response_model=Incident, status_code=201)
    def create_incident(request: CreateIncidentRequest) -> Incident:
        try:
            return engine.investigate(request.scenario_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/incidents", response_model=list[Incident])
    def incidents() -> list[Incident]:
        return engine.store.list_incidents()

    @app.get("/api/incidents/{incident_id}", response_model=Incident)
    def incident(incident_id: str) -> Incident:
        try:
            return engine.store.get_incident(incident_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Incident not found") from exc

    @app.get("/api/incidents/{incident_id}/events")
    def incident_events(incident_id: str) -> StreamingResponse:
        try:
            current = engine.store.get_incident(incident_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Incident not found") from exc

        def stream():
            for sequence, step in enumerate(current.trace, start=1):
                payload = json.dumps(
                    {"sequence": sequence, "type": "agent.step", "data": step.model_dump(mode="json")}
                )
                yield f"id: {sequence}\nevent: agent.step\ndata: {payload}\n\n"
            yield f"event: incident.state\ndata: {json.dumps({'status': current.status})}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/incidents/{incident_id}/approve", response_model=Incident)
    def approve(incident_id: str, request: ApprovalRequest) -> Incident:
        try:
            return engine.approve_and_execute(incident_id, request.action_id, request.approved_by)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Incident or action not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/api/evaluations/run", response_model=EvaluationReport)
    def run_evaluation() -> EvaluationReport:
        return engine.evaluate()

    @app.get("/api/evaluations/latest", response_model=EvaluationReport | None)
    def latest_evaluation() -> EvaluationReport | None:
        return engine.store.latest_evaluation()

    @app.get("/api/dashboard")
    def dashboard() -> dict[str, object]:
        return dashboard_snapshot(engine.store)

    @app.get("/api/incidents/{incident_id}/postmortem", response_class=PlainTextResponse)
    def postmortem(incident_id: str) -> str:
        try:
            current = engine.store.get_incident(incident_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Incident not found") from exc
        if not current.postmortem:
            raise HTTPException(status_code=409, detail="Postmortem is available after remediation")
        return current.postmortem

    web_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if web_dist.exists():
        assets = web_dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            candidate = web_dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(web_dist / "index.html")

    return app


app = create_app()
