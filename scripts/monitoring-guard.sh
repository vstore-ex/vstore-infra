#!/usr/bin/env bash
# Run as root on the Docker host. No application/container mutation.
set -euo pipefail
unit=vstore-monitoring.slice
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_unit=$root/monitoring/systemd/$unit
action=${1:-check}
[[ $# -le 1 && ($action == check || $action == install || $action == budget) ]] || { echo 'usage: monitoring-guard.sh [check|install|budget]' >&2; exit 2; }

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
memory_max=$(bytes "$(setting MemoryMax)")
swap_max=$(bytes "$(setting MemorySwapMax)")
tasks_max=$(setting TasksMax)
cpu_quota=$(setting CPUQuota)
[[ $tasks_max =~ ^[0-9]+$ && $cpu_quota =~ ^([0-9]+)%$ ]] || { echo 'Unsupported TasksMax/CPUQuota' >&2; exit 1; }
cpu_percent=${BASH_REMATCH[1]}
if [[ $action == budget ]]; then
  printf 'memory.max=%s\nmemory.swap.max=%s\npids.max=%s\ncpu.percent=%s\n' "$memory_max" "$swap_max" "$tasks_max" "$cpu_percent"
  exit 0
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
systemctl is-active --quiet "$unit"
path=$(systemctl show "$unit" --property=ControlGroup --value)
[[ $path == /vstore.slice/vstore-monitoring.slice ]] || { echo 'Unexpected parent cgroup' >&2; exit 1; }
base=/sys/fs/cgroup$path
[[ $(cat "$base/memory.max") == "$memory_max" && $(cat "$base/memory.swap.max") == "$swap_max" ]] || { echo 'Memory guard limits do not match reviewed budget' >&2; exit 1; }
[[ $(cat "$base/pids.max") == "$tasks_max" ]] || { echo 'Task guard limit does not match reviewed budget' >&2; exit 1; }
read -r quota period < "$base/cpu.max"
[[ $quota =~ ^[0-9]+$ && $period =~ ^[0-9]+$ ]] && (( quota * 100 == period * cpu_percent )) || { echo 'CPU guard limit does not match reviewed budget' >&2; exit 1; }
echo "Monitoring guard verified: $((memory_max / 1048576)) MiB RAM + $((swap_max / 1048576)) MiB swap; no application changes"
