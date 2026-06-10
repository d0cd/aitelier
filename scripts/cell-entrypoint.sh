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
if [ -f /run/secrets/claude-credentials ]; then
    ln -sf /run/secrets/claude-credentials "$HOME/.claude/.credentials.json"
fi
if [ -f /run/secrets/codex-credentials ]; then
    ln -sf /run/secrets/codex-credentials "$HOME/.codex/auth.json"
fi

# Warden MITM-intercepts cell egress to enforce the per-cell allowlist.
# Without trusting warden's CA, every outbound HTTPS handshake fails
# with "unknown ca." We mount the CA pem as a secret and stitch a
# combined trust bundle under /tmp (the rootfs is RO).
if [ -f /run/secrets/warden-ca-cert ]; then
    CA_BUNDLE=/tmp/ca-bundle.crt
    cat /etc/ssl/certs/ca-certificates.crt /run/secrets/warden-ca-cert > "$CA_BUNDLE"
    export SSL_CERT_FILE="$CA_BUNDLE"
    export REQUESTS_CA_BUNDLE="$CA_BUNDLE"
    export CURL_CA_BUNDLE="$CA_BUNDLE"
    export NODE_EXTRA_CA_CERTS=/run/secrets/warden-ca-cert
fi

# `--no-token` is fine because brig's ingress already gates inbound
# traffic with bearer auth (`<cell-name>-ingress-token` secret).
# Bind 0.0.0.0 so the ingress reverse-proxy at the warden layer can
# reach SA from its bridge IP.
exec sandbox-agent server --host 0.0.0.0 --port 2468 --no-token
