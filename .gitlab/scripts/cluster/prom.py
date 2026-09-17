"""
Prometheus access for the cluster collector, over kubectl port-forward.

Why port-forward rather than the API server's service proxy: the proxy returns
504 dialing the pod, which is consistent with ambient mesh intercepting
plaintext arriving from outside the mesh. Port-forward goes API server ->
kubelet -> pod and is unaffected. Both were measured; this is the one that works.

Why this matters at all: spend is integrated across a week while node counts and
requests are read at one instant. On a busy cluster 23 of 38 nodepools showed a
node count their spend could not account for, because they had scaled up shortly
before collection. Averages over the same window the cost covers are the only
thing that makes the two comparable.

Every query is evaluated at an explicit `end` timestamp so the lookback closes
exactly where the cost window closes. Without that the two would describe
overlapping but different periods, which is the bug this is meant to remove.

Returns None rather than raising when Prometheus is absent or unreachable: a
cluster without monitoring still has requests and cost worth reporting.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request

LOCAL_PORT = 19090
READY_TIMEOUT = 90
QUERY_TIMEOUT = 120

# Matched against service names. The first round of probing queried the first
# service whose name merely contained "prometheus", which was a CoreDNS metrics
# exporter, and reported the resulting failure as a retention problem.
SERVICE_NAMES = ("prometheus", "prometheus-operated", "prometheus-server",
                 "kube-prometheus-stack-prometheus")


def discover(kubeconfig: str) -> tuple | None:
    """(namespace, service, port) for the Prometheus server, or None."""
    try:
        out = subprocess.run(
            ["kubectl", "get", "svc", "--all-namespaces", "-o", "json"],
            capture_output=True, text=True, timeout=60, check=True,
            env={"KUBECONFIG": kubeconfig, "PATH": _path()}).stdout
        items = json.loads(out).get("items", [])
    except Exception:
        return None

    for svc in items:
        name = svc["metadata"]["name"]
        if name not in SERVICE_NAMES:
            continue
        for port in svc.get("spec", {}).get("ports", []) or []:
            if port.get("port") == 9090:
                return svc["metadata"]["namespace"], name, 9090
    return None


def ready_endpoint(kubeconfig: str, namespace: str, service: str) -> tuple:
    """(pod, port, reason) for a ready backend of `service`.

    `kubectl port-forward svc/...` picks a pod itself, and will happily pick one
    that is not serving: dev failed with "connection refused inside namespace",
    meaning the chosen pod had nothing listening. Resolving the endpoints first
    targets a pod that is actually ready, and distinguishes "Prometheus is not
    serving here" from "the forward broke" — different problems with different
    owners.
    """
    try:
        out = subprocess.run(
            ["kubectl", "get", "endpoints", "-n", namespace, service, "-o", "json"],
            capture_output=True, text=True, timeout=60, check=True,
            env={"KUBECONFIG": kubeconfig, "PATH": _path()}).stdout
        subsets = json.loads(out).get("subsets", []) or []
    except Exception as e:
        return None, None, f"could not read endpoints: {e}"

    not_ready = 0
    for subset in subsets:
        not_ready += len(subset.get("notReadyAddresses", []) or [])
        ports = [p.get("port") for p in subset.get("ports", []) or []]
        for addr in subset.get("addresses", []) or []:
            ref = addr.get("targetRef") or {}
            if ref.get("kind") == "Pod" and ref.get("name"):
                return ref["name"], (ports[0] if ports else 9090), ""
    if not_ready:
        return None, None, f"{not_ready} endpoint(s) present but none ready"
    return None, None, "service has no endpoints"


def _path() -> str:
    import os
    return os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")


class PortForward:
    """kubectl port-forward for the life of a `with` block."""

    def __init__(self, kubeconfig: str, namespace: str, target: str, port: int):
        self.args = ["kubectl", "port-forward", "-n", namespace,
                     target, f"{LOCAL_PORT}:{port}"]
        self.kubeconfig = kubeconfig
        self.proc = None

    def __enter__(self):
        import os
        import tempfile
        # Keeping stderr: discarding it once already turned a diagnosable
        # failure into "did not come up", which said nothing about why.
        self._log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
        self.proc = subprocess.Popen(
            self.args, stdout=subprocess.DEVNULL, stderr=self._log,
            env={**os.environ, "KUBECONFIG": self.kubeconfig})
        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline:
            if self.proc.poll() is not None:
                print(f"[WARN] port-forward exited: {self.error()}", file=sys.stderr)
                return None
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{LOCAL_PORT}/-/ready", timeout=2).read()
                return self
            except Exception:
                time.sleep(1)
        self.__exit__(None, None, None)
        return None

    def error(self) -> str:
        """Whatever kubectl said, for a failure that would otherwise be silent."""
        try:
            self._log.flush()
            self._log.seek(0)
            return " / ".join(l.strip() for l in self._log.readlines()[-3:] if l.strip())
        except Exception:
            return "no output captured"

    def __exit__(self, *_exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


def query(promql: str, at: int) -> list:
    """Instant query evaluated at `at`. Returns the result series, or []."""
    url = (f"http://127.0.0.1:{LOCAL_PORT}/api/v1/query"
           f"?query={urllib.parse.quote(promql)}&time={at}")
    try:
        with urllib.request.urlopen(url, timeout=QUERY_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as e:
        print(f"[WARN] prometheus query failed: {e}", file=sys.stderr)
        return []
    if payload.get("status") != "success":
        print(f"[WARN] prometheus: {payload.get('error', 'unknown error')}",
              file=sys.stderr)
        return []
    return payload.get("data", {}).get("result", []) or []


def _by_label(series: list, label: str) -> dict:
    out = {}
    for s in series:
        key = s.get("metric", {}).get(label)
        if not key:
            continue
        try:
            out[key] = float(s["value"][1])
        except (KeyError, IndexError, ValueError):
            continue
    return out


def _by_labels(series: list, labels: tuple) -> dict:
    """Series keyed by a tuple of label values, for splitting one axis by another."""
    out = {}
    for s in series:
        metric = s.get("metric", {})
        key = tuple(metric.get(l) for l in labels)
        if any(k is None for k in key):
            continue
        try:
            out[key] = float(s["value"][1])
        except (KeyError, IndexError, ValueError):
            continue
    return out


def _first_that_returns(candidates: list, at: int, label: str,
                        what: str = "") -> tuple:
    """Try each query in turn; return (values, which) for the first with data.

    Recording rule and label names differ between kube-prometheus-stack versions
    and between clusters, so the working form is discovered rather than assumed.

    Every attempt is reported. Returning nothing used to be indistinguishable
    from Prometheus being absent, which cost a run to work out — the same
    mistake as discarding kubectl's stderr, made again.
    """
    for promql in candidates:
        values = _by_label(query(promql, at), label)
        if values:
            print(f"[INFO]   {what or label}: {len(values)} series", file=sys.stderr)
            return values, promql
        print(f"[WARN]   {what or label}: no series from {promql[:90]}",
              file=sys.stderr)
    return {}, ""


def available_days(days: int, end: int) -> int:
    """How far back data actually goes, up to `days`.

    `avg_over_time(x[7d])` averages whatever points exist in the window and
    ignores the gaps, so a server holding two days of history answers a
    seven-day query without complaint. The answer is a two-day average wearing a
    seven-day label.

    That is not hypothetical here: dev's Prometheus crash-looped for days on a
    full disk, and whatever it holds after being repaired will be short of the
    window for a week afterwards. Reporting the span that was really covered
    costs one query per step and keeps the figures honest about their basis.
    """
    for candidate in (days, 5, 3, 2, 1):
        if candidate > days:
            continue
        moment = end - candidate * 86400
        if query("count(up)", moment):
            return candidate
    return 0


def collect(kubeconfig: str, days: int, end: int) -> dict | None:
    """Windowed averages for one cluster, or None if Prometheus is unavailable."""
    target = discover(kubeconfig)
    if not target:
        return None
    namespace, service, port = target

    pod, pod_port, reason = ready_endpoint(kubeconfig, namespace, service)
    if not pod:
        # Not a failure of this collector: the cluster has no serving Prometheus
        # to ask. Requests and cost still stand without it.
        print(f"[WARN] {namespace}/{service}: {reason} — usage not collected",
              file=sys.stderr)
        return None

    with PortForward(kubeconfig, namespace, f"pod/{pod}", pod_port) as pf:
        if pf is None:
            print(f"[WARN] port-forward to {namespace}/{pod}:{pod_port} did not "
                  f"come up within {READY_TIMEOUT}s", file=sys.stderr)
            return None

        # Queries are anchored to where the cost window closes, so the two
        # describe the same period. A server younger than that window has no
        # data there at all, and every query comes back empty while Prometheus
        # is healthy and scraping — which is what a rebuilt TSDB looks like for
        # the first days after an outage. Fall back to now and say so, rather
        # than reporting nothing about a cluster that is being measured.
        anchor, aligned = end, True
        if not query("count(up)", end):
            now = int(time.time())
            if query("count(up)", now):
                anchor, aligned = now, False
                print("[WARN] prometheus holds no data at the end of the cost "
                      "window — usage is measured to now instead, so it covers a "
                      "later period than the spend beside it", file=sys.stderr)

        # Ask what history exists before averaging over an assumed window.
        covered = available_days(days, anchor)
        if covered and covered < days:
            print(f"[WARN] prometheus holds {covered}d of history, not {days}d — "
                  f"averages cover the shorter span", file=sys.stderr)
        w = f"{days}d"
        # Filter both the pause container and unlabelled cgroup rollups, or the
        # same CPU is counted more than once.
        sel = '{container!="",container!="POD"}'

        def windowed(inner: str, step: str = "10m") -> list:
            """avg_over_time OUTSIDE the aggregation, which is the whole point.

            Averaging each series first and summing afterwards treats a
            container that lived an hour as though it ran all week: a node
            hosting many short-lived pods sums dozens of alive-time averages
            as if they were concurrent. Prod reported 58.77 cores used on a
            four-core node that way. Summing at each step and averaging the
            totals is the figure actually wanted.
            """
            return [f"avg_over_time({inner}[{w}:{step}])", inner]

        # Average node count per pool. This settles the part-window question
        # directly, which no snapshot can.
        nodes, nodes_q = _first_that_returns([
            f"avg_over_time(count by (label_agentpool) (kube_node_labels)[{w}:1h])",
        ], anchor, "label_agentpool", "nodes by pool")
        if not nodes:
            nodes, nodes_q = _first_that_returns([
                f"avg_over_time(count by (label_kubernetes_azure_com_agentpool) "
                f"(kube_node_labels)[{w}:1h])",
            ], anchor, "label_kubernetes_azure_com_agentpool", "nodes by pool (alt label)")

        cpu_ns, cpu_q = _first_that_returns(windowed(
            f"sum by (namespace) (rate(container_cpu_usage_seconds_total{sel}[5m]))"),
            anchor, "namespace", "cpu by namespace")

        mem_ns, mem_q = _first_that_returns(windowed(
            f"sum by (namespace) (container_memory_working_set_bytes{sel})"),
            anchor, "namespace", "memory by namespace")

        cpu_node, _ = _first_that_returns(windowed(
            f"sum by (node) (rate(container_cpu_usage_seconds_total{sel}[5m]))"),
            anchor, "node", "cpu by node")

        mem_node, _ = _first_that_returns(windowed(
            f"sum by (node) (container_memory_working_set_bytes{sel})"),
            anchor, "node", "memory by node")

        # Split by node as well as namespace, so a namespace spread over two
        # pools is attributed to each rather than counted whole against both.
        cpu_ns_node = {}
        for q in windowed(f"sum by (namespace, node) "
                          f"(rate(container_cpu_usage_seconds_total{sel}[5m]))"):
            cpu_ns_node = _by_labels(query(q, anchor), ("namespace", "node"))
            if cpu_ns_node:
                break

    if not any((nodes, cpu_ns, mem_ns, cpu_node, mem_node)):
        print(f"[WARN] {namespace}/{service} answered every query with no series — "
              f"reachable but holding nothing useful at the evaluated time",
              file=sys.stderr)
        return None

    # Whether the figures are genuine windowed averages or a single sample the
    # fallback produced. The page must not present the second as the first.
    windowed = bool(cpu_q and "avg_over_time" in cpu_q)
    return {
        "source": f"prometheus ({namespace}/{service})",
        "window_days": days,
        # What the averages actually span, which is not always what was asked
        # for. A server rebuilt after an outage answers a seven-day query with
        # however many days it has.
        "window_days_covered": covered,
        # False when the averages had to be taken to now because the cost
        # window predates the data. The figures are real; they describe a
        # different period from the spend they sit beside.
        "aligned_to_cost_window": aligned,
        "windowed": windowed and covered >= days,
        "avg_nodes_by_pool": nodes,
        "cpu_by_namespace": cpu_ns,
        "mem_by_namespace": mem_ns,
        "cpu_by_node": cpu_node,
        "mem_by_node": mem_node,
        # tuple keys will not survive JSON, so flatten on the way out
        "cpu_by_namespace_node": {f"{ns}\u0000{node}": v
                                  for (ns, node), v in cpu_ns_node.items()},
    }
