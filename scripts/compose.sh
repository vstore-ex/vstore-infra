#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
case "${1:-}" in
  bootstrap|verify|rollback)
    exec python3 scripts/release-state.py "$@"
    ;;
  *)
    exec python3 scripts/release-state.py compose "$@"
    ;;
esac
