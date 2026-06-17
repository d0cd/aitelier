#!/usr/bin/env bash
# Install macOS launchd agents so aitelier auto-starts at login, restarts on
# crash, and backs up Postgres daily. Templates live in docs/deploy/.
#
#   ./scripts/install-launchd.sh      (or: make service-install)
#
# Re-run to pick up template/path changes — it reloads cleanly.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LA_DIR="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

if [ "$(uname)" != "Darwin" ]; then
    echo "✗ launchd is macOS-only. On Linux, write an equivalent systemd unit." >&2
    exit 1
fi

# launchd starts agents with a minimal PATH. Build one that includes the dirs
# holding the tools our scripts call (uv, docker, curl), plus standard paths.
tool_dirs=""
for t in uv docker curl; do
    p="$(command -v "$t" 2>/dev/null || true)"
    if [ -n "$p" ]; then
        tool_dirs="$tool_dirs:$(dirname "$p")"
    else
        echo "  ! '$t' not on PATH — the agent may fail until it's installed" >&2
    fi
done
LAUNCHD_PATH="${tool_dirs#:}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$LA_DIR" "$REPO_ROOT/runs/logs"

_install_one() {
    local label="$1" template="$2"
    local dest="$LA_DIR/$label.plist"
    sed -e "s#@REPO_ROOT@#$REPO_ROOT#g" -e "s#@PATH@#$LAUNCHD_PATH#g" \
        "$REPO_ROOT/docs/deploy/$template" > "$dest"
    # Reload cleanly if it's already loaded.
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    if launchctl bootstrap "$DOMAIN" "$dest" 2>/dev/null; then
        echo "  ✓ loaded $label"
    else
        # Pre-Big-Sur fallback.
        launchctl unload "$dest" 2>/dev/null || true
        launchctl load -w "$dest"
        echo "  ✓ loaded $label (legacy load)"
    fi
}

echo "Installing launchd agents (domain $DOMAIN)…"
_install_one com.aitelier.agent  com.aitelier.agent.plist
_install_one com.aitelier.backup com.aitelier.backup.plist

cat <<EOF

Done. The service starts now, at every login, and restarts if it crashes.
A Postgres backup runs daily at 03:00.

  health:  curl -f http://127.0.0.1:7777/v1/health
  status:  launchctl print $DOMAIN/com.aitelier.agent
  logs:    runs/logs/aitelier.log , runs/logs/backup.log
  stop:    make service-uninstall

NOTE: for full reboot recovery, set Docker Desktop to "Start at login"
      (Settings → General) so Postgres/LiteLLM come back too.
EOF
