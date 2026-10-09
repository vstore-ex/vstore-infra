#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
case "${1:-}" in
  redis-adopt|redis-check)
    command="${1#redis-}"
    shift
    exec python3 scripts/redis-policy.py "$command" "$@"
    ;;
  bootstrap|verify|rollback|recover)
    exec python3 scripts/release-state.py "$@"
    ;;
  *)
    exec python3 scripts/release-state.py compose "$@"
    ;;
esac
