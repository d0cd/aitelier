# Contributing to aitelier

Thanks for your interest. aitelier is a self-hosted, OpenAI-compatible gateway
and control plane for LLM and coding-agent calls. It's pre-1.0 and maintained as
a small project — contributions are welcome, with no guarantee of how quickly
they can be reviewed.

## Development setup

Prerequisites: [`uv`](https://docs.astral.sh/uv/) (Python), `pnpm` (TypeScript),
Docker (Postgres + LiteLLM), and — for the agent path — a logged-in Claude Code
and/or Codex CLI (aitelier reads their local credentials; no manual API keys).

```bash
make install     # install all deps (Python + TypeScript)
make start       # Postgres + LiteLLM + Sandbox Agent + the service
make status      # what's running + healthchecks
make doctor      # preflight: ports, tools, creds, docker
```

See [`README.md`](README.md) for the runtime overview and
[`docs/INTEGRATION.md`](docs/INTEGRATION.md) for the API contract.

## Tests and linting

Every change must pass:

```bash
make test        # Python unit + smoke + TypeScript
make lint        # ruff + tsc
```

`make test-py` / `make test-ts` run one side only. The Postgres integration
tests require a database — CI provides one, and locally `make start` does.

## Conventions

- **Test-driven.** Write the failing test first, then make it pass.
- **No silent skips.** Tests assert their preconditions rather than skipping when
  a dependency is absent — a missing dependency is a failure, not a pass.
- **Generated code is generated.** Anything under a `_generated/` directory is
  produced from `schemas/v1/` by `scripts/generate-types.sh` — never hand-edit
  it; change the schema and regenerate.
- **Configuration is TOML.** App code reads zero environment variables; all
  configuration is layered TOML (see `aitelier.toml.example`).
- **Lockstep versioning.** All packages share one version, bumped together via
  `./scripts/release.sh X.Y.Z`.
- **Conventional commits.** Use `type(scope): summary` — e.g.
  `feat(agent): …`, `fix(errors): …`, `docs(schemas): …`, `chore: …`.

## Pull requests

1. Branch from `main`.
2. Make the change with tests; keep the diff focused on one concern.
3. Ensure `make test` and `make lint` pass.
4. Add a `CHANGELOG.md` entry under `## Unreleased` for anything user-visible.
5. Open the PR with a clear description and link any related issue.

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
