# Security model

Multi-Agent Incident Lab is an educational sandbox. In live telemetry mode it can make read-only HTTP queries to operator-configured Prometheus and Loki endpoints. It never mutates those systems or executes production remediation.

## Safety guarantees in the demo

- The telemetry tools are read-only and explicitly allow-listed.
- Remediation previews are data, not shell commands.
- The executor only accepts commands prefixed with `incident-lab ` and returns synthetic results.
- Medium- and high-risk actions require explicit human approval.
- Repeated execution is rejected after an incident reaches `resolved`.
- Evidence and tool calls are captured in the incident trace.
- Telemetry credentials must be provided through environment variables, cannot be embedded in endpoint URLs, and are never copied into evidence or sanitized error messages.
- HTTPS uses the operating-system trust store. There is no insecure skip-verification setting.
- Query windows, series counts, line counts, response size, and request timeouts are bounded.

Use read-only, least-privilege credentials for Prometheus/Loki and put an authenticating reverse proxy in front of Loki where required. Do not commit `.env` files or expose credentials to the browser. Do not wire the demo executor directly to Kubernetes, cloud, or shell credentials. A production remediation adapter should use short-lived credentials, least-privilege roles, idempotency keys, policy-as-code, and a separate approval service.

To report a vulnerability, open a private GitHub security advisory rather than a public issue.
