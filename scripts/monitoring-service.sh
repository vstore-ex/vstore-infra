#!/usr/bin/env bash
# Lifecycle for the opt-in monitoring project, run by systemd as root.
# start: wait for the application, verify the slice budget, start monitoring.
# stop: stop monitoring containers; volumes and Blob history are kept.
# watchdog: stop monitoring when it threatens the application (timer, every minute).
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
action=${1:-}
unit=vstore-monitoring.service
env_file=${MONITORING_ENV_FILE:?set MONITORING_ENV_FILE in /etc/vstore-monitoring/service.env}
health_url=${MONITORING_HEALTH_URL:-http://127.0.0.1:8080/health}
health_timeout=${MONITORING_HEALTH_TIMEOUT:-300}
psi_io=${MONITORING_PSI_IO:-/proc/pressure/io}
slice_dir=${MONITORING_SLICE_DIR:-/sys/fs/cgroup/vstore.slice/vstore-monitoring.slice}
state_dir=${MONITORING_STATE_DIR:-/run/vstore-monitoring}
# Stop rule (README): host I/O pressure `some avg300` above this percentage.
io_limit=20

compose() {
  docker compose -p vstore-monitoring --env-file "$env_file" \
    -f "$root/monitoring/compose.yml" -f "$root/monitoring/production.yml" "$@"
}

oom_kills() {
  sed -n 's/^oom_kill //p' "$slice_dir/memory.events" 2>/dev/null || echo 0
}

start() {
  [[ -f $env_file ]] || { echo "Missing private monitoring env file: $env_file" >&2; exit 1; }
  # Let the application finish booting before monitoring competes for memory and disk.
  local deadline=$((SECONDS + health_timeout))
  until curl -fsS --max-time 5 -o /dev/null "$health_url"; do
    (( SECONDS < deadline )) || { echo "Application not healthy after ${health_timeout}s; monitoring not started" >&2; exit 1; }
    sleep 5
  done
  bash "$root/scripts/monitoring-guard.sh" check
  mkdir -p "$state_dir"
  oom_kills > "$state_dir/oom-baseline"
  compose up -d --wait
}

stop() {
  compose stop
}

watchdog() {
  systemctl is-active --quiet "$unit" || exit 0
  local reason=() pressure baseline
  pressure=$(sed -n 's/^some .*avg300=\([0-9.]*\).*/\1/p' "$psi_io")
  if [[ -n $pressure ]] && awk -v p="$pressure" -v l="$io_limit" 'BEGIN { exit !(p > l) }'; then
    reason+=("io some avg300=${pressure}% > ${io_limit}%")
  fi
  baseline=$(cat "$state_dir/oom-baseline" 2>/dev/null || echo 0)
  if (( $(oom_kills) > baseline )); then
    reason+=("oom_kill $(oom_kills) > $baseline at start")
  fi
  (( ${#reason[@]} )) || exit 0
  mkdir -p "$state_dir"
  printf '%s %s\n' "$(date -u +%FT%TZ)" "${reason[*]}" | tee "$state_dir/last-stop"
  echo "Stopping $unit; the application is not touched" >&2
  systemctl stop "$unit"
}

case $action in
  start) start ;;
  stop) stop ;;
  watchdog) watchdog ;;
  *) echo 'usage: monitoring-service.sh start|stop|watchdog' >&2; exit 2 ;;
esac
