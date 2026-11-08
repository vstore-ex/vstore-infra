#!/usr/bin/env bash
# Run as root on the Docker host. No application/container mutation.
set -euo pipefail
unit=vstore-monitoring.slice
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_unit=$root/monitoring/systemd/$unit
action=${1:-check}
usage='usage: monitoring-guard.sh [check|install|budget|status|install-service --env-file <private compose.env>]'
service_units=(vstore-monitoring.service vstore-monitoring-watchdog.service vstore-monitoring-watchdog.timer)
service_env=/etc/vstore-monitoring/service.env
private_env=
case $action in
  check|install|budget|status) [[ $# -le 1 ]] || { echo "$usage" >&2; exit 2; } ;;
  install-service)
    [[ $# == 3 && $2 == --env-file ]] || { echo "$usage" >&2; exit 2; }
    private_env=$3 ;;
  *) echo "$usage" >&2; exit 2 ;;
esac

# The reviewed slice unit is the only source of the budget.
setting() {
  local value
  value=$(sed -n "s/^$1=//p" "$source_unit")
  [[ -n $value && $value != *$'\n'* ]] || { echo "Slice unit must set $1 exactly once" >&2; exit 1; }
  printf '%s' "$value"
}
bytes() {
  [[ $1 =~ ^([0-9]+)([KMG]?)$ ]] || { echo "Unsupported size: $1" >&2; exit 1; }
  local n=${BASH_REMATCH[1]}
  case ${BASH_REMATCH[2]} in
    K) n=$((n * 1024)) ;; M) n=$((n * 1024 * 1024)) ;; G) n=$((n * 1024 * 1024 * 1024)) ;;
  esac
  echo "$n"
}
memory_high=$(bytes "$(setting MemoryHigh)")
memory_max=$(bytes "$(setting MemoryMax)")
(( memory_high < memory_max )) || { echo "MemoryHigh must be below MemoryMax" >&2; exit 1; }
swap_max=$(bytes "$(setting MemorySwapMax)")
tasks_max=$(setting TasksMax)
cpu_quota=$(setting CPUQuota)
[[ $tasks_max =~ ^[0-9]+$ && $cpu_quota =~ ^([0-9]+)%$ ]] || { echo 'Unsupported TasksMax/CPUQuota' >&2; exit 1; }
cpu_percent=${BASH_REMATCH[1]}
if [[ $action == budget ]]; then
  printf 'memory.high=%s\nmemory.max=%s\nmemory.swap.max=%s\npids.max=%s\ncpu.percent=%s\n' "$memory_high" "$memory_max" "$swap_max" "$tasks_max" "$cpu_percent"
  exit 0
fi

if [[ $action == status ]]; then
  for name in "$unit" "${service_units[@]}"; do
    printf '%-36s enabled=%s active=%s\n' "$name" "$(systemctl is-enabled "$name" 2>/dev/null || true)" "$(systemctl is-active "$name" 2>/dev/null || true)"
  done
  echo "last watchdog stop: $(cat /run/vstore-monitoring/last-stop 2>/dev/null || echo none since boot)"
  exec bash "$0" check
fi

[[ $(uname -s) == Linux && -d /run/systemd/system && -f /sys/fs/cgroup/cgroup.controllers ]] || { echo 'Requires Linux systemd and cgroup v2' >&2; exit 1; }
[[ $(docker info --format '{{.CgroupDriver}} {{.CgroupVersion}}') == 'systemd 2' ]] || { echo 'Requires Docker systemd cgroup v2 driver' >&2; exit 1; }
if [[ $action == install ]]; then
  [[ $EUID == 0 ]] || { echo 'install requires root' >&2; exit 1; }
  target=/etc/systemd/system/$unit
  # Refuse to replace unrelated or previously reviewed host policy in place.
  if [[ -e $target || -L $target ]]; then
    [[ ! -L $target ]] && cmp -s "$source_unit" "$target" || { echo 'Existing slice differs; stop monitoring, verify the slice is empty and remove the old unit first' >&2; exit 1; }
  else
    install -m 0644 "$source_unit" "$target"
  fi
  systemctl daemon-reload
  systemctl enable --now "$unit"
fi
if [[ $action == install-service ]]; then
  [[ $EUID == 0 ]] || { echo 'install-service requires root' >&2; exit 1; }
  # The private env file must be the unprivileged output of `monitoring.py prepare`.
  [[ $private_env == /* && -f $private_env && ! -L $private_env ]] || { echo 'Use the absolute path of an existing private compose.env' >&2; exit 1; }
  private_dir=$(dirname -- "$private_env")
  [[ $(stat -c '%a' "$private_env") == 600 && $(stat -c '%a' "$private_dir") == 700 ]] || { echo 'Private env file must be 0600 in a 0700 directory' >&2; exit 1; }
  (( $(stat -c '%u' "$private_env") != 0 )) || { echo 'Private configuration must belong to the unprivileged monitoring owner' >&2; exit 1; }
  grep -qxF "MONITORING_SECRETS_DIR=\"$private_dir\"" "$private_env" || { echo 'compose.env does not point at its own directory' >&2; exit 1; }
  wanted=$(printf 'VSTORE_DEPLOY_PATH=%s\nMONITORING_ENV_FILE=%s\n' "$root" "$private_env")
  if [[ -e $service_env || -L $service_env ]]; then
    [[ ! -L $service_env && $(cat "$service_env") == "$wanted" ]] || { echo "Existing $service_env differs; review and remove it first" >&2; exit 1; }
  else
    install -d -m 0755 "$(dirname -- "$service_env")"
    printf '%s\n' "$wanted" > "$service_env.tmp" && chmod 0644 "$service_env.tmp" && mv "$service_env.tmp" "$service_env"
  fi
  for name in "${service_units[@]}"; do
    target=/etc/systemd/system/$name
    if [[ -e $target || -L $target ]]; then
      [[ ! -L $target ]] && cmp -s "$root/monitoring/systemd/$name" "$target" || { echo "Existing $name differs; stop monitoring and remove the old unit first" >&2; exit 1; }
    else
      install -m 0644 "$root/monitoring/systemd/$name" "$target"
    fi
  done
  systemctl daemon-reload
  # Enabled for the next boot only; starting now is a separate, explicit step.
  systemctl enable vstore-monitoring.service vstore-monitoring-watchdog.timer
fi
systemctl is-active --quiet "$unit"
path=$(systemctl show "$unit" --property=ControlGroup --value)
[[ $path == /vstore.slice/vstore-monitoring.slice ]] || { echo 'Unexpected parent cgroup' >&2; exit 1; }
base=/sys/fs/cgroup$path
[[ $(cat "$base/memory.high") == "$memory_high" && $(cat "$base/memory.max") == "$memory_max" && $(cat "$base/memory.swap.max") == "$swap_max" ]] || { echo 'Memory guard limits do not match reviewed budget' >&2; exit 1; }
[[ $(cat "$base/pids.max") == "$tasks_max" ]] || { echo 'Task guard limit does not match reviewed budget' >&2; exit 1; }
read -r quota period < "$base/cpu.max"
[[ $quota =~ ^[0-9]+$ && $period =~ ^[0-9]+$ ]] && (( quota * 100 == period * cpu_percent )) || { echo 'CPU guard limit does not match reviewed budget' >&2; exit 1; }
echo "Monitoring guard verified: reclaim from $((memory_high / 1048576)) MiB, $((memory_max / 1048576)) MiB RAM + $((swap_max / 1048576)) MiB swap; no application changes"
