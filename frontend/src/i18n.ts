import type { Action, Incident, Scenario, TraceStep } from "./types";

const scenarioCopy: Record<string, { title: string; symptom: string }> = {
  "payment-pool-exhaustion": { title: "支付 API 延迟激增", symptom: "P99 延迟从 220 毫秒升至 4.2 秒，结算错误率达到 18%。" },
  "inventory-memory-leak": { title: "库存服务 Pod 反复重启", symptom: "内存持续增长，Pod 每 17 分钟被 OOM Killer 终止。" },
  "checkout-feature-regression": { title: "结算校验异常", symptom: "功能开关发布后有效优惠券被拒绝，422 响应升至 31%。" },
  "gateway-tls-expiry": { title: "网关 TLS 连接失败", symptom: "服务间请求无法通过 TLS 校验并返回 502。" },
  "redis-topology-stale": { title: "会话缓存重定向风暴", symptom: "Redis 重定向和重试放大使登录延迟超过 3 秒。" },
  "order-deadlock": { title: "订单事务死锁", symptom: "数据库锁等待激增，订单确认请求间歇失败。" },
  "notification-rate-limit": { title: "通知投递积压", symptom: "邮件投递延迟，重试队列快速增长。" },
  "event-schema-incompatibility": { title: "订单事件被拒绝", symptom: "生产者发布后，新订单事件持续进入死信队列。" },
  "service-dns-failure": { title: "服务发现失败", symptom: "无法解析用户画像服务，推荐请求因此超时。" },
  "logging-disk-full": { title: "日志节点拒绝写入", symptom: "采集节点磁盘耗尽，审计日志开始丢失。" },
  "auth-clock-skew": { title: "认证令牌校验失败", symptom: "部分节点时钟偏移，导致有效令牌被判定为尚未生效。" },
  "worker-thread-saturation": { title: "报表生成阻塞", symptom: "CPU 低于 50%，但交互式报表仍排队数分钟。" },
};

const rootCauses: Record<string, string> = {
  "database connection pool exhaustion": "数据库连接池耗尽",
  "unbounded inventory snapshot cache": "库存快照缓存无上限增长",
  "strict coupon expiry feature flag regression": "优惠券过期校验功能开关回归",
  "expired service TLS certificate": "服务 TLS 证书过期",
  "stale Redis cluster topology cache": "Redis 集群拓扑缓存过期",
  "database deadlock caused by inconsistent lock ordering": "锁顺序不一致引发数据库死锁",
  "notification provider rate-limit exhaustion": "通知供应商限流额度耗尽",
  "incompatible event schema deployment": "事件 Schema 发布不兼容",
  "service discovery DNS failure": "服务发现 DNS 故障",
  "log volume disk exhaustion": "日志卷磁盘空间耗尽",
  "node clock skew invalidating authentication tokens": "节点时钟偏移导致认证令牌失效",
  "worker thread pool saturation": "Worker 线程池饱和",
};

const actionCopy: Record<string, { title: string; description: string }> = {
  "payment-pool-exhaustion": { title: "恢复支付数据库连接池上限", description: "回滚 cfg-992，将连接池上限从 30 恢复至 60，再验证等待请求与 P99 延迟。" },
  "inventory-memory-leak": { title: "回滚无上限缓存版本", description: "回滚 deploy-2077，限制缓存容量后再进行受控发布。" },
  "checkout-feature-regression": { title: "关闭异常校验开关", description: "关闭 flag-441，回放一笔金丝雀结算请求并监控 422 比例。" },
  "gateway-tls-expiry": { title: "轮换过期证书", description: "启用已预置的证书并验证完整信任链。" },
  "redis-topology-stale": { title: "刷新 Redis 拓扑", description: "清除客户端拓扑缓存并恢复安全的 TTL。" },
  "order-deadlock": { title: "回滚并行行更新", description: "恢复确定性的加锁顺序，并重放失败事务。" },
  "notification-rate-limit": { title: "限制营销活动流量", description: "暂停活动扇出，在供应商配额内逐步清空队列。" },
  "event-schema-incompatibility": { title: "恢复兼容的事件 Schema", description: "回滚 Schema v7，并重放死信事件。" },
  "service-dns-failure": { title: "恢复服务别名", description: "恢复被删除的 DNS 别名，并从金丝雀 Pod 验证解析。" },
  "logging-disk-full": { title: "恢复安全的日志保留策略", description: "回滚保留期并压缩过期分段，不删除有效审计数据。" },
  "auth-clock-skew": { title: "恢复时间同步", description: "回滚 NTP 策略并重新同步受影响节点。" },
  "worker-thread-saturation": { title: "隔离导出任务", description: "回滚共享 Worker 池并清空交互式任务队列。" },
};

export const statusLabel = (value: string) => ({
  idle: "空闲", connecting: "连接中", reconnecting: "重新连接", live: "实时", complete: "已结束",
  queued: "排队中", running: "调查中", awaiting_approval: "等待审批", resuming: "恢复中",
  resolved: "已解决", failed: "失败", cancelled: "已取消", cancel_requested: "正在取消",
  retry_scheduled: "等待重试", "sev-1": "一级", "sev-2": "二级", "sev-3": "三级",
}[value.toLowerCase()] ?? value.replaceAll("_", " "));

export const localizeScenario = (scenario: Scenario) => ({ ...scenario, ...(scenarioCopy[scenario.id] ?? {}) });
export const localizeIncidentTitle = (incident: Incident) => scenarioCopy[incident.scenario_id]?.title ?? incident.title;
export const localizeRootCause = (value: string | null) => value ? (rootCauses[value] ?? value) : "调查已进入队列";
export const localizeAction = (scenarioId: string, action: Action) => ({ ...action, ...(actionCopy[scenarioId] ?? {}) });

const agentLabels: Record<string, string> = {
  "Triage Agent": "分诊智能体", "Metric Agent": "指标智能体", "Log Agent": "日志智能体",
  "Change Agent": "变更智能体", "Diagnosis Agent": "诊断智能体", "Safety Reviewer": "安全审查智能体",
  "Remediation Agent": "处置智能体", "Verification Agent": "验证智能体",
};

const traceSummaries: Record<string, string> = {
  "Triage Agent": "已完成事故分级并确认受影响服务。",
  "Metric Agent": "已查询服务指标并提取异常信号。",
  "Log Agent": "已找到与事故时间窗口相关的错误日志。",
  "Change Agent": "已定位事故时间窗口内的相关变更。",
  "Safety Reviewer": "处置动作存在变更风险，必须经过人工审批。",
  "Remediation Agent": "已执行通过审批的沙箱处置动作。",
  "Verification Agent": "错误率和延迟已恢复至基线，事故解决。",
};

export const localizeTrace = (step: TraceStep) => ({
  agent: agentLabels[step.agent] ?? step.agent,
  summary: step.agent === "Diagnosis Agent"
    ? `已确认根因：${Object.entries(rootCauses).find(([cause]) => step.summary.includes(cause))?.[1] ?? step.summary}`
    : (traceSummaries[step.agent] ?? step.summary),
});

export const localizePostmortem = (incident: Incident) => {
  const action = incident.actions[0] ? localizeAction(incident.scenario_id, incident.actions[0]) : null;
  return `# ${localizeIncidentTitle(incident)}\n\n**影响：** ${scenarioCopy[incident.scenario_id]?.symptom ?? incident.symptom}\n\n**根因：** ${localizeRootCause(incident.root_cause)}（${Math.round((incident.confidence ?? 0) * 100)}% 置信度）\n\n**解决方案：** ${action?.description ?? "已执行受控处置。"}\n\n**审批：** 沙箱处置已通过人工审批。\n\n**后续行动：** 增加回归告警、明确的容量护栏，并为该故障模式补充重放测试。`;
};

export const workflowStepLabel = (value: string | null) => ({
  triage: "事故分诊", collect_metrics: "采集指标", collect_logs: "检索日志", inspect_changes: "检查变更",
  form_hypotheses: "形成假设", safety_review: "安全审查", await_approval: "等待审批", wait_approval: "等待审批",
  execute_remediation: "执行处置", verify_recovery: "验证恢复", finalize: "完成", complete: "完成",
}[value ?? "complete"] ?? value ?? "完成");
