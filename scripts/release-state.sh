#!/usr/bin/env bash
# Source only after the caller acquires .deploy.lock on descriptor 200.
release_state() {
  python3 scripts/release-state.py --lock-fd 200 "$@"
}

release_load() {
  local values key value
  release_state bootstrap --if-missing || return
  values=$(release_state env) || return
  while IFS='=' read -r key value; do
    export "$key=$value"
  done <<< "$values"
}

release_compose() {
  # Candidates use exported digests; never modify successful state before health verification.
  docker compose -f docker-compose.yml "$@"
}
