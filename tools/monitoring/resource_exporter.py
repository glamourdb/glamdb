#!/usr/bin/env python3
"""Bounded, read-only cgroup v2 sampling. No application health is inferred."""

import argparse
import json
import os
from pathlib import Path
import re
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LABELS = ("cluster", "namespace", "pod", "node", "service")
MEMORY = {
    "anon": ("memory_anon_bytes", "gauge", "Anonymous memory charged to this cgroup."),
    "file": ("memory_file_bytes", "gauge", "File cache including tmpfs and shared memory."),
    "unevictable": ("memory_unevictable_bytes", "gauge", "Memory on unevictable lists."),
    "pgmajfault": ("memory_major_faults_total", "counter", "Major page faults; not necessarily disk reads."),
}
CPU = {
    "usage_usec": ("cpu_usage_seconds_total", "Cumulative cgroup CPU usage."),
    "throttled_usec": ("cpu_throttled_seconds_total", "Cumulative CFS throttle time; may exceed wall time."),
    "burst_usec": ("cpu_burst_seconds_total", "Cumulative CFS bandwidth burst time, when supported."),
}
IO = {"rbytes": "read_bytes_total", "wbytes": "write_bytes_total",
      "rios": "read_operations_total", "wios": "write_operations_total"}


def key_values(path):
    values = {}
    for line in path.read_text().splitlines():
        key, value = line.split()
        values[key] = int(value)
    return values


def labels_text(labels):
    return "{" + ",".join(key + "=" + json.dumps(str(value), ensure_ascii=False)
                           for key, value in sorted(labels.items())) + "}"


class Metrics:
    def __init__(self):
        self.lines = []
        self.declared = set()

    def add(self, name, value, labels, help_text, kind="gauge"):
        if name not in self.declared:
            self.lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"])
            self.declared.add(name)
        self.lines.append(f"{name}{labels_text(labels)} {value}")

    def render(self):
        return ("\n".join(self.lines) + "\n").encode()


def checked_target(root, item):
    """Resolve under an explicit root; reject traversal, symlink escapes and excess labels."""
    if set(item) - {"path", *LABELS}:
        raise ValueError("target contains unknown fields")
    relative = Path(item["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("target path must be relative to cgroup root")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("target escapes cgroup root")
    labels = {key: str(item[key]) for key in LABELS}
    if any(not value or len(value) > 253 or any(ord(character) < 32 or ord(character) == 127 for character in value)
           for value in labels.values()):
        raise ValueError("target labels must be printable, nonempty and at most 253 characters")
    return path, labels


def sample_target(root, target, metrics):
    path, labels = checked_target(root, target)
    # A failed controller emits its own 0, with no fabricated resource values.
    controllers = {
        "memory": lambda: sample_memory(path, labels, metrics),
        "cpu": lambda: sample_cpu(path, labels, metrics),
        "pids": lambda: metrics.add("glamdb_cgroup_threads", int((path / "pids.current").read_text()),
                                    labels, "Kernel tasks in this cgroup subtree (includes threads)."),
        "io": lambda: sample_io(path, labels, metrics),
        "pressure": lambda: sample_pressure(path, labels, metrics),
    }
    for controller, sample in controllers.items():
        try:
            sample()
            success = 1
        except (OSError, ValueError):
            success = 0
        metrics.add("glamdb_cgroup_collector_success", success, {**labels, "collector": controller},
                    "Whether this controller could be sampled; missing values remain absent.")


def sample_memory(path, labels, metrics):
    values = key_values(path / "memory.stat")
    current = int((path / "memory.current").read_text())
    limit = (path / "memory.max").read_text().strip()
    metrics.add("glamdb_cgroup_memory_usage_bytes", current, labels, "Total charged memory in the cgroup subtree.")
    for key, (suffix, kind, help_text) in MEMORY.items():
        if key in values:
            metrics.add("glamdb_cgroup_" + suffix, values[key], labels, help_text, kind)
    if limit != "max":
        metrics.add("glamdb_cgroup_memory_limit_bytes", int(limit), labels, "Local hard memory limit; ancestors can be lower.")
    try:
        events = key_values(path / "memory.events")
        for event in ("oom", "oom_kill", "high", "max"):
            if event in events:
                metrics.add("glamdb_cgroup_memory_events_total", events[event], {**labels, "event": event},
                            "Hierarchical memory.events counter.", "counter")
    except FileNotFoundError:
        pass


def sample_cpu(path, labels, metrics):
    values = key_values(path / "cpu.stat")
    for key, (suffix, help_text) in CPU.items():
        if key in values:
            metrics.add("glamdb_cgroup_" + suffix, values[key] / 1_000_000, labels, help_text, "counter")
    for key, suffix in (("nr_periods", "periods_total"), ("nr_throttled", "throttled_periods_total"),
                        ("nr_bursts", "bursts_total")):
        if key in values:
            metrics.add("glamdb_cgroup_cpu_" + suffix, values[key], labels, "CFS bandwidth event counter.", "counter")
    quota, period = (path / "cpu.max").read_text().split()
    if quota != "max":
        metrics.add("glamdb_cgroup_cpu_limit_cores", int(quota) / int(period), labels,
                    "Local quota divided by period; not CPU request or effective ancestor limit.")


def sample_io(path, labels, metrics):
    # io.stat has per-device counters, not per-direction wall-clock utilization.
    lines = (path / "io.stat").read_text().splitlines()
    if len(lines) > 256:
        raise ValueError("per-target I/O device limit exceeded")
    for line in lines:
        device, *fields = line.split()
        if not re.fullmatch(r"\d+:\d+", device):
            raise ValueError("invalid io.stat device")
        values = dict(field.split("=", 1) for field in fields)
        for key, suffix in IO.items():
            if key in values:
                metrics.add("glamdb_cgroup_io_" + suffix, int(values[key]), {**labels, "device": device},
                            "Cgroup block I/O counter; buffered writeback attribution depends on filesystem.", "counter")


def sample_pressure(path, labels, metrics):
    for resource in ("cpu", "memory", "io"):
        for line in (path / (resource + ".pressure")).read_text().splitlines():
            scope, *fields = line.split()
            if scope not in ("some", "full"):
                continue
            total = int(dict(field.split("=", 1) for field in fields)["total"])
            metrics.add("glamdb_cgroup_pressure_stall_seconds_total", total / 1_000_000,
                        {**labels, "resource": resource, "scope": scope},
                        "PSI wall time; some=at least one task stalled, full=all non-idle tasks stalled.", "counter")


def discover_kubernetes(root, *, api, ca, token, namespace, node, selector, cluster, service, max_targets):
    query = urlencode({"fieldSelector": "spec.nodeName=" + node, "labelSelector": selector,
                       "limit": max_targets + 1})
    request = Request(api + "/api/v1/namespaces/" + namespace + "/pods?" + query,
                      headers={"Authorization": "Bearer " + token.read_text().strip()})
    with urlopen(request, context=ssl.create_default_context(cafile=ca), timeout=10) as response:
        document = json.load(response)
    if document.get("metadata", {}).get("continue") or len(document["items"]) > max_targets:
        raise ValueError("pod target limit exceeded; narrow the namespace/label selector")
    pods = {}
    for pod in document["items"]:
        if pod["status"].get("phase") not in ("Succeeded", "Failed"):
            uid = pod["metadata"]["uid"]
            pods[uid.replace("-", "_")] = pod["metadata"]["name"]
    # Match pod parent cgroups once. Never combine those parents with child containers.
    found = []
    for parent, directories, _files in os.walk(root):
        for name in list(directories):
            match = re.search(r"(?:^|-)pod([0-9a-f_-]{36})(?:\.slice)?$", name)
            if match and match[1].replace("-", "_") in pods:
                uid = match[1].replace("-", "_")
                found.append({"path": str((Path(parent) / name).relative_to(root)), "cluster": cluster,
                              "namespace": namespace, "pod": pods.pop(uid), "node": node, "service": service})
                directories.remove(name)
        if len(Path(parent).relative_to(root).parts) >= 5:
            directories.clear()
    # Pod creation/deletion races may legitimately leave a pod without a cgroup.
    # Emit an inventory gauge, rather than claiming every pod was sampled.
    return found, len(pods)


class Sampler:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.payload = b""
        self.sampled_at = 0.0
        self.targets = []
        self.discovery_at = 0.0
        self.discovery_ok = False
        self.unmatched = 0

    def collect(self):
        args = self.args
        start = time.monotonic()
        metrics = Metrics()
        try:
            if not args.kubernetes:
                targets = json.loads(args.targets.read_text())
                if not isinstance(targets, list) or len(targets) > args.max_targets:
                    raise ValueError("invalid target list or target limit exceeded")
                self.targets = targets
                self.discovery_ok = True
            elif start - self.discovery_at >= args.discovery_interval or not self.discovery_ok:
                self.targets, self.unmatched = discover_kubernetes(
                    args.cgroup_root, api=args.api, ca=args.ca, token=args.token, namespace=args.namespace,
                    node=args.node, selector=args.selector, cluster=args.cluster, service=args.service,
                    max_targets=args.max_targets)
                self.discovery_at = start
                self.discovery_ok = True
            seen = set()
            paths = set()
            for target in self.targets:
                path, labels = checked_target(args.cgroup_root, target)
                identity = tuple(labels.items())
                if identity in seen:
                    raise ValueError("duplicate target identity")
                if path in paths:
                    raise ValueError("duplicate target cgroup")
                seen.add(identity)
                paths.add(path)
            # Reject overlapping targets, avoiding parent plus child double counting.
            if any(parent in paths for path in paths for parent in path.parents):
                raise ValueError("overlapping target cgroups")
            for target in self.targets:
                sample_target(args.cgroup_root, target, metrics)
        except (OSError, ValueError, KeyError) as error:
            # Never publish a partly valid inventory when discovery/configuration fails.
            self.discovery_ok = False
            self.targets = []
            metrics = Metrics()
            print(f"resource discovery failed: {type(error).__name__}", flush=True)
        metrics.add("glamdb_resource_discovery_success", int(self.discovery_ok), {}, "Whether configured target discovery succeeded.")
        metrics.add("glamdb_resource_targets", len(self.targets), {}, "Configured/discovered cgroup targets, not live DB instances.")
        metrics.add("glamdb_resource_unmatched_pods", self.unmatched, {}, "Discovered Kubernetes pods without a matching pod cgroup.")
        metrics.add("glamdb_resource_sample_duration_seconds", time.monotonic() - start, {}, "Duration of the most recent resource sample.")
        metrics.add("glamdb_resource_sample_timestamp_seconds", time.time(), {}, "Unix time of the most recent resource sample.")
        with self.lock:
            self.payload = metrics.render()
            self.sampled_at = time.monotonic()

    def run(self):
        while True:
            self.collect()
            time.sleep(self.args.interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:9409")
    parser.add_argument("--cgroup-root", type=Path, default=Path("/sys/fs/cgroup"))
    parser.add_argument("--targets", type=Path, default=Path("deploy/monitoring/resource-targets.json"))
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--max-targets", type=int, default=100)
    parser.add_argument("--kubernetes", action="store_true")
    parser.add_argument("--api", default="https://kubernetes.default.svc")
    parser.add_argument("--ca", default="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    parser.add_argument("--token", type=Path, default=Path("/var/run/secrets/kubernetes.io/serviceaccount/token"))
    parser.add_argument("--namespace", default="glamdb")
    parser.add_argument("--node", default=os.environ.get("NODE_NAME", ""))
    parser.add_argument("--cluster", default="local")
    parser.add_argument("--service", default="glamdb")
    parser.add_argument("--selector", default="app.kubernetes.io/part-of=glamdb")
    parser.add_argument("--discovery-interval", type=float, default=60)
    args = parser.parse_args()
    if args.interval < 1 or args.discovery_interval < 15 or not 1 <= args.max_targets <= 1000:
        parser.error("interval >=1s, discovery interval >=15s and max targets 1..1000 required")
    if args.kubernetes and not args.node:
        parser.error("--node or NODE_NAME required for Kubernetes discovery")
    sampler = Sampler(args)
    sampler.collect()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def do_GET(self):
            if self.path not in ("/metrics", "/-/ready"):
                self.send_error(404)
                return
            with sampler.lock:
                payload = sampler.payload
                fresh = time.monotonic() - sampler.sampled_at < args.interval * 3
            self.send_response(200 if fresh else 503)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            pass

    host, port = args.listen.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), Handler)
    threading.Thread(target=sampler.run, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
