"""Run every provisioned dashboard panel query through Grafana and report which return data.

Used by smoke_monitoring.py against the running fixture. Template variables are widened
to match anything, Grafana's interval macros get fixed values.
"""

import json
import re
import urllib.parse

INTERVALS = {"__rate_interval": "2m", "__interval": "1m", "interval": "5m", "__range": "1h",
             "__range_s": "3600", "__interval_ms": "60000", "__auto_interval_interval": "1m"}
# VStore panels that may legitimately be empty in a short fixture run.
EXPECTED_EMPTY = {
    "Since last metric block upload",      # no upload before ~3 h of uptime
    "Firing alerts",                       # empty when healthy
    "Error-looking log lines per service",  # depends on errors in the window
    "Recent error-looking lines (backend, worker, proxy)",
    "Blob operation failures per second",
    "Upload failures (1h)",
    "Blocks served from Blob",
    "Seconds since last metric block upload to Blob",
}
# Docker Desktop exposes no per-container cgroup metrics and its root filesystem is an
# overlay that node exporter excludes; only Linux (production) can fill these.
LINUX_ONLY = __import__("re").compile(r"\b(container_[a-z_]+|node_filesystem_[a-z_]+)")


def substitute(expr):
    # Free-text variables in Loki line filters default to empty, which matches every line.
    expr = re.sub(r'(\|[=~]\s*)"\$\{?\w+\}?"', r'\1""', expr)
    expr = re.sub(r'(=|!=)~?"\$\{?(\w+)(?::\w+)?\}?"', lambda m: ('!~' if m.group(1) == '!=' else '=~') + '".+"', expr)
    expr = re.sub(r'=~"([^"]*)\$\{?\w+(?::\w+)?\}?([^"]*)"', r'=~"\1.+\2"', expr)
    for name, value in INTERVALS.items():
        expr = re.sub(r"\$\{?" + name + r"\}?(?!\w)", value, expr)
    expr = re.sub(r"\[\[\w+\]\]|\$\{?\w+(?::\w+)?\}?", ".+", expr)
    return expr


def panels(items):
    for panel in items:
        yield panel
        yield from panels(panel.get("panels", []))


def has_data(grafana, uid, expr):
    if uid == "vstore-logs":
        query = urllib.parse.urlencode({"query": expr, "limit": 5})
        result = grafana(f"/api/datasources/proxy/uid/vstore-logs/loki/api/v1/query_range?{query}")
    else:
        query = urllib.parse.urlencode({"query": expr})
        result = grafana(f"/api/datasources/proxy/uid/vstore-metrics/api/v1/query?{query}")
    data = result.get("data", {}).get("result")
    if isinstance(data, list):
        return bool(data)
    return data is not None


def coverage(grafana):
    report = {}
    for item in grafana("/api/search?type=dash-db"):
        dashboard = grafana(f"/api/dashboards/uid/{item['uid']}")["dashboard"]
        rows = []
        legacy_rows = [p for row in dashboard.get("rows", []) for p in row.get("panels", [])]
        for panel in panels(dashboard.get("panels", []) + legacy_rows):
            targets = [t for t in panel.get("targets", []) if t.get("expr")]
            if not targets:
                continue
            uid = (panel.get("datasource") or {}).get("uid")
            ok = False
            for target in targets:
                source = (target.get("datasource") or {}).get("uid", uid)
                try:
                    ok = ok or has_data(grafana, source, substitute(target["expr"]))
                except Exception:  # noqa: BLE001 - a failing query counts as no data
                    pass
            rows.append((panel.get("title", ""), ok, " ".join(t["expr"] for t in targets)))
        report[dashboard["title"]] = {"folder": item.get("folderTitle", "General"), "panels": rows}
    return report


def summarize(report):
    lines = []
    for title, entry in sorted(report.items()):
        rows = entry["panels"]
        filled = sum(ok for _, ok, _ in rows)
        lines.append(f"{entry['folder']}/{title}: {filled}/{len(rows)} panels with data")
    return "\n".join(lines)


def unexpected_empty(report, linux=True):
    return {title: [p for p, ok, exprs in entry["panels"]
                    if not ok and p not in EXPECTED_EMPTY and (linux or not LINUX_ONLY.search(exprs))]
            for title, entry in report.items() if entry["folder"] == "VStore"}


if __name__ == "__main__":
    print(json.dumps({"example": substitute('rate(x{job="$job",i=~"$node"}[$__rate_interval])')}))
