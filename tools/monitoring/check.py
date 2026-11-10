#!/usr/bin/env python3
"""Focused checks. Requires a verified promtool; no stack is installed or changed."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def run(command, **kwargs):
    print("+ " + " ".join(str(part) for part in command), flush=True)
    return subprocess.run(command, cwd=ROOT, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--promtool", default=shutil.which("promtool"))
    parser.add_argument("--cxx", default=shutil.which("c++"))
    parser.add_argument("--kubeconform")
    args = parser.parse_args()
    if not args.promtool or not args.cxx:
        parser.error("verified promtool and C++23 compiler required (set --promtool and --cxx)")
    run([sys.executable, "tools/monitoring/generate.py", "--check"])
    run([sys.executable, "-m", "unittest", "discover", "-s", "tools/monitoring/tests", "-v"])
    run([args.promtool, "check", "config", "--syntax-only", "deploy/monitoring/prometheus/prometheus.yml", "deploy/monitoring/prometheus/prometheus-alerting.yml"])
    run([args.promtool, "check", "rules", "deploy/monitoring/prometheus/rules/recording.yml"])
    run([args.promtool, "test", "rules", "tools/monitoring/tests/rules.test.yml"])
    with tempfile.TemporaryDirectory(prefix="glamdb-monitoring-check-") as directory:
        temporary = Path(directory)
        binary = temporary / "native-metrics-test"
        run([args.cxx, "-std=c++23", "-Wall", "-Wextra", "-Wpedantic", "-Werror", "-pthread",
             "-fsanitize=address,undefined", "-I.", "-Ipublic", "core/telemetry/metrics.cpp",
             "tools/monitoring/tests/native_metrics_test.cpp", "-o", str(binary)])
        result = run([str(binary), str(temporary / "native.prom")], capture_output=True)
        run([args.promtool, "check", "metrics"], input=result.stdout)
        samples = [line for line in result.stdout.decode().splitlines() if line and not line.startswith("#")]
        if len(samples) > 1000:
            raise SystemExit("native series budget exceeded")
        print(f"Native fixture: {len(samples)} series, {len(result.stdout)} bytes; no workload-health claim")
        manifest = temporary / "kubernetes.json"
        with manifest.open("w") as output:
            run([sys.executable, "tools/monitoring/kubernetes.py", "--with-cadvisor"], stdout=output)
        config = next(item["data"]["prometheus.yml"] for item in json.loads(manifest.read_text())["items"] if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "prometheus-config")
        (temporary / "prometheus-kubernetes.yml").write_text(config)
        run([args.promtool, "check", "config", "--syntax-only", str(temporary / "prometheus-kubernetes.yml")])
        queries = []
        import re
        for dashboard_path in sorted((ROOT / "deploy/monitoring/grafana/dashboards").glob("*.json")):
            dashboard = json.loads(dashboard_path.read_text())
            for panel in dashboard["panels"]:
                for target in panel.get("targets", []):
                    expression = re.sub(r"\$[a-zA-Z_]+", ".*", target["expr"])
                    queries.append({"record": "glamdb_dashboard_check_" + str(len(queries)), "expr": expression})
        dashboard_rules = temporary / "dashboard-queries.yml"
        dashboard_rules.write_text(json.dumps({"groups": [{"name": "dashboard-syntax", "rules": queries}]}))
        run([args.promtool, "check", "rules", str(dashboard_rules)])
        if args.kubeconform:
            run([args.kubeconform, "-strict", "-summary", "-kubernetes-version", "1.36.0", str(manifest)])


if __name__ == "__main__":
    main()
