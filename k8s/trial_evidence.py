"""Record a trial time window and collect proxy logs without displaying task content."""
import datetime
import json
from pathlib import Path
import sys

def _proxy_pod_name(core):
    """The egress proxy is a Deployment; its pod name carries a hash."""
    pods = core.list_namespaced_pod("anyeval-sandbox", label_selector="anyeval.io/role=egress-proxy").items
    ready = [p for p in pods if p.status.phase == "Running"]
    if not ready:
        raise RuntimeError("no running egress-proxy pod")
    return ready[0].metadata.name

from spike2 import ROOT, apis, save

window = ROOT / "evidence/trial-window.json"
jobs = ROOT / "jobs"
if sys.argv[1] == "begin":
    save(window.name, {"started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       "jobs_before": [p.name for p in jobs.iterdir() if p.is_dir()] if jobs.exists() else []})
else:
    data = json.loads(window.read_text())
    new = [p for p in jobs.iterdir() if p.is_dir() and p.name not in data["jobs_before"]] if jobs.exists() else []
    data.update(finished=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                jobs_created=[str(p) for p in new])
    if new:
        try:
            _, core, _ = apis()
            elapsed = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(data["started"])
            logs = core.read_namespaced_pod_log(_proxy_pod_name(core), "anyeval-sandbox",
                                               since_seconds=int(elapsed.total_seconds()) + 2, _request_timeout=30)
            for job in new:
                (job / "iron-proxy.jsonl").write_text(logs)
            data["proxy_logs_collected"] = True
        except Exception as exc:
            data.update(proxy_logs_collected=False, log_error_type=type(exc).__name__, log_error=str(exc))
    else:
        data["status"] = "No new Harbor job was created"
    save(window.name, data)
