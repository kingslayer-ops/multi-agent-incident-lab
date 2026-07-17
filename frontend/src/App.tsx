import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type { Dashboard, Evaluation, Incident, Scenario } from "./types";

const pct = (value: number | null) => (value === null ? "—" : `${Math.round(value * 100)}%`);
const money = (value: number) => `$${value.toFixed(4)}`;
const formatPct = (value: number | null) => (value === null ? "—" : pct(value));

function StatusPill({ status }: { status: string }) {
  return <span className={`status status-${status.replaceAll("_", "-")}`}>{status.replaceAll("_", " ")}</span>;
}

function App() {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [active, setActive] = useState<Incident | null>(null);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null);
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [provider, setProvider] = useState("initializing");
  const [streamStatus, setStreamStatus] = useState("idle");
  const [workerWaiting, setWorkerWaiting] = useState(false);
  const [rejectionReason, setRejectionReason] = useState("Risk is not acceptable for this incident");
  const [postmortem, setPostmortem] = useState<string | null>(null);
  const lastEventIds = useRef(new Map<string, number>());
  const lastProgressAt = useRef(Date.now());
  const activeRefreshes = useRef(new Map<string, Promise<void>>());
  const refreshAgain = useRef(new Set<string>());

  const refresh = async () => {
    const [nextIncidents, nextDashboard] = await Promise.all([api.incidents(), api.dashboard()]);
    setIncidents(nextIncidents);
    setDashboard(nextDashboard);
  };

  useEffect(() => {
    Promise.all([api.scenarios(), api.incidents(), api.dashboard(), api.latestEvaluation(), api.health()])
      .then(([nextScenarios, nextIncidents, nextDashboard, nextEvaluation, health]) => {
        setScenarios(nextScenarios);
        setIncidents(nextIncidents);
        setDashboard(nextDashboard);
        setEvaluation(nextEvaluation);
        setActive(nextIncidents[0] ?? null);
        setProvider(health.provider);
      })
      .catch((reason: Error) => setError(reason.message));
  }, []);

  const terminal = active ? ["resolved", "failed", "cancelled"].includes(active.status) : false;

  useEffect(() => {
    const incidentId = active?.id;
    if (!incidentId || terminal) {
      setStreamStatus(incidentId ? "complete" : "idle");
      return;
    }
    let disposed = false;
    let source: EventSource | null = null;
    let reconnectTimer: number | null = null;
    let reconnectAttempt = 0;
    lastProgressAt.current = Date.now();
    setWorkerWaiting(false);

    const refreshIncident = () => {
      const existing = activeRefreshes.current.get(incidentId);
      if (existing) {
        refreshAgain.current.add(incidentId);
        return existing;
      }
      const task = (async () => {
        do {
          refreshAgain.current.delete(incidentId);
          const [nextIncident, nextIncidents, nextDashboard] = await Promise.all([
            api.incident(incidentId), api.incidents(), api.dashboard()
          ]);
          if (!disposed) {
            setActive(nextIncident);
            setIncidents(nextIncidents);
            setDashboard(nextDashboard);
          }
        } while (!disposed && refreshAgain.current.has(incidentId));
      })().catch((reason: Error) => {
        if (!disposed) setError(reason.message);
      }).finally(() => {
        activeRefreshes.current.delete(incidentId);
        if (disposed) refreshAgain.current.delete(incidentId);
      });
      activeRefreshes.current.set(incidentId, task);
      return task;
    };

    const connect = () => {
      if (disposed) return;
      const cursor = lastEventIds.current.get(incidentId) ?? 0;
      setStreamStatus(reconnectAttempt ? "reconnecting" : "connecting");
      source = new EventSource(`/api/incidents/${incidentId}/events?after=${cursor}`);
      source.onopen = () => {
        reconnectAttempt = 0;
        lastProgressAt.current = Date.now();
        setStreamStatus("live");
        setWorkerWaiting(false);
      };
      const eventNames = [
        "run.created", "step.started", "step.completed", "step.retry_scheduled",
        "step.lease_expired", "approval.requested", "approval.rejected", "run.resumed",
        "run.resolved", "run.failed", "run.cancel_requested", "run.cancelled"
      ];
      const update = (rawEvent: Event) => {
        const event = rawEvent as MessageEvent;
        const eventId = Number(event.lastEventId || 0);
        const previous = lastEventIds.current.get(incidentId) ?? 0;
        if (eventId && eventId <= previous) return;
        if (eventId) lastEventIds.current.set(incidentId, eventId);
        lastProgressAt.current = Date.now();
        setStreamStatus("live");
        setWorkerWaiting(false);
        void refreshIncident();
      };
      eventNames.forEach((name) => source?.addEventListener(name, update));
      source.onerror = () => {
        source?.close();
        if (disposed) return;
        reconnectAttempt += 1;
        setStreamStatus("reconnecting");
        const delay = Math.min(250 * (2 ** (reconnectAttempt - 1)), 4000);
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      disposed = true;
      source?.close();
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
    };
  }, [active?.id, terminal]);

  useEffect(() => {
    if (!active || terminal || active.status === "awaiting_approval" || streamStatus !== "live") {
      setWorkerWaiting(false);
      return;
    }
    const timer = window.setInterval(() => {
      if (Date.now() - lastProgressAt.current > 1500) {
        setWorkerWaiting(true);
      }
    }, 250);
    return () => window.clearInterval(timer);
  }, [active?.id, active?.status, streamStatus, terminal]);

  const runScenario = async (scenarioId: string) => {
    setLoading(scenarioId);
    setError(null);
    try {
      const incident = await api.createIncident(scenarioId);
      setActive(incident);
      setPostmortem(null);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Investigation failed");
    } finally {
      setLoading(null);
    }
  };

  const approve = async () => {
    if (!active?.actions[0]) return;
    setLoading("approve");
    setError(null);
    try {
      const incident = await api.approve(active.id, active.actions[0].id);
      setActive(incident);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Approval failed");
    } finally {
      setLoading(null);
    }
  };

  const runEvaluation = async () => {
    setLoading("evaluation");
    setError(null);
    try {
      setEvaluation(await api.evaluate());
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Evaluation failed");
    } finally {
      setLoading(null);
    }
  };

  const reject = async () => {
    if (!active?.actions[0] || rejectionReason.trim().length < 3) return;
    setLoading("reject");
    setError(null);
    try {
      setActive(await api.reject(active.id, active.actions[0].id, rejectionReason.trim()));
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Rejection failed");
    } finally {
      setLoading(null);
    }
  };

  const openPostmortem = async () => {
    if (!active) return;
    setLoading("postmortem");
    try {
      setPostmortem(await api.postmortem(active.id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Postmortem failed");
    } finally {
      setLoading(null);
    }
  };

  const cancel = async () => {
    if (!active) return;
    setLoading("cancel");
    try {
      setActive(await api.cancel(active.id));
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Cancellation failed");
    } finally {
      setLoading(null);
    }
  };

  const retry = async () => {
    if (!active) return;
    setLoading("retry");
    try {
      setActive(await api.retry(active.id));
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Retry failed");
    } finally {
      setLoading(null);
    }
  };

  const services = useMemo(() => dashboard?.services_healthy ?? 12, [dashboard]);

  return (
    <div className="app-shell" data-testid="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">IL</div>
          <div><strong>Incident Lab</strong><span>multi-agent response</span></div>
        </div>
        <nav>
          <a className="active" href="#overview"><span>01</span> Command center</a>
          <a href="#investigations"><span>02</span> Investigations</a>
          <a href="#evaluation"><span>03</span> Evaluation lab</a>
        </nav>
        <div className="runtime-card">
          <div className="pulse" />
          <div><strong>Runtime online</strong><span>{provider}</span></div>
        </div>
      </aside>

      <main>
        <header>
          <div>
            <p className="eyebrow">COMMAND CENTER / LIVE</p>
            <h1>Investigate with evidence.<br/><em>Act with control.</em></h1>
          </div>
          <div className="health-ring">
            <span>{services}/{dashboard?.services_total ?? 12}</span>
            <small>services healthy</small>
          </div>
        </header>

        {error && <div className="error-banner">{error}</div>}
        <div className={`connection-state connection-${streamStatus}`} data-testid="connection-state">
          {`Event stream: ${streamStatus}`}
        </div>
        {workerWaiting && (
          <div className="worker-recovery-state" data-testid="worker-recovery-state">
            Waiting for worker recovery
          </div>
        )}

        <section className="metrics" id="overview">
          <article><span>INCIDENTS</span><strong>{dashboard?.incidents_total ?? 0}</strong><small>synthetic investigations</small></article>
          <article><span>AWAITING APPROVAL</span><strong className="amber">{dashboard?.awaiting_approval ?? 0}</strong><small>human control gates</small></article>
          <article><span>ROOT CAUSE ACCURACY</span><strong className="cyan">{formatPct(dashboard?.root_cause_accuracy ?? null)}</strong><small>latest benchmark</small></article>
          <article><span>MODEL COST</span><strong>{money(dashboard?.estimated_cost_usd ?? 0)}</strong><small>{dashboard?.total_tokens ?? 0} estimated tokens</small></article>
        </section>

        <section className="scenario-strip">
          <div className="section-heading">
            <div><p className="eyebrow">FAULT INJECTION LAB</p><h2>Launch a controlled incident</h2></div>
            <span className="safe-note">All actions stay inside the sandbox</span>
          </div>
          <div className="scenario-grid">
            {scenarios.map((scenario, index) => (
              <button className="scenario-card" data-testid={`scenario-${scenario.id}`} key={scenario.id} onClick={() => runScenario(scenario.id)} disabled={loading !== null}>
                <div><span className="scenario-number">{String(index + 1).padStart(2, "0")}</span><StatusPill status={scenario.severity.toLowerCase()} /></div>
                <h3>{scenario.title}</h3>
                <p>{scenario.symptom}</p>
                <footer><code>{scenario.service}</code><span>{loading === scenario.id ? "Investigating…" : "Run scenario →"}</span></footer>
              </button>
            ))}
          </div>
        </section>

        <section className="workspace" id="investigations">
          <div className="incident-list panel">
            <div className="panel-title"><span>RECENT CASES</span><b>{incidents.length}</b></div>
            {incidents.length === 0 && <div className="empty">Run a scenario to start an investigation.</div>}
            {incidents.map((incident) => (
              <button key={incident.id} data-testid={`incident-${incident.id}`} className={active?.id === incident.id ? "incident-row selected" : "incident-row"} onClick={() => { setActive(incident); setPostmortem(null); }}>
                <div><strong>{incident.title}</strong><small>{incident.id} · {incident.service}</small></div>
                <StatusPill status={incident.status} />
              </button>
            ))}
          </div>

          <div className="trace-panel panel">
            <div className="panel-title"><span>AGENT TRACE</span>{active && <code data-testid="active-incident-id">{active.id}</code>}</div>
            {!active && <div className="empty large">No active trace.<br/>Select a fault scenario above.</div>}
            {active && (
              <>
                <div className="incident-summary" data-testid="incident-summary">
                  <div><p className="eyebrow">LEADING DIAGNOSIS</p><h2 data-testid="root-cause">{active.root_cause ?? "Investigation queued"}</h2><small>Step: <span data-testid="current-step">{active.current_step_key ?? "complete"}</span> · retries: {active.retry_count} · evidence: <span data-testid="evidence-count">{active.evidence.length}</span></small></div>
                  <div className="confidence"><strong>{pct(active.confidence)}</strong><span>confidence</span></div>
                </div>
                <div className="active-status"><StatusPill status={active.status} /><span data-testid="active-status">{active.status}</span></div>
                <div className="workflow-controls">
                  {!['resolved', 'failed', 'cancelled'].includes(active.status) && <button className="secondary" onClick={cancel} disabled={loading !== null}>Cancel workflow</button>}
                  {active.status === 'failed' && <button className="secondary" onClick={retry} disabled={loading !== null}>Retry failed step</button>}
                </div>
                <div className="timeline" data-testid="agent-timeline">
                  {active.trace.map((step, index) => (
                    <div className="trace-step" data-testid="trace-step" data-trace-id={step.id} data-agent={step.agent} key={step.id}>
                      <div className="step-index">{String(index + 1).padStart(2, "0")}</div>
                      <div><strong>{step.agent}</strong><p>{step.summary}</p>{step.tool && <code>{step.tool}</code>}</div>
                      <div className="step-meta"><span>{step.duration_ms} ms</span><span>{step.token_estimate} tok</span></div>
                    </div>
                  ))}
                </div>
                {active.actions[0] && (
                  <div className={active.actions[0].executed ? "approval resolved" : "approval"} data-testid="approval-card">
                    <div>
                      <p className="eyebrow">{active.actions[0].executed ? "REMEDIATION COMPLETE" : "HUMAN APPROVAL REQUIRED"}</p>
                      <h3>{active.actions[0].title}</h3>
                      <p>{active.actions[0].description}</p>
                      <code>{active.actions[0].command_preview}</code>
                    </div>
                    {!active.actions[0].executed && active.status === "awaiting_approval" && <div className="approval-actions">
                      <button data-testid="approve-action" onClick={approve} disabled={loading !== null}>{loading === "approve" ? "Executing…" : "Approve sandbox action"}</button>
                      <input data-testid="rejection-reason" value={rejectionReason} onChange={(event) => setRejectionReason(event.target.value)} disabled={loading !== null} aria-label="Rejection reason" />
                      <button data-testid="reject-action" className="danger" onClick={reject} disabled={loading !== null || rejectionReason.trim().length < 3}>{loading === "reject" ? "Rejecting…" : "Reject action"}</button>
                    </div>}
                  </div>
                )}
                {active.approval_rejection_reason && <div className="rejection-result" data-testid="rejection-result">Rejected: {active.approval_rejection_reason}</div>}
                {active.status === "resolved" && <button className="secondary postmortem-button" data-testid="open-postmortem" onClick={openPostmortem} disabled={loading !== null}>Open postmortem report</button>}
                {postmortem && <pre className="postmortem-report" data-testid="postmortem-report">{postmortem}</pre>}
              </>
            )}
          </div>
        </section>

        <section className="evaluation panel" id="evaluation">
          <div className="section-heading">
            <div><p className="eyebrow">REGRESSION SUITE</p><h2>Agent reliability benchmark</h2></div>
            <button className="secondary" onClick={runEvaluation} disabled={loading !== null}>{loading === "evaluation" ? "Running…" : "Run all evaluations"}</button>
          </div>
          <div className="eval-layout">
            <div className="score"><strong>{pct(evaluation?.root_cause_accuracy ?? null)}</strong><span>root cause accuracy</span></div>
            <div className="eval-stat"><span>Evidence coverage</span><strong>{pct(evaluation?.average_evidence_coverage ?? null)}</strong></div>
            <div className="eval-stat"><span>Unsafe action rate</span><strong>{pct(evaluation?.unsafe_action_rate ?? null)}</strong></div>
            <div className="eval-stat"><span>Cases</span><strong>{evaluation?.cases.length ?? 0}/12</strong></div>
          </div>
          {evaluation && <div className="case-table">{evaluation.cases.map(item => <div key={item.scenario_id}><span className={item.correct ? "check" : "fail"}>{item.correct ? "PASS" : "FAIL"}</span><code>{item.scenario_id}</code><span>{item.predicted_root_cause}</span></div>)}</div>}
        </section>

        <footer className="page-footer"><span>Multi-Agent Incident Lab / deterministic runtime</span><span>Evidence first · approval gated · fully traceable</span></footer>
      </main>
    </div>
  );
}

export default App;
