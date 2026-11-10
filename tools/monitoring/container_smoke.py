#!/usr/bin/env python3
"""Real, disposable Linux cgroup -> Prometheus -> Grafana provisioning check.

Uses only named task containers and an isolated network. Does not modify a VM,
an existing stack, a database store, or any preexisting volume.
"""

import argparse
import base64
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "deploy/monitoring"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection", required=True, help="Explicit dedicated Podman connection")
    parser.add_argument("--machine", required=True, help="Explicit dedicated VM for cgroup PID lookup")
    parser.add_argument("--vm-root", default="/workspace", help="This checkout's existing share path in that VM")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/monitoring/container-smoke.json")
    args = parser.parse_args()
    engine = ["podman", "--connection", args.connection]
    images = json.loads((BASE / "versions.lock.json").read_text())["images"]
    suffix = secrets.token_hex(4)
    network = "glamdb-monitoring-test-" + suffix
    names = []

    def command(*parts, capture=True):
        result = subprocess.run([*engine, *parts], capture_output=capture, text=True)
        if result.returncode:
            if capture:
                print(result.stderr, flush=True)
            raise RuntimeError("container command failed: " + parts[0])
        return result.stdout.strip() if capture else ""

    def vm_path(path):
        return args.vm_root + "/" + str(Path(path).relative_to(ROOT))

    def start(role, image, extra, command_args=()):
        name = "glamdb-monitoring-test-" + role + "-" + suffix
        names.append(name)
        command("run", "--detach", "--name", name, "--network", network, "--memory", "512m", "--cpus", "0.5",
                *extra, images[image]["image"], *command_args)
        return name

    def port(name, internal):
        data = json.loads(command("inspect", name))[0]
        return int(data["NetworkSettings"]["Ports"][str(internal) + "/tcp"][0]["HostPort"])

    def get(url, *, auth=None):
        request = Request(url)
        if auth:
            request.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def until(fn, timeout=60):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                result = fn()
                if result:
                    return result
            except Exception as error:
                last = str(error)
            time.sleep(0.5)
        raise RuntimeError("condition timed out: " + str(last))

    evidence = {"fixture": "disposable Python allocator, not native database health", "images": {key: item["image"] for key, item in images.items()}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="container-smoke-", dir=args.output.parent)
    work = Path(temporary.name)
    try:
        command("network", "create", network)
        target = start("workload", "python", [], ["python3", "-c", "import time,threading; data=bytearray(64*1024*1024); [threading.Thread(target=lambda:time.sleep(180),daemon=True).start() for _ in range(3)]; time.sleep(180)"])
        pid = json.loads(command("inspect", target))[0]["State"]["Pid"]
        cgroup = subprocess.run(["podman", "machine", "ssh", args.machine, "cat", f"/proc/{pid}/cgroup"], check=True, capture_output=True, text=True).stdout
        relative = next(line.split("::", 1)[1].lstrip("/") for line in cgroup.splitlines() if line.startswith("0::"))
        (work / "targets.json").write_text(json.dumps([{"path": relative, "cluster": "test", "namespace": "monitoring-fixture", "pod": "allocator-0", "node": "task-vm", "service": "fixture"}]))
        resource = start("resources", "python", ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--volume", vm_path(ROOT / "tools/monitoring/resource_exporter.py") + ":/app/exporter.py:ro",
            "--volume", vm_path(work / "targets.json") + ":/app/targets.json:ro", "--volume", "/sys/fs/cgroup:/host-cgroup:ro"],
            ["python3", "/app/exporter.py", "--listen", "0.0.0.0:9409", "--cgroup-root", "/host-cgroup", "--targets", "/app/targets.json", "--interval", "1"])
        config = {"global": {"scrape_interval": "1s", "evaluation_interval": "1s"},
                  "rule_files": ["/etc/monitoring/rules.yml"],
                  "scrape_configs": [{"job_name": "glamdb-resources", "static_configs": [{"targets": [resource + ":9409"]}]}]}
        (work / "prometheus.yml").write_text(json.dumps(config))
        prom = start("prometheus", "prometheus", ["--publish", "127.0.0.1::9090", "--read-only", "--cap-drop", "ALL",
            "--tmpfs", "/prometheus:rw,size=256m,mode=1777", "--volume", vm_path(work / "prometheus.yml") + ":/etc/monitoring/prometheus.yml:ro",
            "--volume", vm_path(BASE / "prometheus/rules/recording.yml") + ":/etc/monitoring/rules.yml:ro"],
            ["--config.file=/etc/monitoring/prometheus.yml", "--storage.tsdb.path=/prometheus"])
        prom_url = "http://127.0.0.1:" + str(port(prom, 9090))

        def query(expression):
            response = get(prom_url + "/api/v1/query?" + urlencode({"query": expression}))
            if response["status"] != "success":
                raise RuntimeError(str(response))
            return response["data"]["result"]

        values = until(lambda: query('glamdb:pod_memory_anon_bytes{pod="allocator-0"}'))
        anon = float(values[0]["value"][1])
        if anon < 64 * 1024 * 1024:
            raise RuntimeError("actual allocated anonymous memory not observed")
        threads = float(until(lambda: query('glamdb:pod_threads{pod="allocator-0"}'))[0]["value"][1])
        if threads < 4:
            raise RuntimeError("actual threads not observed")
        evidence.update({"anonymous_bytes": anon, "kernel_tasks": threads, "cgroup_version": 2,
                         "collector_failures": query("glamdb_cgroup_collector_success == 0"),
                         "series": len(get(prom_url + "/api/v1/series?" + urlencode({"match[]": '{__name__=~"glamdb.*"}'}))["data"])})
        if evidence["collector_failures"]:
            raise RuntimeError("a real cgroup controller failed")
        provisioning = work / "provisioning"
        (provisioning / "datasources").mkdir(parents=True)
        (provisioning / "dashboards").mkdir()
        (provisioning / "datasources/default.yml").write_text(json.dumps({"apiVersion": 1, "datasources": [{"name": "Prometheus", "uid": "prometheus", "type": "prometheus", "access": "proxy", "url": "http://" + prom + ":9090", "isDefault": True}]}))
        (provisioning / "dashboards/default.yml").write_text((BASE / "grafana/provisioning/dashboards/default.yml").read_text())
        password = secrets.token_urlsafe(24)
        grafana = start("grafana", "grafana", ["--publish", "127.0.0.1::3000", "--read-only", "--cap-drop", "ALL",
            "--tmpfs", "/var/lib/grafana:rw,size=128m,mode=1777", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
            "--env", "GF_SECURITY_ADMIN_PASSWORD=" + password, "--env", "GF_ANALYTICS_REPORTING_ENABLED=false",
            "--env", "GF_ANALYTICS_CHECK_FOR_UPDATES=false", "--env", "GF_AUTH_ANONYMOUS_ENABLED=false",
            "--volume", vm_path(provisioning) + ":/etc/grafana/provisioning:ro",
            "--volume", vm_path(BASE / "grafana/dashboards") + ":/var/lib/grafana/dashboards:ro"])
        grafana_url = "http://127.0.0.1:" + str(port(grafana, 3000))
        auth = "admin:" + password
        until(lambda: get(grafana_url + "/api/health")["database"] == "ok")
        provisioned = until(lambda: get(grafana_url + "/api/search?type=dash-db", auth=auth))
        uids = {item["uid"] for item in provisioned}
        if uids != {"glamdb-pods", "glamdb-nodes", "glamdb-service"}:
            raise RuntimeError("provisioned dashboards mismatch: " + str(uids))
        dashboard = get(grafana_url + "/api/dashboards/uid/glamdb-pods", auth=auth)["dashboard"]
        if len(dashboard["panels"]) < 25:
            raise RuntimeError("pod panels were lost during provisioning")
        proxied = get(grafana_url + "/api/datasources/proxy/uid/prometheus/api/v1/query?" + urlencode({"query": 'glamdb:pod_memory_anon_bytes{pod="allocator-0"}'}), auth=auth)
        if not proxied["data"]["result"]:
            raise RuntimeError("Grafana datasource cannot query actual metrics")
        evidence.update({"grafana_health": "ok", "provisioned_dashboard_uids": sorted(uids), "pod_panels": len(dashboard["panels"]), "grafana_datasource_query": "pass"})
        command("stop", "--time", "5", target)
        until(lambda: not query('glamdb:pod_memory_anon_bytes{pod="allocator-0"}'), timeout=30)
        if not query('up{job="glamdb-resources"} == 1'):
            raise RuntimeError("exporter should remain reachable when workload disappears")
        evidence["workload_stopped_resource_absence"] = "pass"
        evidence["result"] = "pass"
        args.output.write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence, indent=2))
    finally:
        for name in reversed(names):
            subprocess.run([*engine, "rm", "--force", name], capture_output=True)
        subprocess.run([*engine, "network", "rm", network], capture_output=True)
        temporary.cleanup()


if __name__ == "__main__":
    main()
