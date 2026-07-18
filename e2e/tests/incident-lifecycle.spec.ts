import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import type { APIRequestContext, Page } from "@playwright/test";
import { test, expect } from "../fixtures";

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const composeFile = path.join(repository, "docker-compose.e2e.yml");

const compose = (...args: string[]) => execFileSync(
  "docker", ["compose", "-f", composeFile, ...args], { cwd: repository, stdio: "inherit" }
);

async function startIncident(page: Page, scenarioId: string) {
  await page.goto("/");
  await expect(page.getByTestId("app-shell")).toBeVisible();
  await page.getByTestId(`scenario-${scenarioId}`).click();
  await expect(page.getByTestId("active-incident-id")).toContainText("inc-");
  await expect(page.getByTestId("active-status")).toHaveText(/queued|running/);
  return (await page.getByTestId("active-incident-id").textContent())!;
}

async function waitForApproval(page: Page) {
  await expect(page.getByTestId("active-status")).toHaveText("awaiting_approval");
  await expect(page.getByTestId("approval-card")).toBeVisible();
  await expect(page.getByTestId("approve-action")).toBeEnabled();
}

async function approveAndResolve(page: Page) {
  await page.getByTestId("approve-action").click();
  await expect(page.getByTestId("active-status")).toHaveText(/resuming|running|resolved/);
  await expect(page.getByTestId("active-status")).toHaveText("resolved");
}

async function traceIds(page: Page) {
  return page.getByTestId("trace-step").evaluateAll(elements =>
    elements.map(element => element.getAttribute("data-trace-id") ?? "")
  );
}

async function incidentTraceCount(request: APIRequestContext, incidentId: string) {
  const response = await request.get(`/api/incidents/${incidentId}`);
  expect(response.ok()).toBeTruthy();
  return ((await response.json()) as { trace: unknown[] }).trace.length;
}

test.describe.configure({ mode: "serial" });

test("@smoke complete browser incident response loop", async ({ page, networkLog: _networkLog }) => {
  await startIncident(page, "payment-pool-exhaustion");
  await expect(page.getByTestId("trace-step").first()).toBeVisible();
  await waitForApproval(page);
  await expect(page.getByTestId("evidence-count")).toHaveText("3");
  await expect(page.getByTestId("root-cause")).not.toHaveText("调查已进入队列");
  await approveAndResolve(page);
  await expect(page.locator('[data-agent="Remediation Agent"]')).toHaveCount(1);
  await page.getByTestId("open-postmortem").click();
  await expect(page.getByTestId("postmortem-report")).toContainText("根因");
});

test("refresh restores durable progress without duplicate steps", async ({ page, networkLog: _networkLog }) => {
  await startIncident(page, "inventory-memory-leak");
  await expect.poll(() => page.getByTestId("trace-step").count()).toBeGreaterThanOrEqual(2);
  const before = await traceIds(page);
  await page.reload();
  await expect(page.getByTestId("active-incident-id")).toContainText("inc-");
  await expect.poll(() => traceIds(page)).toEqual(expect.arrayContaining(before));
  await waitForApproval(page);
  const restored = await traceIds(page);
  expect(new Set(restored).size).toBe(restored.length);
  await approveAndResolve(page);
});

test("approval pauses once and resumes from remediation", async ({ page, request, networkLog: _networkLog }) => {
  const incidentId = await startIncident(page, "checkout-feature-regression");
  await waitForApproval(page);
  const paused = await request.get(`/api/incidents/${incidentId}/workflow`);
  const pausedSteps = ((await paused.json()) as { steps: Array<{ status: string }> }).steps;
  expect(pausedSteps).toHaveLength(7);
  await page.getByTestId("approve-action").evaluate(element => {
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await expect(page.getByTestId("active-status")).toHaveText("resolved");
  const completed = await request.get(`/api/incidents/${incidentId}/workflow`);
  expect(((await completed.json()) as { steps: unknown[] }).steps).toHaveLength(10);
  await expect(page.locator('[data-agent="Remediation Agent"]')).toHaveCount(1);
});

test("rejected approval is cancelled with a durable reason", async ({ page, networkLog: _networkLog }) => {
  await startIncident(page, "gateway-tls-expiry");
  await waitForApproval(page);
  await page.getByTestId("rejection-reason").fill("Maintenance window is closed");
  await page.getByTestId("reject-action").click();
  await expect(page.getByTestId("active-status")).toHaveText("cancelled");
  await expect(page.getByTestId("rejection-result")).toContainText("Maintenance window is closed");
  await expect(page.locator('[data-agent="Remediation Agent"]')).toHaveCount(0);
});

test("@resilience worker restart recovers the browser workflow", async ({ page, networkLog: _networkLog }) => {
  await startIncident(page, "redis-topology-stale");
  await expect(page.getByTestId("trace-step").first()).toBeVisible();
  const before = await traceIds(page);
  compose("kill", "worker");
  try {
    await expect(page.getByTestId("connection-state")).toHaveText("事件流：实时");
    await expect(page.getByTestId("worker-recovery-state")).toHaveText("正在等待 Worker 恢复");
    compose("start", "worker");
    await waitForApproval(page);
    const recovered = await traceIds(page);
    expect(recovered).toEqual(expect.arrayContaining(before));
    expect(new Set(recovered).size).toBe(recovered.length);
    await approveAndResolve(page);
  } finally {
    compose("start", "worker");
  }
});

test("@resilience SSE reconnect replays missed progress once", async ({ page, request, context, networkLog: _networkLog }) => {
  const incidentId = await startIncident(page, "order-deadlock");
  await expect(page.getByTestId("trace-step").first()).toBeVisible();
  const before = await page.getByTestId("trace-step").count();
  await context.setOffline(true);
  await expect.poll(() => incidentTraceCount(request, incidentId)).toBeGreaterThan(before);
  const durableCount = await incidentTraceCount(request, incidentId);
  await context.setOffline(false);
  await expect(page.getByTestId("connection-state")).toContainText(/实时|已结束/);
  await expect.poll(() => page.getByTestId("trace-step").count()).toBeGreaterThanOrEqual(durableCount);
  await waitForApproval(page);
  const replayed = await traceIds(page);
  expect(new Set(replayed).size).toBe(replayed.length);
  await approveAndResolve(page);
});
