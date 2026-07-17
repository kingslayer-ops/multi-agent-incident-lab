import { useEffect, useMemo, useState } from "react";
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

  const runScenario = async (scenarioId: string) => {
    setLoading(scenarioId);
    setError(null);
    try {
      const incident = await api.createIncident(scenarioId);
      setActive(incident);
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

  const services = useMemo(() => dashboard?.services_healthy ?? 12, [dashboard]);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">OP</div>
          <div><strong>OpsPilot</strong><span>incident intelligence</span></div>
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
              <button className="scenario-card" key={scenario.id} onClick={() => runScenario(scenario.id)} disabled={loading !== null}>
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
              <button key={incident.id} className={active?.id === incident.id ? "incident-row selected" : "incident-row"} onClick={() => setActive(incident)}>
                <div><strong>{incident.title}</strong><small>{incident.id} · {incident.service}</small></div>
                <StatusPill status={incident.status} />
              </button>
            ))}
          </div>

          <div className="trace-panel panel">
            <div className="panel-title"><span>AGENT TRACE</span>{active && <code>{active.id}</code>}</div>
            {!active && <div className="empty large">No active trace.<br/>Select a fault scenario above.</div>}
            {active && (
              <>
                <div className="incident-summary">
                  <div><p className="eyebrow">LEADING DIAGNOSIS</p><h2>{active.root_cause}</h2></div>
                  <div className="confidence"><strong>{pct(active.confidence)}</strong><span>confidence</span></div>
                </div>
                <div className="timeline">
                  {active.trace.map((step, index) => (
                    <div className="trace-step" key={step.id}>
                      <div className="step-index">{String(index + 1).padStart(2, "0")}</div>
                      <div><strong>{step.agent}</strong><p>{step.summary}</p>{step.tool && <code>{step.tool}</code>}</div>
                      <div className="step-meta"><span>{step.duration_ms} ms</span><span>{step.token_estimate} tok</span></div>
                    </div>
                  ))}
                </div>
                {active.actions[0] && (
                  <div className={active.actions[0].executed ? "approval resolved" : "approval"}>
                    <div>
                      <p className="eyebrow">{active.actions[0].executed ? "REMEDIATION COMPLETE" : "HUMAN APPROVAL REQUIRED"}</p>
                      <h3>{active.actions[0].title}</h3>
                      <p>{active.actions[0].description}</p>
                      <code>{active.actions[0].command_preview}</code>
                    </div>
                    {!active.actions[0].executed && <button onClick={approve} disabled={loading !== null}>{loading === "approve" ? "Executing…" : "Approve sandbox action"}</button>}
                  </div>
                )}
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

        <footer className="page-footer"><span>OpsPilot / deterministic demo runtime</span><span>Evidence first · approval gated · fully traceable</span></footer>
      </main>
    </div>
  );
}

export default App;
