# Multi-Agent Incident Lab

**以证据为核心、支持宕机恢复的多 Agent 故障响应实验室。**

[English](README.md) · [架构说明](docs/architecture.md) · [工作流可靠性](docs/workflow-reliability.md) · [安全策略](SECURITY.md)

项目由 8 个职责明确的角色协作完成故障分级、指标分析、日志检索、变更关联、根因判断、安全审查、沙箱修复和恢复验证。v1.2.0 将 API 与 Worker 分离，并把调查过程拆成 10 个稳定、带版本的持久化步骤。

## v1.2.0 核心能力

- `POST /api/incidents` 只创建任务并返回 `202 Accepted`，不会等待调查完成。
- Worker 使用 SQLite `BEGIN IMMEDIATE` 原子领取步骤，并记录租约、心跳、版本和每次尝试。
- Worker 崩溃后，其他 Worker 可回收过期租约并从当前步骤继续；旧 Worker 的迟到提交会被拒绝。
- 状态更新与 SSE 事件在同一事务提交，SSE 支持 `Last-Event-ID` 断线回放。
- 人工审批是唯一、最终且持久化的决定；修复操作使用幂等键抑制重复执行。
- 支持自动重试、人工重试、取消请求和安全边界停止。

项目采用“至少一次调度 + 幂等步骤提交”，不宣称 exactly-once。详细保证、失败窗口和边界见[工作流可靠性说明](docs/workflow-reliability.md)。

## 一键运行

```bash
docker compose up --build
```

访问 <http://localhost:8000>，API 文档位于 <http://localhost:8000/docs>。Compose 会启动独立的 `api` 与 `worker` 服务，并共享 SQLite WAL 数据卷。默认离线模式不需要模型密钥。

## 本地开发

安装依赖后分别启动 API 和 Worker：

```bash
pip install -e ".[dev]"
uvicorn incident_lab.api:app --app-dir src --reload
```

```bash
python -m incident_lab.worker
```

## 验证

```bash
pytest --cov=incident_lab --cov-report=term-missing --cov-fail-under=90
cd frontend && npm run build
docker compose config
```

测试覆盖双 Worker 竞争、租约过期恢复、旧 Worker 提交拒绝、重试与取消、并发审批、事务回滚、SSE 回放、修复幂等，以及真实 Worker 子进程领取任务后崩溃并恢复。

## 项目边界

这是一个完成度较高、可复现的 Agent 故障响应模拟项目，不是真实生产级分布式控制平台。v1.2.0 仍使用 SQLite；真实 Prometheus/Loki、Redis/PostgreSQL 和远程命令执行均不在本版本范围内。

## 许可证

[MIT](LICENSE)
