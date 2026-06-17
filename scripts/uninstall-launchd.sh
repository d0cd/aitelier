#!/usr/bin/env bash
# Remove the aitelier launchd agents (service supervisor + daily backup).
# Booting them out also stops the supervised service.
#
#   ./scripts/uninstall-launchd.sh    (or: make service-uninstall)

set -euo pipefail

LA_DIR="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

for label in com.aitelier.agent com.aitelier.backup; do
    dest="$LA_DIR/$label.plist"
    if launchctl bootout "$DOMAIN/$label" 2>/dev/null \
        || launchctl unload "$dest" 2>/dev/null; then
        echo "  ✓ unloaded $label"
    else
        echo "  - $label was not loaded"
    fi
    rm -f "$dest"
done

echo "Removed. The service no longer auto-starts or auto-restarts."
echo "Infra (Postgres/LiteLLM/Sandbox Agent) is untouched — stop it with 'make stop infra'."
