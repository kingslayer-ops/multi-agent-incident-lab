export type Scenario = {
  id: string;
  title: string;
  service: string;
  symptom: string;
  severity: string;
  difficulty: string;
};

export type TraceStep = {
  id: string;
  agent: string;
  status: string;
  summary: string;
  tool: string | null;
  evidence_ids: string[];
  duration_ms: number;
  token_estimate: number;
  started_at: string;
};

export type Evidence = {
  id: string;
  kind: string;
  source: string;
  summary: string;
  payload: Record<string, unknown>;
};

export type Action = {
  id: string;
  title: string;
  description: string;
  command_preview: string;
  risk: string;
  requires_approval: boolean;
  approved: boolean;
  executed: boolean;
  execution_result: string | null;
};

export type Incident = {
  id: string;
  scenario_id: string;
  title: string;
  service: string;
  severity: string;
  status: string;
  workflow_run_id: string | null;
  current_step_key: string | null;
  retry_count: number;
  approval_rejection_reason: string | null;
  symptom: string;
  created_at: string;
  root_cause: string | null;
  confidence: number | null;
  evidence: Evidence[];
  hypotheses: Array<{
    cause: string;
    confidence: number;
    evidence_ids: string[];
    counter_evidence_ids: string[];
    verdict: string;
  }>;
  trace: TraceStep[];
  actions: Action[];
  postmortem: string | null;
  total_tokens: number;
  estimated_cost_usd: number;
  provider: string;
  fallback_events: Array<{
    from_provider: string;
    to_provider: string;
    reason: string;
    occurred_at: string;
  }>;
};

export type Evaluation = {
  run_id: string;
  root_cause_accuracy: number;
  average_evidence_coverage: number;
  unsafe_action_rate: number;
  average_duration_ms: number;
  cases: Array<{
    scenario_id: string;
    expected_root_cause: string;
    predicted_root_cause: string;
    correct: boolean;
    evidence_coverage: number;
  }>;
};

export type Dashboard = {
  incidents_total: number;
  incidents_resolved: number;
  awaiting_approval: number;
  total_tokens: number;
  estimated_cost_usd: number;
  root_cause_accuracy: number | null;
  services_healthy: number;
  services_total: number;
};
