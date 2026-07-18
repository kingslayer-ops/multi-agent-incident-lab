import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type { Dashboard, Evaluation, Incident, Scenario } from "./types";
import { localizeAction, localizeIncidentTitle, localizePostmortem, localizeRootCause, localizeScenario, localizeTrace, statusLabel, workflowStepLabel } from "./i18n";

const pct = (value: number | null) => (value === null ? "—" : `${Math.round(value * 100)}%`);
const money = (value: number) => `$${value.toFixed(4)}`;
const formatPct = (value: number | null) => (value === null ? "—" : pct(value));

function StatusPill({ status }: { status: string }) {
  return <span className={`status status-${status.replaceAll("_", "-")}`}>{statusLabel(status)}</span>;
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
  const [rejectionReason, setRejectionReason] = useState("当前事故的处置风险不可接受");
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
      setError(reason instanceof Error ? reason.message : "调查启动失败");
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
      setError(reason instanceof Error ? reason.message : "审批失败");
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
      setError(reason instanceof Error ? reason.message : "评测运行失败");
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
      setError(reason instanceof Error ? reason.message : "拒绝操作失败");
    } finally {
      setLoading(null);
    }
  };

  const openPostmortem = async () => {
    if (!active) return;
    setLoading("postmortem");
    try {
      await api.postmortem(active.id);
      setPostmortem(localizePostmortem(active));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "复盘报告加载失败");
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
      setError(reason instanceof Error ? reason.message : "取消工作流失败");
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
      setError(reason instanceof Error ? reason.message : "重试失败");
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
          <div><strong>事故响应实验室</strong><span>多智能体协同处置</span></div>
        </div>
        <nav>
          <a className="active" href="#overview"><span>01</span> 指挥中心</a>
          <a href="#investigations"><span>02</span> 事故调查</a>
          <a href="#evaluation"><span>03</span> 评测实验室</a>
        </nav>
        <div className="runtime-card">
          <div className="pulse" />
          <div><strong>运行时在线</strong><span>{provider}</span></div>
        </div>
      </aside>

      <main>
        <header>
          <div>
            <p className="eyebrow">指挥中心 / 实时</p>
            <h1>以证据驱动调查，<br/><em>让处置始终可控。</em></h1>
          </div>
          <div className="health-ring">
            <span>{services}/{dashboard?.services_total ?? 12}</span>
            <small>服务运行正常</small>
          </div>
        </header>

        {error && <div className="error-banner">{error}</div>}
        <div className={`connection-state connection-${streamStatus}`} data-testid="connection-state">
          {`事件流：${statusLabel(streamStatus)}`}
        </div>
        {workerWaiting && (
          <div className="worker-recovery-state" data-testid="worker-recovery-state">
            正在等待 Worker 恢复
          </div>
        )}

        <section className="metrics" id="overview">
          <article><span>事故总数</span><strong>{dashboard?.incidents_total ?? 0}</strong><small>模拟事故调查</small></article>
          <article><span>等待审批</span><strong className="amber">{dashboard?.awaiting_approval ?? 0}</strong><small>人工控制节点</small></article>
          <article><span>根因准确率</span><strong className="cyan">{formatPct(dashboard?.root_cause_accuracy ?? null)}</strong><small>最近一次评测</small></article>
          <article><span>模型成本</span><strong>{money(dashboard?.estimated_cost_usd ?? 0)}</strong><small>预计 {dashboard?.total_tokens ?? 0} Tokens</small></article>
        </section>

        <section className="scenario-strip">
          <div className="section-heading">
            <div><p className="eyebrow">故障注入实验室</p><h2>启动一次受控事故</h2></div>
            <span className="safe-note">所有动作仅在沙箱内执行</span>
          </div>
          <div className="scenario-grid">
            {scenarios.map((rawScenario, index) => {
              const scenario = localizeScenario(rawScenario);
              return (
              <button className="scenario-card" data-testid={`scenario-${scenario.id}`} key={scenario.id} onClick={() => runScenario(scenario.id)} disabled={loading !== null}>
                <div><span className="scenario-number">{String(index + 1).padStart(2, "0")}</span><StatusPill status={scenario.severity.toLowerCase()} /></div>
                <h3>{scenario.title}</h3>
                <p>{scenario.symptom}</p>
                <footer><code>{scenario.service}</code><span>{loading === scenario.id ? "调查中…" : "运行场景 →"}</span></footer>
              </button>
              );
            })}
          </div>
        </section>

        <section className="workspace" id="investigations">
          <div className="incident-list panel">
            <div className="panel-title"><span>最近事故</span><b>{incidents.length}</b></div>
            {incidents.length === 0 && <div className="empty">运行一个故障场景以开始调查。</div>}
            {incidents.map((incident) => (
              <button key={incident.id} data-testid={`incident-${incident.id}`} className={active?.id === incident.id ? "incident-row selected" : "incident-row"} onClick={() => { setActive(incident); setPostmortem(null); }}>
                <div><strong>{localizeIncidentTitle(incident)}</strong><small>{incident.id} · {incident.service}</small></div>
                <StatusPill status={incident.status} />
              </button>
            ))}
          </div>

          <div className="trace-panel panel">
            <div className="panel-title"><span>智能体轨迹</span>{active && <code data-testid="active-incident-id">{active.id}</code>}</div>
            {!active && <div className="empty large">暂无调查轨迹。<br/>请从上方选择一个故障场景。</div>}
            {active && (
              <>
                <div className="incident-summary" data-testid="incident-summary">
                  <div><p className="eyebrow">当前诊断</p><h2 data-testid="root-cause">{localizeRootCause(active.root_cause)}</h2><small>步骤：<span data-testid="current-step">{workflowStepLabel(active.current_step_key)}</span> · 重试：{active.retry_count} · 证据：<span data-testid="evidence-count">{active.evidence.length}</span></small></div>
                  <div className="confidence"><strong>{pct(active.confidence)}</strong><span>置信度</span></div>
                </div>
                <div className="active-status"><StatusPill status={active.status} /><span className="sr-only" data-testid="active-status">{active.status}</span></div>
                <div className="workflow-controls">
                  {!['resolved', 'failed', 'cancelled'].includes(active.status) && <button className="secondary" onClick={cancel} disabled={loading !== null}>取消工作流</button>}
                  {active.status === 'failed' && <button className="secondary" onClick={retry} disabled={loading !== null}>重试失败步骤</button>}
                </div>
                <div className="timeline" data-testid="agent-timeline">
                  {active.trace.map((step, index) => {
                    const copy = localizeTrace(step);
                    return (
                    <div className="trace-step" data-testid="trace-step" data-trace-id={step.id} data-agent={step.agent} key={step.id}>
                      <div className="step-index">{String(index + 1).padStart(2, "0")}</div>
                      <div><strong>{copy.agent}</strong><p>{copy.summary}</p>{step.tool && <code>{step.tool}</code>}</div>
                      <div className="step-meta"><span>{step.duration_ms} ms</span><span>{step.token_estimate} tok</span></div>
                    </div>
                    );
                  })}
                </div>
                {active.actions[0] && (
                  <div className={active.actions[0].executed ? "approval resolved" : "approval"} data-testid="approval-card">
                    <div>
                      <p className="eyebrow">{active.actions[0].executed ? "处置已完成" : "需要人工审批"}</p>
                      <h3>{localizeAction(active.scenario_id, active.actions[0]).title}</h3>
                      <p>{localizeAction(active.scenario_id, active.actions[0]).description}</p>
                      <code>{active.actions[0].command_preview}</code>
                    </div>
                    {!active.actions[0].executed && active.status === "awaiting_approval" && <div className="approval-actions">
                      <button data-testid="approve-action" onClick={approve} disabled={loading !== null}>{loading === "approve" ? "执行中…" : "批准沙箱处置"}</button>
                      <input data-testid="rejection-reason" value={rejectionReason} onChange={(event) => setRejectionReason(event.target.value)} disabled={loading !== null} aria-label="拒绝原因" />
                      <button data-testid="reject-action" className="danger" onClick={reject} disabled={loading !== null || rejectionReason.trim().length < 3}>{loading === "reject" ? "拒绝中…" : "拒绝处置"}</button>
                    </div>}
                  </div>
                )}
                {active.approval_rejection_reason && <div className="rejection-result" data-testid="rejection-result">已拒绝：{active.approval_rejection_reason}</div>}
                {active.status === "resolved" && <button className="secondary postmortem-button" data-testid="open-postmortem" onClick={openPostmortem} disabled={loading !== null}>打开事故复盘报告</button>}
                {postmortem && <pre className="postmortem-report" data-testid="postmortem-report">{postmortem}</pre>}
              </>
            )}
          </div>
        </section>

        <section className="evaluation panel" id="evaluation">
          <div className="section-heading">
            <div><p className="eyebrow">回归测试集</p><h2>智能体可靠性评测</h2></div>
            <button className="secondary" onClick={runEvaluation} disabled={loading !== null}>{loading === "evaluation" ? "运行中…" : "运行全部评测"}</button>
          </div>
          <div className="eval-layout">
            <div className="score"><strong>{pct(evaluation?.root_cause_accuracy ?? null)}</strong><span>根因准确率</span></div>
            <div className="eval-stat"><span>证据覆盖率</span><strong>{pct(evaluation?.average_evidence_coverage ?? null)}</strong></div>
            <div className="eval-stat"><span>不安全操作率</span><strong>{pct(evaluation?.unsafe_action_rate ?? null)}</strong></div>
            <div className="eval-stat"><span>评测案例</span><strong>{evaluation?.cases.length ?? 0}/12</strong></div>
          </div>
          {evaluation && <div className="case-table">{evaluation.cases.map(item => <div key={item.scenario_id}><span className={item.correct ? "check" : "fail"}>{item.correct ? "通过" : "失败"}</span><code>{item.scenario_id}</code><span>{localizeRootCause(item.predicted_root_cause)}</span></div>)}</div>}
        </section>

        <footer className="page-footer"><span>多智能体事故响应实验室 / 确定性运行时</span><span>证据优先 · 审批控制 · 全程可追踪</span></footer>
      </main>
    </div>
  );
}

export default App;
