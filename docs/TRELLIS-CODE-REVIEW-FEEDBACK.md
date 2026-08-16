# Trellis code-review resolution record for Aitelier

Date: 2026-08-15

Findings raised against `da36c4b247d256b47d8ec44c4f6f2c246b905312`; resolutions verified
independently against the working tree.

Status: all three findings are resolved, each with a regression that fails before its fix.
Two were re-scoped during independent verification — see the severity notes below. Retained
as acceptance history rather than an active backlog.

Review evidence:

- Trellis Run `e7aedf70-bc90-427b-ab29-171fec5da2ed` audited `core`, `docs`, and `sdks` read-only.
- Three independent claim-verifier calls confirmed the findings below against immutable source
  receipts.
- The six-call Run exhausted its soft call budget before lead settlement. These are verified
  partial findings, not a complete repository verdict.

## Resolved (filed P0, assessed P2) — Webhook SSRF validation was disconnected from the connection

### Problem

`is_public_url` resolves the webhook hostname and checks those addresses. `_deliver_once` then asks
the shared `httpx` client to post the original hostname, causing a separate DNS resolution. An
attacker-controlled hostname can return a public address during validation and a private,
loopback, link-local, or metadata-service address when the HTTP client connects. Revalidating at
delivery time narrows the window but does not bind validation to the connection it authorizes.

### Required change

Make the outbound connection use an address set that was validated for that attempt, while
preserving the original hostname for TLS SNI and the HTTP Host header. Alternatively use a transport
whose resolver rejects non-public connection targets at connect time. Keep redirects disabled, or
validate and bind every redirect hop through the same mechanism.

### Acceptance criteria

- A test makes validation DNS return a public address and connection DNS return a private address;
  no request reaches the private target.
- Mixed public/private DNS answers fail closed.
- HTTPS hostname verification and the original Host header remain correct.
- Retry attempts repeat the complete resolution-and-connect policy rather than reusing stale trust.

Resolution: `security.resolve_public_target` resolves once per attempt and returns a validated
literal address; `_deliver_once` connects to that address and carries the original name in the
`Host` header and TLS SNI, so virtual hosting and certificate verification are unaffected. A mixed
public/private answer set fails closed. Redirect following stays off by default.

Severity note: filed P0, assessed P2 for this deployment model. Exploitation needs an
authenticated caller *and* attacker-controlled low-TTL DNS, and yields a blind POST — the response
body is never returned, only a status code is persisted, and no endpoint exposes
`webhook_deliveries`. Worth fixing on the merits; not a P0 against a single-tenant gateway.

## Resolved P1 — Relative workspace paths bypassed symlink-component validation

### Problem

`validate_workspace_path` calls `_has_symlinked_component` only for absolute paths. Relative paths
therefore skip the symlink walk entirely. With the default empty `allowed_workspace_roots`, a
relative workspace, artifact fetch, or prepare-file path containing a symlinked component is handed
to the agent even though the documented boundary says symlink-bearing prefixes are refused.

### Required change

Either reject relative agent paths, or resolve them against one explicit trusted base before applying
the same component-by-component no-follow check and optional allowlist. Do not resolve relative
paths against ambient process working directory without declaring that directory as authority.

### Acceptance criteria

- Relative paths with a symlinked prefix are rejected when the allowlist is empty and when it is set.
- Ordinary relative paths have one documented, deterministic base or are rejected uniformly.
- Workspace, artifact-fetch, and prepare-file inputs use the same rule.
- Documentation does not imply protection against filesystem races or later agent-created symlinks.

Resolution: relative agent paths are refused outright. The "resolve against a trusted base" option
was discarded during verification: with `roots` set, the old code resolved relative paths against
*aitelier's* working directory while Sandbox Agent resolves against its own, so the allowlist could
both wrongly admit and wrongly reject. Absolute-only is the single rule that either check can
actually answer for.

## Resolved (filed P1, assessed P0) — Permission-policy exceptions failed open

### Problem

`AcpClient._respond_to_agent_request` defaults permission requests to allow. When a configured
`permission_decider` raises, the exception handler logs the error and explicitly sets `allow = True`.
A policy outage or bug therefore grants the tool action the policy was installed to control.

### Required change

When a configured decider fails, select a deny outcome and retain a bounded diagnostic. The existing
default behavior when no decider is configured is a separate deployment-policy choice; it does not
justify overriding a present but failed policy.

### Acceptance criteria

- A raising or cancelled permission decider returns a deny outcome and never an allow outcome.
- The failure remains visible without leaking sensitive request data.
- Explicit allow and explicit deny results remain unchanged.
- Tests exercise the actual JSON-RPC permission response, not only a helper return value.

Resolution: an exception in a configured decider selects deny; cancellation answers the ask and
then propagates; `_permission_tool_name` type-checks every candidate field so a malformed ask
yields None; and an unnameable ask is denied whenever an allowlist is configured. Regressions cover
the wire-level JSON-RPC response for all four paths, including the case where no reject option is
offered and the outcome must be `cancelled` rather than a silent allow.

Severity note: filed P1 as a policy-outage concern, assessed P0. The ask is agent-controlled, so a
non-string tool name (`{"toolCall": {"name": 123}}`) reached `.split()` and the fail-open handler
turned the resulting AttributeError into a grant — a `tool_allowlist` bypass reachable from the
untrusted side, not merely a degraded-policy case.

Correction to the filed acceptance criteria: the baseline test committed with this finding asserted
`optionId == "deny"` against option fixtures using `kind: "deny"`. The selector matches ACP's real
reject kinds (`reject_once` / `reject_always`), so that assertion could not have passed even after a
correct fix. Fixtures now use ACP kinds.

## Rejected candidate

The discovery lead also proposed redirect-based webhook SSRF. The shared `httpx.AsyncClient` does
not enable redirect following, so the current source does not establish that defect. Keep redirect
following disabled explicitly or apply the connection-bound SSRF policy to every hop if redirects
are introduced; no separate open finding is recorded here.
