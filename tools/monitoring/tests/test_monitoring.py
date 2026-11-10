import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools/monitoring" / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


exporter = module("resource_exporter")
generate = module("generate")
kubernetes = module("kubernetes")


def fixture(root, path="workload"):
    target = root / path
    target.mkdir(parents=True)
    files = {"memory.stat": "anon 1048576\nfile 2097152\nunevictable 4096\npgmajfault 8\n",
             "memory.current": "4194304\n", "memory.max": "8388608\n",
             "memory.events": "oom 0\noom_kill 0\nhigh 2\nmax 1\n",
             "cpu.stat": "usage_usec 3000000\nthrottled_usec 100000\nburst_usec 20000\nnr_periods 10\nnr_throttled 2\nnr_bursts 1\n",
             "cpu.max": "50000 100000\n", "pids.current": "7\n",
             "io.stat": "8:0 rbytes=4096 wbytes=8192 rios=2 wios=4 dbytes=0 dios=0\n"}
    for resource in ("cpu", "memory", "io"):
        files[resource + ".pressure"] = "some avg10=1.0 avg60=0 avg300=0 total=200000\nfull avg10=0 avg60=0 avg300=0 total=10000\n"
    for name, content in files.items():
        (target / name).write_text(content)
    return {"path": path, "cluster": "test", "namespace": "glamdb", "pod": "db-0", "node": "node-1", "service": "glamdb"}


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.target = fixture(self.root)

    def sample(self):
        metrics = exporter.Metrics()
        exporter.sample_target(self.root, self.target, metrics)
        return metrics.render().decode()

    def test_units_and_true_fields(self):
        text = self.sample()
        for name, value in (("memory_anon_bytes", "1048576"), ("memory_unevictable_bytes", "4096"),
                            ("memory_major_faults_total", "8"), ("cpu_usage_seconds_total", "3.0"),
                            ("cpu_burst_seconds_total", "0.02"), ("cpu_limit_cores", "0.5"), ("threads", "7")):
            line = next(line for line in text.splitlines() if line.startswith("glamdb_cgroup_" + name + "{"))
            self.assertEqual(line.rsplit(" ", 1)[1], value)
        self.assertIn('device="8:0"', text)
        self.assertIn('collector="memory"', text)
        self.assertNotIn("request_id", text)

    def test_missing_controller_is_explicit_not_zero(self):
        (self.root / "workload/memory.stat").unlink()
        text = self.sample()
        self.assertNotIn("glamdb_cgroup_memory_anon_bytes", text)
        failed = next(line for line in text.splitlines() if line.startswith("glamdb_cgroup_collector_success{") and 'collector="memory"' in line)
        self.assertTrue(failed.endswith(" 0"))

    def test_unlimited_limits_and_absent_burst_remain_absent(self):
        (self.root / "workload/cpu.max").write_text("max 100000\n")
        (self.root / "workload/memory.max").write_text("max\n")
        (self.root / "workload/cpu.stat").write_text("usage_usec 1000000\n")
        text = self.sample()
        for name in ("cpu_limit_cores", "memory_limit_bytes", "cpu_burst_seconds_total"):
            self.assertNotIn("glamdb_cgroup_" + name, text)

    def test_traversal_and_unbounded_labels_rejected(self):
        for changes in ({"path": "../escape"}, {"path": "/absolute"}, {"operation_id": "secret"}, {"pod": "x" * 254}, {"pod": "bad\tvalue"}):
            with self.assertRaises(ValueError):
                exporter.checked_target(self.root, {**self.target, **changes})
        (self.root / "escape").symlink_to(self.root.parent)
        with self.assertRaises(ValueError):
            exporter.checked_target(self.root, {**self.target, "path": "escape/other"})

    def test_sampling_has_bounded_series_and_no_labels_from_files(self):
        text = self.sample()
        samples = [line for line in text.splitlines() if line and not line.startswith("#")]
        self.assertLess(len(samples), 50)
        self.assertEqual(len(samples), len(set(line.rsplit(" ", 1)[0] for line in samples)))

    def test_kubernetes_matches_parent_not_containers(self):
        uid = "12345678-1234-1234-1234-123456789abc"
        parent = "kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod" + uid.replace("-", "_") + ".slice"
        fixture(self.root, parent)
        (self.root / parent / "cri-container.scope").mkdir()
        token = self.root / "token"
        token.write_text("private-token")
        response = io.BytesIO(json.dumps({"items": [{"metadata": {"uid": uid, "name": "db-0"}, "status": {"phase": "Running"}}]}).encode())
        with patch.object(exporter, "urlopen", return_value=response) as opened, patch.object(exporter.ssl, "create_default_context"):
            targets, unmatched = exporter.discover_kubernetes(self.root, api="https://cluster", ca="ca", token=token,
                namespace="glamdb", node="node-1", selector="app=x", cluster="test", service="glamdb", max_targets=100)
        self.assertEqual(unmatched, 0)
        self.assertEqual([target["path"] for target in targets], [parent])
        self.assertIn("fieldSelector=spec.nodeName%3Dnode-1", opened.call_args.args[0].full_url)


class DeploymentTests(unittest.TestCase):
    def test_native_loki_inventory_is_optional_and_uses_the_alert_job(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools/monitoring/local_config.py"),
                                 "--loki", "127.0.0.1:3100"], capture_output=True, text=True, check=True)
        config = json.loads(result.stdout)
        self.assertEqual(len(config["scrape_configs"]), 1)
        job = config["scrape_configs"][0]
        self.assertEqual(job["job_name"], "loki")
        self.assertEqual(job["static_configs"][0]["targets"], ["127.0.0.1:3100"])
        self.assertEqual(job["static_configs"][0]["labels"]["cluster"], "local")

    def test_actual_caddy_route_label_contract(self):
        captured = (ROOT / "tools/monitoring/tests/fixtures/caddy-2.11.9.prom").read_text()
        samples = [line for line in captured.splitlines() if line.startswith("caddy_http_request_duration_seconds_count{")]
        self.assertTrue(samples)
        for sample in samples:
            self.assertIn('handler="subroute"', sample)
            self.assertIn('server="workbench"', sample)
        self.assertIn('method="OTHER"', captured)
        for rule in generate.RULES:
            if "caddy_http_request_duration_seconds" in rule["expr"]:
                self.assertIn('handler="subroute"', rule["expr"])
                self.assertIn('server="workbench"', rule["expr"])
                self.assertNotIn('handler="reverse_proxy"', rule["expr"])

    def test_all_images_are_digest_pinned(self):
        lock = json.loads((ROOT / "deploy/monitoring/versions.lock.json").read_text())
        for entry in lock["images"].values():
            self.assertRegex(entry["digest"], r"^sha256:[a-f0-9]{64}$")
            self.assertTrue(entry["image"].endswith("@" + entry["digest"]))

    def test_generated_dashboards_and_rule_references(self):
        records = {rule["record"] for rule in generate.RULES}
        import re
        for path, data in generate.artifacts().items():
            self.assertEqual(json.loads(path.read_text()), data)
            if "panels" not in data:
                continue
            self.assertEqual(len(data["panels"]), len({panel["id"] for panel in data["panels"]}))
            for panel in data["panels"]:
                for target in panel.get("targets", []):
                    for record in re.findall(r"glamdb:[a-zA-Z0-9_:]+", target["expr"]):
                        self.assertIn(record, records)
                    self.assertNotIn("vector(0)", target["expr"])
                    self.assertNotIn("or 0", target["expr"])

    def test_kubernetes_credentials_privileges_and_sources(self):
        document = kubernetes.render(with_cadvisor=True)
        items = document["items"]
        configmaps = {item["metadata"]["name"]: item["data"] for item in items if item["kind"] == "ConfigMap"}
        for kind in ("DaemonSet", "Deployment"):
            for item in (item for item in items if item["kind"] == kind):
                for container in item["spec"]["template"]["spec"]["containers"]:
                    self.assertIn("@sha256:", container["image"])
                    self.assertIn("resources", container)
                    if container["name"] != "cadvisor-detail":
                        self.assertFalse(container["securityContext"].get("privileged", False))
        self.assertFalse(any(item["kind"] == "Secret" for item in items))
        self.assertIn("resource_exporter.py", configmaps["resource-exporter"])
        config = json.loads(configmaps["prometheus-config"]["prometheus.yml"])
        jobs = {job["job_name"] for job in config["scrape_configs"]}
        self.assertTrue({"cadvisor", "kubelet-volumes", "node", "glamdb-resources", "cadvisor-detail"}.issubset(jobs))
        self.assertNotIn("insecure_skip_verify", json.dumps(config))
        self.assertNotIn("gateway-metrics", [item["metadata"]["name"] for item in items if item["kind"] == "NetworkPolicy"])


if __name__ == "__main__":
    unittest.main()
