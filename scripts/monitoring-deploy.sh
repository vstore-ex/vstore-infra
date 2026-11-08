#!/usr/bin/env bash
# Called by the infrastructure deployment after the application stack converged,
# from the deployment checkout and under the deployment lock:
#   monitoring-deploy.sh <previous commit or empty> <deployed commit>
# Applies changed monitoring configuration to a boot-managed monitoring service.
# Never fails the deployment: problems are reported as GitHub warnings.
set -uo pipefail
prev=${1:-}
new=${2:?usage: monitoring-deploy.sh <previous commit> <deployed commit>}
systemd_dir=${MONITORING_SYSTEMD_DIR:-/etc/systemd/system}
service=vstore-monitoring.service
units=(vstore-monitoring.service vstore-monitoring-watchdog.service vstore-monitoring-watchdog.timer)
paths=(monitoring scripts/monitoring-service.sh scripts/monitoring-guard.sh)

if ! systemctl is-enabled --quiet "$service" 2>/dev/null; then
  echo "==> Monitoring service not installed on this host; nothing to apply."
  exit 0
fi

# Installed unit files are copies; a changed unit needs a reviewed reinstall.
for name in "${units[@]}"; do
  if ! cmp -s "monitoring/systemd/$name" "$systemd_dir/$name"; then
    echo "::warning::Installed $name differs from $new. Stop monitoring, remove the old unit and run: sudo bash scripts/monitoring-guard.sh install-service --env-file <private compose.env>"
  fi
done

if [[ -n $prev ]] && git cat-file -e "$prev^{commit}" 2>/dev/null && git diff --quiet "$prev" "$new" -- "${paths[@]}"; then
  echo "==> Monitoring configuration unchanged; monitoring left as it is."
  exit 0
fi

# try-restart only restarts a running service: a stack the watchdog or an
# operator stopped stays stopped.
echo "==> Monitoring configuration changed; restarting monitoring if it is running."
if sudo -n systemctl try-restart "$service"; then
  echo "==> Monitoring restart requested; check with: sudo bash scripts/monitoring-guard.sh status"
else
  echo "::warning::Monitoring restart failed; the application deployment is unaffected. Check: journalctl -u $service"
fi
exit 0
