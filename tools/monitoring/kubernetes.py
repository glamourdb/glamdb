#!/usr/bin/env python3
"""Render a standalone metrics stack; kubectl apply remains an operator action."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "deploy/monitoring"


def render(namespace="glamdb-monitoring", workload_namespace="glamdb", cluster="local", with_cadvisor=False):
    images = json.loads((BASE / "versions.lock.json").read_text())["images"]
    items = []

    def add(kind, name, body, api="v1", ns=namespace):
        metadata = {"name": name, "labels": {"app.kubernetes.io/part-of": "glamdb-monitoring"}}
        if ns:
            metadata["namespace"] = ns
        item = {"apiVersion": api, "kind": kind, "metadata": metadata, **body}
        items.append(item)
        return item

    add("Namespace", namespace, {}, ns=None)
    for name in ("prometheus", "resources", "kube-state-metrics"):
        add("ServiceAccount", name, {})
    add("ClusterRole", namespace + "-prometheus", {"rules": [
        {"apiGroups": [""], "resources": ["nodes", "nodes/proxy", "pods", "services", "endpoints"], "verbs": ["get", "list", "watch"]},
        {"nonResourceURLs": ["/metrics"], "verbs": ["get"]},
    ]}, "rbac.authorization.k8s.io/v1", ns=None)
    add("ClusterRoleBinding", namespace + "-prometheus", {
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": namespace + "-prometheus"},
        "subjects": [{"kind": "ServiceAccount", "name": "prometheus", "namespace": namespace}]},
        "rbac.authorization.k8s.io/v1", ns=None)
    add("Role", "glamdb-resource-reader", {"rules": [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]}]},
        "rbac.authorization.k8s.io/v1", ns=workload_namespace)
    add("RoleBinding", "glamdb-resource-reader", {
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "glamdb-resource-reader"},
        "subjects": [{"kind": "ServiceAccount", "name": "resources", "namespace": namespace}]},
        "rbac.authorization.k8s.io/v1", ns=workload_namespace)
    add("Role", "glamdb-state-reader", {"rules": [
        {"apiGroups": [""], "resources": ["pods", "persistentvolumeclaims"], "verbs": ["list", "watch"]}]},
        "rbac.authorization.k8s.io/v1", ns=workload_namespace)
    add("RoleBinding", "glamdb-state-reader", {
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "glamdb-state-reader"},
        "subjects": [{"kind": "ServiceAccount", "name": "kube-state-metrics", "namespace": namespace}]},
        "rbac.authorization.k8s.io/v1", ns=workload_namespace)
    add("ConfigMap", "resource-exporter", {"data": {"resource_exporter.py": (ROOT / "tools/monitoring/resource_exporter.py").read_text()}})
    add("ConfigMap", "recording-rules", {"data": {"recording.yml": (BASE / "prometheus/rules/recording.yml").read_text()}})
    add("ConfigMap", "grafana-provisioning", {"data": {
        "datasources.yml": (BASE / "grafana/provisioning/datasources/metrics.yml").read_text(),
        "dashboards.yml": (BASE / "grafana/provisioning/dashboards/default.yml").read_text()}})
    add("ConfigMap", "grafana-dashboards", {"data": {path.name: path.read_text() for path in sorted((BASE / "grafana/dashboards").glob("*.json"))}})

    def pod_job(name, port, *, sd_name=None):
        return {"job_name": name, "sample_limit": 100000, "label_limit": 30,
                "kubernetes_sd_configs": [{"role": "pod", "namespaces": {"names": [namespace]}}],
                "relabel_configs": [
                    {"source_labels": ["__meta_kubernetes_pod_label_app_kubernetes_io_name"], "regex": sd_name or name, "action": "keep"},
                    {"source_labels": ["__meta_kubernetes_pod_container_port_number"], "regex": str(port), "action": "keep"},
                    {"source_labels": ["__meta_kubernetes_pod_node_name"], "target_label": "node"},
                    {"target_label": "cluster", "replacement": cluster},
                    {"target_label": "service", "replacement": "glamdb"},
                ]}

    credentials = "/var/run/secrets/kubernetes.io/serviceaccount"
    kubelet_relabel = [{"source_labels": ["__meta_kubernetes_node_name"], "target_label": "node"},
                       {"target_label": "cluster", "replacement": cluster},
                       {"target_label": "__address__", "replacement": "kubernetes.default.svc:443"},
                       {"source_labels": ["__meta_kubernetes_node_name"], "regex": "(.+)", "target_label": "__metrics_path__", "replacement": "/api/v1/nodes/$1/proxy/metrics/cadvisor"}]
    kubelet_job = {"job_name": "cadvisor", "scheme": "https", "sample_limit": 100000, "label_limit": 30,
                   "authorization": {"credentials_file": credentials + "/token"},
                   "tls_config": {"ca_file": credentials + "/ca.crt"},
                   "kubernetes_sd_configs": [{"role": "node"}], "relabel_configs": kubelet_relabel}
    volume_job = json.loads(json.dumps(kubelet_job))
    volume_job["job_name"] = "kubelet-volumes"
    volume_job["relabel_configs"][-1]["replacement"] = "/api/v1/nodes/$1/proxy/metrics"
    volume_job["metric_relabel_configs"] = [{"source_labels": ["__name__"], "regex": "kubelet_volume_stats_.*", "action": "keep"}]
    gateway_job = {"job_name": "glamdb-gateway", "sample_limit": 20000, "label_limit": 20,
                   "kubernetes_sd_configs": [{"role": "pod", "namespaces": {"names": [workload_namespace]}}],
                   "relabel_configs": [
                       {"source_labels": ["__meta_kubernetes_pod_label_app_kubernetes_io_name"], "regex": "glamdb-gateway", "action": "keep"},
                       {"source_labels": ["__meta_kubernetes_pod_container_port_number"], "regex": "9464", "action": "keep"},
                       {"target_label": "cluster", "replacement": cluster}, {"target_label": "service", "replacement": "glamdb"},
                       {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "namespace"},
                       {"source_labels": ["__meta_kubernetes_pod_name"], "target_label": "pod"}]}
    jobs = [kubelet_job, volume_job, gateway_job,
            pod_job("node", 9100, sd_name="node-exporter"), pod_job("glamdb-resources", 9409, sd_name="resources"),
            {"job_name": "kube-state-metrics", "static_configs": [{"targets": ["kube-state-metrics:8080"], "labels": {"cluster": cluster}}]},
            {"job_name": "prometheus", "static_configs": [{"targets": ["localhost:9090"], "labels": {"cluster": cluster, "service": "monitoring"}}]}]
    if with_cadvisor:
        detail = pod_job("cadvisor-detail", 8080)
        detail["metric_relabel_configs"] = [
            {"source_labels": ["container_label_io_kubernetes_pod_namespace"], "regex": "(.+)", "target_label": "namespace"},
            {"source_labels": ["container_label_io_kubernetes_pod_name"], "regex": "(.+)", "target_label": "pod"},
            {"source_labels": ["container_label_io_kubernetes_container_name"], "regex": "(.+)", "target_label": "container"},
            {"regex": "container_label_.*|image", "action": "labeldrop"}]
        jobs.append(detail)
    prometheus = {"global": {"scrape_interval": "15s", "scrape_timeout": "10s", "evaluation_interval": "30s", "external_labels": {"cluster": cluster}},
                  "rule_files": ["/etc/prometheus/rules/*.yml"], "scrape_configs": jobs}
    add("ConfigMap", "prometheus-config", {"data": {"prometheus.yml": json.dumps(prometheus, indent=2)}})

    def pvc(name, size):
        add("PersistentVolumeClaim", name, {"spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}}})

    pvc("prometheus-data", "10Gi")
    pvc("grafana-data", "2Gi")

    def config_volume(name, config):
        return {"name": name, "configMap": {"name": config}}

    def mount(name, path, subpath=None):
        result = {"name": name, "mountPath": path, "readOnly": True}
        if subpath:
            result["subPath"] = subpath
        return result

    def container(name, image, args, port, *, cpu="100m", memory="128Mi", extra=None):
        return {"name": name, "image": images[image]["image"], "args": args, "ports": [{"name": "http", "containerPort": port}],
                "resources": {"requests": {"cpu": cpu, "memory": memory}, "limits": {"memory": "1Gi" if name == "prometheus" else "512Mi"}},
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                "readinessProbe": {"httpGet": {"path": "/metrics", "port": "http"}, "periodSeconds": 15}, **(extra or {})}

    def workload(name, containers, volumes=(), *, daemon=False, service_account=None, extra_spec=None):
        spec = {"containers": containers, "volumes": list(volumes), "nodeSelector": {"kubernetes.io/os": "linux"},
                "automountServiceAccountToken": bool(service_account), **(extra_spec or {})}
        if service_account:
            spec["serviceAccountName"] = service_account
        labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": "glamdb-monitoring"}
        body = {"selector": {"matchLabels": {"app.kubernetes.io/name": name}}, "template": {"metadata": {"labels": labels}, "spec": spec}}
        if not daemon:
            body.update({"replicas": 1, "strategy": {"type": "Recreate"}})
        add("DaemonSet" if daemon else "Deployment", name, {"spec": body}, "apps/v1")

    prom = container("prometheus", "prometheus", ["--config.file=/etc/prometheus/prometheus.yml", "--storage.tsdb.path=/prometheus", "--storage.tsdb.retention.time=7d", "--storage.tsdb.retention.size=5GB"], 9090, memory="512Mi")
    prom["volumeMounts"] = [mount("config", "/etc/prometheus/prometheus.yml", "prometheus.yml"), mount("rules", "/etc/prometheus/rules"), {"name": "data", "mountPath": "/prometheus"}]
    prom["readinessProbe"]["httpGet"]["path"] = "/-/ready"
    workload("prometheus", [prom], [config_volume("config", "prometheus-config"), config_volume("rules", "recording-rules"), {"name": "data", "persistentVolumeClaim": {"claimName": "prometheus-data"}}], service_account="prometheus", extra_spec={"securityContext": {"runAsUser": 65534, "runAsGroup": 65534, "fsGroup": 65534, "runAsNonRoot": True}})
    grafana = container("grafana", "grafana", [], 3000, memory="256Mi", extra={"env": [
        {"name": "GF_SECURITY_ADMIN_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "grafana-admin", "key": "password"}}},
        {"name": "GF_USERS_ALLOW_SIGN_UP", "value": "false"}, {"name": "GF_AUTH_ANONYMOUS_ENABLED", "value": "false"},
        {"name": "GF_ANALYTICS_REPORTING_ENABLED", "value": "false"}, {"name": "GF_ANALYTICS_CHECK_FOR_UPDATES", "value": "false"}]})
    grafana["readinessProbe"]["httpGet"]["path"] = "/api/health"
    grafana["volumeMounts"] = [mount("provisioning", "/etc/grafana/provisioning/datasources/metrics.yml", "datasources.yml"), mount("provisioning", "/etc/grafana/provisioning/dashboards/default.yml", "dashboards.yml"), mount("dashboards", "/var/lib/grafana/dashboards"), {"name": "data", "mountPath": "/var/lib/grafana"}, {"name": "tmp", "mountPath": "/tmp"}]
    workload("grafana", [grafana], [config_volume("provisioning", "grafana-provisioning"), config_volume("dashboards", "grafana-dashboards"), {"name": "data", "persistentVolumeClaim": {"claimName": "grafana-data"}}, {"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}}], extra_spec={"securityContext": {"runAsUser": 472, "runAsGroup": 472, "fsGroup": 472, "runAsNonRoot": True}})
    state = container("kube-state-metrics", "kube-state-metrics", ["--resources=pods,persistentvolumeclaims", "--namespaces=" + workload_namespace], 8080)
    workload("kube-state-metrics", [state], service_account="kube-state-metrics", extra_spec={"securityContext": {"runAsNonRoot": True, "runAsUser": 65534}})
    node = container("node-exporter", "node-exporter", ["--path.rootfs=/host", "--web.listen-address=:9100", "--collector.textfile.directory=/host/var/lib/glamdb/metrics"], 9100)
    node["volumeMounts"] = [mount("root", "/host")]
    node["volumeMounts"][0]["mountPropagation"] = "HostToContainer"
    workload("node-exporter", [node], [{"name": "root", "hostPath": {"path": "/", "type": "Directory"}}], daemon=True,
             extra_spec={"hostNetwork": True, "hostPID": True, "dnsPolicy": "ClusterFirstWithHostNet", "securityContext": {"runAsUser": 65534, "runAsNonRoot": True}})
    resources = container("resources", "python", ["/app/resource_exporter.py", "--listen", "0.0.0.0:9409", "--cgroup-root", "/host-cgroup", "--kubernetes", "--cluster", cluster, "--namespace", workload_namespace], 9409)
    resources["command"] = ["python3"]
    resources["env"] = [{"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}}, {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}]
    resources["volumeMounts"] = [mount("source", "/app"), mount("cgroup", "/host-cgroup")]
    resources["readinessProbe"]["httpGet"]["path"] = "/-/ready"
    workload("resources", [resources], [config_volume("source", "resource-exporter"), {"name": "cgroup", "hostPath": {"path": "/sys/fs/cgroup", "type": "Directory"}}], daemon=True, service_account="resources", extra_spec={"securityContext": {"runAsUser": 65534, "runAsNonRoot": True}})
    if with_cadvisor:
        cadvisor = container("cadvisor-detail", "cadvisor", ["--housekeeping_interval=15s", "--max_housekeeping_interval=15s", "--store_container_labels=false", "--whitelisted_container_labels=io.kubernetes.pod.namespace,io.kubernetes.pod.name,io.kubernetes.container.name", "--enable_metrics=cpu,network,process,sched,tcp,advtcp"], 8080)
        cadvisor["securityContext"] = {"privileged": True, "readOnlyRootFilesystem": True}
        cadvisor["volumeMounts"] = [mount("root", "/rootfs"), mount("sys", "/sys"), mount("run", "/var/run"), mount("kmsg", "/dev/kmsg")]
        workload("cadvisor-detail", [cadvisor], [{"name": name, "hostPath": {"path": path}} for name, path in (("root", "/"), ("sys", "/sys"), ("run", "/var/run"), ("kmsg", "/dev/kmsg"))], daemon=True)
    for name, port in (("prometheus", 9090), ("grafana", 3000), ("kube-state-metrics", 8080)):
        add("Service", name, {"spec": {"selector": {"app.kubernetes.io/name": name}, "ports": [{"name": "http", "port": port, "targetPort": "http"}]}})
    add("NetworkPolicy", "monitoring-ingress", {"spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}}}]}]}}, "networking.k8s.io/v1")
    return {"apiVersion": "v1", "kind": "List", "items": items}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="glamdb-monitoring")
    parser.add_argument("--workload-namespace", default="glamdb")
    parser.add_argument("--cluster", default="local")
    parser.add_argument("--with-cadvisor", action="store_true")
    args = parser.parse_args()
    print(json.dumps(render(args.namespace, args.workload_namespace, args.cluster, args.with_cadvisor), indent=2))


if __name__ == "__main__":
    main()
