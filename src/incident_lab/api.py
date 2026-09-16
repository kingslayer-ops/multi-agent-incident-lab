from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .engine import IncidentEngine, dashboard_snapshot
from .models import ApprovalRequest, CreateIncidentRequest, EvaluationReport, Incident, IncidentStatus, RejectionRequest, ScenarioSummary, WorkflowStatus
from .scenarios import get_scenario, list_scenarios
from .runtime import WorkNotifier, build_notifier, build_runtime_stores
from .store import IncidentStore
from .workflow_store import InvalidTransitionError, TERMINAL, WORKFLOW_VERSION, WorkflowStore


def create_app(
    store: IncidentStore | None = None,
    workflow_store: WorkflowStore | None = None,
    notifier: WorkNotifier | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Multi-Agent Incident Lab API",
        version=__version__,
        description="Durable, evidence-driven multi-agent incident response workbench.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if store is None and workflow_store is None:
        runtime = build_runtime_stores()
        durable_store = runtime.incidents
        workflow = runtime.workflow
        storage_backend = runtime.backend
    elif store is not None and workflow_store is not None:
        durable_store = store
        workflow = workflow_store
        storage_backend = "injected"
    else:
        raise ValueError("store and workflow_store must be injected together")
    work_notifier = notifier or build_notifier()
    engine = IncidentEngine(store=durable_store)
    app.state.engine = engine
    app.state.workflow_store = workflow

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "provider": engine.provider.name,
            "workflow_version": WORKFLOW_VERSION,
            "storage": storage_backend,
            "notifier": work_notifier.name,
        }

    def notify_worker() -> None:
        try:
            work_notifier.notify()
        except Exception:  # Redis is a wake-up optimization; PostgreSQL remains the source of truth.
            return None

    @app.get("/api/scenarios", response_model=list[ScenarioSummary])
    def scenarios() -> list[ScenarioSummary]:
        return list_scenarios()

    @app.post("/api/incidents", response_model=Incident, status_code=202)
    def create_incident(request: CreateIncidentRequest) -> Incident:
        try:
            scenario = get_scenario(request.scenario_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        summary = scenario["summary"]
        incident = Incident(
            scenario_id=request.scenario_id,
            title=summary.title,
            service=summary.service,
            severity=summary.severity,
            symptom=summary.symptom,
            status=IncidentStatus.QUEUED,
        )
        created = workflow.create_run(incident)
        notify_worker()
        return created

    @app.get("/api/incidents", response_model=list[Incident])
    def incidents() -> list[Incident]:
        return durable_store.list_incidents()

    @app.get("/api/incidents/{incident_id}", response_model=Incident)
    def incident(incident_id: str) -> Incident:
        try:
            return durable_store.get_incident(incident_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Incident not found") from exc

    @app.get("/api/incidents/{incident_id}/workflow")
    def workflow_detail(incident_id: str) -> dict[str, object]:
        try:
            run = workflow.get_run_for_incident(incident_id)
            return {"run": run, "steps": workflow.list_steps(run.id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Workflow not found") from exc

    @app.get("/api/incidents/{incident_id}/events")
    def incident_events(
        incident_id: str,
        last_event_id: int = Header(0, alias="Last-Event-ID"),
        after_event_id: int = Query(0, alias="after", ge=0),
        follow: bool = Query(True),
    ) -> StreamingResponse:
        try:
            run = workflow.get_run_for_incident(incident_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Incident not found") from exc

        def stream():
            cursor = max(last_event_id, after_event_id)
            idle_ticks = 0
            while True:
                events = workflow.list_events(run.id, cursor)
                if events:
                    idle_ticks = 0
                    for event in events:
                        cursor = event.id
                        data = json.dumps({
                            "run_id": event.workflow_run_id,
                            "incident_id": event.incident_id,
                            "step_key": event.step_key,
                            "sequence": event.sequence_number,
                            "payload": event.payload,
                        })
                        yield f"id: {event.id}\nevent: {event.event_type}\ndata: {data}\n\n"
                elif not follow:
                    break
                else:
                    idle_ticks += 1
                    if idle_ticks >= 20:
                        yield ": heartbeat\n\n"
                        idle_ticks = 0
                    time.sleep(0.25)
                current = workflow.get_run_for_incident(incident_id)
                if WorkflowStatus(current.status) in TERMINAL and not workflow.list_events(run.id, cursor):
                    break

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/incidents/{incident_id}/approve", response_model=Incident)
    def approve(incident_id: str, request: ApprovalRequest) -> Incident:
        try:
            approved = workflow.approve(incident_id, request.action_id, request.approved_by)
            notify_worker()
            return approved
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Incident, action, or approval request not found") from exc
        except InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/incidents/{incident_id}/cancel", response_model=Incident)
    def cancel(incident_id: str) -> Incident:
        try:
            return workflow.cancel(incident_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Incident not found") from exc

    @app.post("/api/incidents/{incident_id}/reject", response_model=Incident)
    def reject(incident_id: str, request: RejectionRequest) -> Incident:
        try:
            return workflow.reject(
                incident_id, request.action_id, request.rejected_by, request.reason
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Incident, action, or approval request not found") from exc
        except InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/incidents/{incident_id}/retry", response_model=Incident)
    def retry(incident_id: str) -> Incident:
        try:
            retried = workflow.retry(incident_id)
            notify_worker()
            return retried
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Incident not found") from exc
        except InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/evaluations/run", response_model=EvaluationReport)
    def run_evaluation() -> EvaluationReport:
        return engine.evaluate()

    @app.get("/api/evaluations/latest", response_model=EvaluationReport | None)
    def latest_evaluation() -> EvaluationReport | None:
        return durable_store.latest_evaluation()

    @app.get("/api/dashboard")
    def dashboard() -> dict[str, object]:
        return dashboard_snapshot(durable_store)

    @app.get("/api/incidents/{incident_id}/postmortem", response_class=PlainTextResponse)
    def postmortem(incident_id: str) -> str:
        try:
            current = durable_store.get_incident(incident_id)
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
