import type { Dashboard, Evaluation, Incident, Scenario } from "./types";

const json = async <T>(response: Response): Promise<T> => {
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail ?? "Request failed");
  }
  return response.json() as Promise<T>;
};

export const api = {
  health: () => fetch("/health").then(json<{ status: string; provider: string }>),
  scenarios: () => fetch("/api/scenarios").then(json<Scenario[]>),
  incidents: () => fetch("/api/incidents").then(json<Incident[]>),
  dashboard: () => fetch("/api/dashboard").then(json<Dashboard>),
  createIncident: (scenarioId: string) =>
    fetch("/api/incidents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario_id: scenarioId })
    }).then(json<Incident>),
  approve: (incidentId: string, actionId: string) =>
    fetch(`/api/incidents/${incidentId}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action_id: actionId, approved_by: "console-operator" })
    }).then(json<Incident>),
  evaluate: () => fetch("/api/evaluations/run", { method: "POST" }).then(json<Evaluation>),
  latestEvaluation: () => fetch("/api/evaluations/latest").then(json<Evaluation | null>)
};
