# Security Policy

## Supported versions

aitelier is pre-1.0. Only the latest release (and `main`) receives security
fixes; there are no backports to older tags.

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability):
on the repository, go to **Security → Report a vulnerability**. You'll get a
private advisory thread to share details and coordinate a fix.

Please include a description, reproduction steps, and the affected version or
commit. There's no formal SLA, but reports are taken seriously and acknowledged
as soon as is practical.

## Security model — know before you deploy

aitelier is designed to run **locally, for a single user**, not as an
internet-facing multi-tenant service. A few properties matter:

- **Binds to localhost.** The HTTP server is intended for `127.0.0.1`
  invocation by local tools, not public exposure.
- **`mode = "host"` runs the agent as you.** With the host-installed Sandbox
  Agent, coding agents run as your user and inherit your full host permissions.
  Do not expose `/v1/*` to untrusted callers in host mode. For stronger
  isolation, run the Sandbox Agent in Docker or a remote sandbox.
- **Credentials are read from local files.** The agent path uses your existing
  Claude Code / Codex credentials (and a long-lived setup token in the macOS
  Keychain); no provider keys are stored in the repo.
- **Optional auth for hosted mode.** Set `service.api_key` to require
  `Authorization: Bearer <key>` on `/v1/*`, and `service.webhook_secret` to sign
  outbound webhooks.
- **Workspace allowlisting.** `service.allowed_workspace_roots` bounds which
  host paths a caller may hand to the agent.
- **Secret scrubbing.** Upstream provider error bodies are scrubbed (regex +
  entropy heuristics) before they're surfaced or logged.

If you expose aitelier beyond localhost, put it behind authentication and a
trusted network boundary, and prefer a sandboxed (non-host) agent mode.
