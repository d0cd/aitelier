#!/bin/sh
# Entrypoint for the sandbox-agent brig cell.
#
# This cell hosts ONLY Sandbox Agent. Aitelier itself runs elsewhere and
# talks to SA-in-cell via brig's ingress reverse proxy. SA's job here is
# pure: receive ACP requests, spawn the requested agent CLI inside the
# cell's filesystem, stream results back.
#
# The cell rootfs is read-only (only /work, /tmp, /run are writable), so
# we relocate HOME under /tmp and symlink brig-mounted secrets there.

set -e

export HOME=/tmp/home
mkdir -p "$HOME/.claude" "$HOME/.codex"

# Brig mounts secrets read-only at /run/secrets/<name>. Symlink (don't
# copy) so credential rotation via `brig secrets add` takes effect at
# the next cell restart without rebuilding the image.
#
# Claude auth, preferred path: a long-lived OAuth token from
# `claude setup-token` (valid ~1 year, no refresh-token rotation). claude-code
# reads it from CLAUDE_CODE_OAUTH_TOKEN, so there's no credentials file to mount
# and nothing goes stale between restarts. Falls back to the rotating
# .credentials.json snapshot when the token secret isn't registered.
if [ -f /run/secrets/claude-oauth-token ]; then
    CLAUDE_CODE_OAUTH_TOKEN="$(cat /run/secrets/claude-oauth-token)"
    export CLAUDE_CODE_OAUTH_TOKEN
elif [ -f /run/secrets/claude-credentials ]; then
    ln -sf /run/secrets/claude-credentials "$HOME/.claude/.credentials.json"
fi
if [ -f /run/secrets/codex-credentials ]; then
    ln -sf /run/secrets/codex-credentials "$HOME/.codex/auth.json"
fi

# Warden MITM-intercepts cell egress; HTTPS clients need warden's CA
# in their trust store or every handshake fails with "unknown ca."
# Brig 0.3.0+ does this automatically: `trust_warden_ca: true` (default
# in cell yaml) stages a combined system+warden bundle at
# /run/brig/ca-bundle.crt and auto-exports SSL_CERT_FILE,
# REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE, NODE_EXTRA_CA_CERTS. We used to
# stitch this ourselves from a `warden-ca-cert` secret, but that
# cached the CA in the secret and broke on every warden restart
# (warden regenerates its CA, secret stays stale → silent TLS hangs).
# Nothing to do here.

# `--no-token` is fine because brig's ingress already gates inbound
# traffic with bearer auth (`<cell-name>-ingress-token` secret).
# Bind 0.0.0.0 so the ingress reverse-proxy at the warden layer can
# reach SA from its bridge IP.
#
# `--no-telemetry` disables SA's anonymous usage POSTs to
# `tc.rivet.dev`. Without it, warden blocks the call (not in
# policy.allow), SA retries, and the retry loop adds ~30-90s to the
# agent-startup critical path. Allowing the host would let SA phone
# home with usage data we don't owe Rivet anyway.
#
# SA self-redirects logs to /tmp/home/.local/share/sandbox-agent/logs/
# (the entrypoint's redirect is replaced at runtime). `brig cell logs`
# will be empty as a result — to inspect SA's actual logs:
#   brig cell exec sandbox-agent sh -c \
#     'tail -50 /tmp/home/.local/share/sandbox-agent/logs/log-*'
exec sandbox-agent server --host 0.0.0.0 --port 2468 --no-token --no-telemetry
