# Security model

OpsPilot is an educational sandbox. It does not connect to or mutate production infrastructure.

## Safety guarantees in the demo

- The telemetry tools are read-only and explicitly allow-listed.
- Remediation previews are data, not shell commands.
- The executor only accepts commands prefixed with `opspilot ` and returns synthetic results.
- Medium- and high-risk actions require explicit human approval.
- Repeated execution is rejected after an incident reaches `resolved`.
- Evidence and tool calls are captured in the incident trace.

Do not wire the demo executor directly to Kubernetes, cloud, or shell credentials. A production adapter should use short-lived credentials, least-privilege roles, idempotency keys, policy-as-code, and a separate approval service.

To report a vulnerability, open a private GitHub security advisory rather than a public issue.

