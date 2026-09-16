# Multi-Agent Incident Lab

**以证据为核心、支持崩溃恢复与真实可观测性查询的多 Agent 事故响应实验平台。**

[![CI](https://github.com/kingslayer-ops/multi-agent-incident-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/kingslayer-ops/multi-agent-incident-lab/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/kingslayer-ops/multi-agent-incident-lab?display_name=tag)](https://github.com/kingslayer-ops/multi-agent-incident-lab/releases/latest)
![覆盖率门槛](https://img.shields.io/badge/coverage-%E2%89%A590%25-brightgreen)
[![License](https://img.shields.io/github/license/kingslayer-ops/multi-agent-incident-lab)](LICENSE)

[English](README.md) · [架构说明](docs/architecture.md) · [生产运行时](docs/production-runtime.md) · [工作流可靠性](docs/workflow-reliability.md) · [可观测性适配器](docs/observability-adapters.md) · [端到端测试](docs/e2e-testing.md) · [安全策略](SECURITY.md)

项目由 8 个职责明确的角色协作完成故障分级、指标分析、日志检索、变更关联、根因判断、安全审查、沙箱修复和恢复验证。API 与 Worker 独立运行，十个稳定步骤持久化到 SQLite WAL 或 PostgreSQL；Worker 崩溃后可以通过租约和检查点继续执行。

> 本仓库是安全实验平台。它不会执行操作系统、云平台或 Kubernetes 命令。

## 产品展示

![Multi-Agent Incident Lab 展示证据关联根因、持久化审批检查点和 Agent 调查轨迹](docs/assets/command-center.png)

截图展示固定的订单事务死锁场景，并停留在持久化审批检查点：简体中文界面同时展示证据关联根因、置信度、不可变证据数量、智能体调查轨迹和工作流状态。同一条浏览器闭环已由仓库现有 Playwright 测试覆盖。

| 持久化工作流 | 人工控制 | 可观测且可复现 |
| --- | --- | --- |
| 租约领取、心跳、检查点、旧 Worker 隔离、重试、取消与恢复 | 幂等沙箱修复前必须存在最终且持久化的人工批准 | 只读 Prometheus/Loki 适配器可独立降级到确定性 Mock 基线 |

## 系统架构

![系统架构图：React 控制台、FastAPI 控制面、SQLite 持久化边界、Worker、调查角色、可观测性数据源、安全策略、人工审批和沙箱修复](docs/assets/system-architecture.svg)

API 不会同步等待调查完成。默认配置使用 SQLite `BEGIN IMMEDIATE`，生产配置使用 PostgreSQL `FOR UPDATE SKIP LOCKED` 与 Redis 唤醒提示；Worker 只有在仍持有匹配的租约版本时才能提交输出。架构图展示默认配置，多 Worker 部署见[生产运行时说明](docs/production-runtime.md)。

## Worker 崩溃恢复

![Worker 恢复时序图：Worker A 崩溃、租约过期、Worker B 从检查点恢复、旧提交被隔离，以及浏览器补发缺失事件](docs/assets/worker-recovery-sequence.svg)

Worker 进程崩溃后，其他 Worker 会回收过期租约并从持久化步骤继续；旧 Worker 的迟到提交会被 owner/version fencing 拒绝。浏览器使用 `Last-Event-ID` 只补发断线期间缺失的事件。详细保证见[工作流可靠性说明](docs/workflow-reliability.md)，可编辑源文件见 [`worker-recovery-sequence.drawio`](docs/assets/worker-recovery-sequence.drawio)。

## v1.4 核心能力

- 新增 PostgreSQL 持久化后端，覆盖事故、运行、步骤、租约、尝试、审批、幂等修复和有序事件。
- 多 Worker 通过 `FOR UPDATE SKIP LOCKED` 竞争领取，过期租约恢复和事件序号分配具备并发保护。
- Redis 只传递有界唤醒提示，不承载任务真相；Redis 不可用时自动回退 PostgreSQL 轮询，不丢失已提交任务。
- 保留 SQLite WAL 零依赖模式，继续作为本地演示与 Playwright 稳定回归基线。
- CI 使用真实 PostgreSQL 服务验证四 Worker 竞争时仅有一个领取成功，并校验生产 Compose。

以下 v1.3 的真实可观测性能力继续保留：

- 保留确定性 Mock 指标和日志，作为离线演示、评测与 Playwright 回归基线。
- 新增只读 Prometheus `/api/v1/query_range` 适配器，支持自定义具名 PromQL、查询窗口、步长和序列上限。
- 新增只读 Loki `/loki/api/v1/query_range` 适配器，支持自定义 LogQL、查询窗口、行数上限和日志去重。
- 支持 Bearer 或 Basic 认证；Loki 支持多租户 `X-Scope-OrgID`。
- 超时、连接失败、429/5xx、认证失败、协议错误、空结果和超大响应均有明确原因码。
- 默认按数据源独立降级到 Mock，并在证据中记录 `mode/provider/reason_code/retryable`；也可以关闭降级，让持久化 Worker 执行重试。
- 不允许在 URL 中嵌入凭据，不提供跳过 TLS 校验选项，不把密钥和端点复制到错误消息。

详细配置和证据结构见[真实可观测性适配器说明](docs/observability-adapters.md)。

## 工作流可靠性

- `POST /api/incidents` 只创建任务并返回 `202 Accepted`。
- Worker 在 SQLite 中使用 `BEGIN IMMEDIATE`、在 PostgreSQL 中使用 `FOR UPDATE SKIP LOCKED` 原子领取步骤，记录租约、心跳、版本和每次尝试。
- Worker 崩溃后，其他 Worker 可以回收过期租约并从当前步骤继续；旧 Worker 的迟到提交会被拒绝。
- 状态更新与 SSE 事件在同一事务提交，SSE 支持事件游标、断线补发和去重。
- 人工审批是唯一、最终且持久化的决定；沙箱修复使用幂等键抑制重复执行。
- 支持自动重试、人工重试、拒绝审批、取消请求和安全边界停止。

项目采用“至少一次调度 + 幂等步骤提交”，不宣称 exactly-once。详细保证和失败窗口见[工作流可靠性说明](docs/workflow-reliability.md)。

## 浏览器级验证

六条 Playwright Chromium 流程覆盖完整事故闭环、页面刷新恢复、审批暂停与继续、拒绝审批、Worker 真实崩溃和租约恢复，以及 SSE 断线补发与去重。

E2E 使用独立 API、Worker、前端和 SQLite 数据卷，并明确固定为 Mock 遥测，不依赖外部 Prometheus/Loki。失败时保存截图、视频、Playwright Trace、浏览器/网络日志和 Compose 日志。

## 一键运行

```bash
docker compose up --build
```

访问 <http://localhost:8000>，API 文档位于 <http://localhost:8000/docs>。默认模式无需模型密钥或可观测性服务。

使用 PostgreSQL、Redis 和两个 Worker 的生产式配置：

```bash
docker compose -f docker-compose.production.yml up --build --scale worker=2
```

PostgreSQL 是唯一事实源，Redis 仅用于降低唤醒延迟；详细并发保证和失败语义见[生产运行时说明](docs/production-runtime.md)。

启用真实数据源：

```dotenv
INCIDENT_LAB_TELEMETRY_MODE=live
INCIDENT_LAB_PROMETHEUS_URL=https://prometheus.example.com
INCIDENT_LAB_LOKI_URL=https://loki.example.com
```

生产环境应根据自己的指标名和日志标签配置 PromQL/LogQL 模板，具体见 `.env.example`。

## 本地开发

```bash
pip install -e ".[dev]"
uvicorn incident_lab.api:app --app-dir src --reload
```

另一个终端启动 Worker：

```bash
python -m incident_lab.worker
```

## 验证

```bash
pytest --cov=incident_lab --cov-report=term-missing --cov-fail-under=90
cd frontend && npm run build
docker compose config
docker compose -f docker-compose.production.yml config
make e2e
```

测试覆盖多 Worker 竞争、租约恢复、旧 Worker 提交拒绝、重试与取消、并发审批、事务回滚、SSE 回放、修复幂等、真实子进程崩溃，以及 Prometheus/Loki 请求契约、认证、解析和降级。

## 项目边界

这是作品集级事故响应实验平台，不是真实生产级分布式控制平面。v1.4.0 新增可选的 PostgreSQL/Redis 多 Worker 运行时，同时保留 SQLite 与 Mock 数据源作为稳定、可复现的演示基线。系统仍只执行沙箱修复，也没有应用认证、多租户业务边界或远程命令执行。

## 许可证

[MIT](LICENSE)
