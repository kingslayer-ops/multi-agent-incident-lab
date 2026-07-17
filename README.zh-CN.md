# Multi-Agent Incident Lab

**一个强调证据链、持久化恢复、人工审批与可复现评估的多 Agent 故障响应平台。**

[English](README.md) · [架构说明](docs/architecture.md) · [安全策略](SECURITY.md)

Multi-Agent Incident Lab 由 8 个职责明确的角色协作完成故障分级、指标分析、日志检索、变更关联、根因判断、安全审查、沙箱修复和恢复验证。默认模式不需要模型密钥；也可接入 OpenAI 兼容接口，并在超时、网络异常或结构化输出不合规时自动降级。

## 核心能力

- 结论必须引用指标、日志和变更证据 ID，完整过程可回放。
- SQLite WAL 保存事故、审批状态和评估结果，服务重启后仍可恢复。
- 所有变更动作必须经过确定性安全策略与人工审批。
- 修复执行器仅识别 `incident-lab` 沙箱命令，不调用系统 Shell。
- 内置 12 类故障与回归评估，统计根因准确率、证据覆盖率、危险动作率和耗时。
- 提供 React 运维控制台、FastAPI、SSE 事件回放、Docker Compose 和 GitHub Actions。

## 一键运行

```bash
docker compose up --build
```

访问 <http://localhost:8000>，API 文档位于 <http://localhost:8000/docs>。默认数据保存在 Docker 命名卷 `incident-lab-data` 中。

本地开发、模型接入、API 端点和测试命令请查看 [英文 README](README.md)。

## 项目边界

这是一个安全、可复现的生产故障模拟平台，不会执行真实云平台、Kubernetes 或操作系统命令。接入真实遥测系统前请先阅读 [SECURITY.md](SECURITY.md)。

## 许可证

[MIT](LICENSE)
