#!/usr/bin/env bash
# Run as root on the Docker host. No application/container mutation.
set -euo pipefail
unit=vstore-monitoring.slice
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
action=${1:-check}
[[ $# -le 1 && ($action == check || $action == install) ]] || { echo 'usage: monitoring-guard.sh [check|install]' >&2; exit 2; }
[[ $(uname -s) == Linux && -d /run/systemd/system && -f /sys/fs/cgroup/cgroup.controllers ]] || { echo 'Requires Linux systemd and cgroup v2' >&2; exit 1; }
[[ $(docker info --format '{{.CgroupDriver}} {{.CgroupVersion}}') == 'systemd 2' ]] || { echo 'Requires Docker systemd cgroup v2 driver' >&2; exit 1; }
if [[ $action == install ]]; then
  [[ $EUID == 0 ]] || { echo 'install requires root' >&2; exit 1; }
  target=/etc/systemd/system/$unit
  # Refuse to replace unrelated host policy.
  if [[ -e $target || -L $target ]]; then
    [[ ! -L $target ]] && cmp -s "$root/monitoring/systemd/$unit" "$target" || { echo 'Existing slice differs; review host policy first' >&2; exit 1; }
  else
    install -m 0644 "$root/monitoring/systemd/$unit" "$target"
  fi
  systemctl daemon-reload
  systemctl enable --now "$unit"
fi
systemctl is-active --quiet "$unit"
path=$(systemctl show "$unit" --property=ControlGroup --value)
[[ $path == /vstore.slice/vstore-monitoring.slice ]] || { echo 'Unexpected parent cgroup' >&2; exit 1; }
base=/sys/fs/cgroup$path
[[ $(cat "$base/memory.max") == 268435456 && $(cat "$base/memory.swap.max") == 536870912 ]] || { echo 'Memory guard limits do not match reviewed budget' >&2; exit 1; }
[[ $(cat "$base/pids.max") == 256 ]] || { echo 'Task guard limit does not match reviewed budget' >&2; exit 1; }
read -r quota period < "$base/cpu.max"
[[ $quota =~ ^[0-9]+$ && $period =~ ^[0-9]+$ ]] && (( quota * 2 == period )) || { echo 'CPU guard limit does not match reviewed budget' >&2; exit 1; }
echo 'Monitoring guard verified: 256 MiB RAM + 512 MiB swap; no application changes'
