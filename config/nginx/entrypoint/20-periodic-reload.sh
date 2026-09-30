#!/bin/sh
set -eu

# =============================================================================
# Background Nginx Periodic Reload Watchdog
# =============================================================================
# Runs in an isolated background subshell. Does not block container entrypoint.
# Safely sends SIGHUP (nginx -s reload) at 1-hour intervals to load renewed
# certificates into memory without requiring Docker daemon socket privileges.

(
    while :; do
        sleep 1h
        echo "==> [nginx-watchdog] Triggering periodic reload to sync active TLS certificates..."
        nginx -s reload 2>/dev/null || true
    done
) &
