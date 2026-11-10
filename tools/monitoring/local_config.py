#!/usr/bin/env python3
"""Render a native, loopback Prometheus config from explicitly enabled targets."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default="local")
    parser.add_argument("--service", default="glamdb")
    for name in ("gateway", "core", "node", "cadvisor", "resources", "alloy", "loki"):
        parser.add_argument("--" + name, action="append", default=[], help="Explicit host:port; repeat for multiple targets")
    parser.add_argument("--alertmanager", help="Optional actual Alertmanager host:port")
    args = parser.parse_args()
    jobs = []
    for name in ("gateway", "core", "node", "cadvisor", "resources", "alloy", "loki"):
        targets = getattr(args, name)
        if targets:
            job = "glamdb-" + name if name in ("gateway", "core", "resources") else name
            jobs.append({"job_name": job, "sample_limit": 100000 if name == "cadvisor" else 50000,
                         "label_limit": 30, "static_configs": [{"targets": targets, "labels": {"cluster": args.cluster, "service": args.service}}]})
    config = {"global": {"scrape_interval": "15s", "scrape_timeout": "10s", "evaluation_interval": "30s"},
              "rule_files": [str(ROOT / "deploy/monitoring/prometheus/rules/recording.yml")], "scrape_configs": jobs}
    if args.alertmanager:
        config["alerting"] = {"alertmanagers": [{"static_configs": [{"targets": [args.alertmanager]}]}]}
    print(json.dumps(config, indent=2))


if __name__ == "__main__":
    main()
