# Active Trellis code-review feedback for Aitelier

Date: 2026-08-07

Verified against Aitelier `9ed53231842636285c24ad33401efcb6e33a233a`

This is an active backlog, not a history of earlier findings. Resolved items were removed.
The remaining entries were independently checked against current code after two retained
Trellis persistent-lead reviews:

- `755ebd4b-7bfa-4692-a5fa-4df063de28ed` reviewed the main hardening commit
  (27 files, 1,194 changed lines).
- `d509c4f9-739b-407f-a297-7ae72486af3d` reviewed its follow-ups
  (15 files, 393 changed lines).

Both current Brig terminal handshakes pass: Claude Code `2.1.144` through adapter `0.36.1`,
and Codex CLI `0.137.0` through `codex-acp` `0.16.0`. The active runtime defects below do
not mean that those basic terminal turns are unavailable.

## P0 — ACP error detail can disclose an unlabelled credential

### Problem

`_acp_exception_text` appends `AcpError.data` to the consumer-visible error message. The
three caller paths apply `scrub_error_text`, whose intentionally conservative contract only
redacts named credentials and recognizable headers/query parameters. It does not redact a
bare setup token, refresh token, or other high-entropy credential included in free-form ACP
error data.

That message can be persisted in the run row and returned through API errors, webhooks, and
the terminal doctor probe. Preserving the backend diagnosis is useful, but JSON-RPC error
data is an upstream trust boundary and needs the stronger treatment used for upstream
provider bodies.

### Verification

A fake marker shaped as `sk-ant-oat01-...` was placed in `AcpError.data.message` and passed
through `_error_result`. The complete marker survived in `error_msg`. The existing regression
only covers `Authorization: Bearer ...`, so it does not exercise this path.

### Required change

- Apply structured redaction and the high-recall token/entropy scrubber to ACP error data
  before it is concatenated, persisted, logged, or returned.
- Retain useful non-secret text such as “refresh token was revoked.”
- Do not create a second independent secret-pattern implementation; use the existing error
  scrubbing boundary or factor one shared upstream-detail helper.

### Acceptance criteria

- Tests cover a bare `sk-*`/`sk-ant-oat*` marker, named refresh-token fields, nested/serialized
  error data, authorization headers, and query-string credentials.
- No tested secret appears in `error_msg`, stored run data, webhook payloads, doctor output,
  or ordinary logs.
- The non-secret backend diagnosis remains visible.

## P1 — Streaming structured-output failure is emitted as success

### Problem

Structured-output validation can change a terminal aggregate to:

```text
status=error, finish_reason=error, error_type=SchemaViolation
```

`_build_done_event` still labels that aggregate as `type: done`. The SSE dispatcher sends
every `done` event through `_stream_chunk_for_done`, which hard-codes the wire
`finish_reason` to `stop`. The durable run therefore fails correctly while the streaming
client receives a successful terminal chunk followed by `[DONE]`.

This violates the rule that wire outcome and durable outcome describe the same terminal
result.

### Verification

A focused reproduction passed a `done` event with `status=error` and
`error_type=SchemaViolation` to `_stream_chunk_for_done`. It produced:

```text
wire finish_reason=stop
durable status=error
durable error_type=SchemaViolation
```

The event dispatcher has no branch that corrects this mismatch.

### Required change

Route an error-status terminal aggregate through the existing streaming error path, or emit
an `error` event from the provider boundary when structured-output conformance fails. Do not
send a success terminal chunk for the same result.

### Acceptance criteria

- A streaming `json_schema` or `json_object` conformance failure emits a typed
  `SchemaViolation` error frame and no `finish_reason: stop` success chunk.
- The run finalizes once as `failed`, with its raw content and recoverable parsed value
  retained for diagnosis.
- Successful `done` events retain the current OpenAI-compatible terminal shape and usage
  projection.
- Regression tests cover both the SSE body and the durable run state.

## P1 — Claude setup-token behavior is inconsistent in host and Docker modes

### Problem

The quick-start contract says Claude agent and direct-model routes reuse the long-lived
`claude-oauth-token`. `scripts/start.sh` reads that Brig secret and writes it to
`docker/.env` as `ANTHROPIC_API_KEY`, which enables the direct LiteLLM route.

When Sandbox Agent runs in host mode, a token loaded from the Brig secret is not exported as
`CLAUDE_CODE_OAUTH_TOKEN` to the launched process. In Docker mode, the SA service mounts
legacy `~/.claude` credentials but receives neither that variable nor a setup-token secret.
Consequently direct Claude calls can work while `agent:claude/...` still depends on a
different or stale login.

The active Brig deployment is not affected: its cell already mounts `claude-oauth-token`,
and its live Claude terminal handshake passes.

### Verification

- The credential materialization block scopes `setup_token` inside Python and only writes
  `ANTHROPIC_API_KEY` to `docker/.env`.
- The host `sandbox-agent server` launch has no setup-token environment assignment.
- The Docker Compose SA service has no Claude setup-token environment/secret mapping.

### Required change

Choose and implement one truthful cross-mode contract. The preferred shape is to materialize
the setup token once and pass it to Sandbox Agent through a mode-appropriate secret boundary:

- host: `CLAUDE_CODE_OAUTH_TOKEN` scoped to the SA process;
- Docker: a read-only secret/file consumed by the container entrypoint;
- Brig: the existing `claude-oauth-token` secret mount.

If host or Docker intentionally continue to use their own Claude CLI login instead, narrow
the quick-start and doctor claims and report the two credential domains separately.

### Acceptance criteria

- With only the documented setup token configured, direct Claude and `agent:claude/...`
  terminal probes pass in each advertised local deployment mode.
- No setup token is printed, copied into an image layer, or exposed in generated diagnostics.
- Mode-specific tests or probes fail clearly when the credential required by that mode is
  absent or stale.

## P2 — Inner-model discovery documentation contradicts the implementation

### Problem

`docs/INTEGRATION.md` first describes `aitelier_inner_llms` as the authoritative backend
model list, then says it is not authoritative. Its backend table also says Codex and other
non-Claude agents use `session/set_model`, while current code applies the backend-advertised
model configuration option and uses `session/set_model` only as a legacy fallback.

### Required change

- State that `aitelier_inner_llms` is authoritative when the live probe provides it; omission
  means capability state is unavailable, not that a hard-coded list becomes authoritative.
- Describe advertised model configuration as the normal path and `session/set_model` as the
  compatibility fallback.
- Remove obsolete examples or warnings that imply a different routing path.

### Acceptance criteria

- The model-discovery overview, backend table, selection section, and doctor guidance express
  one consistent contract.
- Documentation examples match a live `GET /v1/models?agent_backend=codex` response and the
  tested configuration method.

## P2 — Brig deployment documentation describes the obsolete topology

### Problem

The integration guide recommends running Aitelier and Sandbox Agent in one Brig cell with
ingress on port 7777. The implemented and tested deployment is different:

- the Brig cell hosts only Sandbox Agent;
- Aitelier runs outside the cell;
- cell ingress forwards Sandbox Agent on port 2468; and
- only agent-runtime credentials belong inside that cell.

Following the obsolete text can expose the wrong service, mount unnecessary Aitelier secrets
into the agent sandbox, and erase the intended ownership boundary.

### Required change

Rewrite the Brig deployment and deployment-test sections to match
`docs/deploy/sandbox-agent.cell.yaml`, `scripts/cell-entrypoint.sh`, and the current shape
tests. The guide should show Aitelier connecting to the cell's authenticated SA ingress,
not running inside the cell.

### Acceptance criteria

- Every Brig example uses the SA-only cell, ingress port 2468, and external Aitelier service.
- Secret and workspace mounts are assigned to the component that consumes them.
- Narrative claims and deployment-shape tests agree on the same topology.

## Verified non-findings

### Immediate cancellation does not strand the acknowledged row

The review claimed that a client could cancel the registered outer task before its coroutine
started, leaving the precreated row `pending`. That scenario does not follow the public API
scheduling boundary: the new task is queued before a client can receive the run ID and issue
the follow-up request. Once the task begins, preparation and provider execution have terminal
cancellation settlement.

A live submit-immediate-cancel-wait reproduction reached `state=cancelled` in 192 ms with a
canonical `Cancelled` result (run `d69e085a4f22a0c1f5cfd04ab0c9b8a1`). This finding is
closed. A focused regression would still be useful protection, but there is no verified
pending-row defect to fix.

### Hard tool budgeting remains an advertised backend limitation

Aitelier reports `aitelier_capabilities.hardToolBudget: false` because the selected backend
does not expose an enforceable work budget or synthesis reserve. Prompt guidance is not
misrepresented as enforcement. Implementing the backend capability remains useful future
work, but it is not an unaddressed correctness claim in Aitelier.
