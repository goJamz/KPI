"""
Cluster utilization collector — what the platform costs against what it uses.

Azure knows what a nodepool costs. The cluster knows what is running on it.
Neither knows about the other, so "we are paying for X and using Y" cannot be
answered from either side alone. This joins them.

The join key is the nodepool. A pool bills as one VM scale set named
`aks-<pool>-<hash>-vmss`, and every pod reaches a pool through its node, so
tags never enter into it — which matters, because the tags on Kubernetes
resources and the tags on Azure resources do not line up.

Three tiers of cost, kept apart because they behave differently:

  per-node agents   Falco, ztunnel, node-exporter. Run on the application's own
                    nodes, so they already bill to that pool. Roughly 0.4 CPU
                    per node for UDS Core, measured by comparing two identical
                    system pools where one cluster runs UDS Core and one does
                    not. Scales with the application's node count, so it never
                    gets cheaper as more applications are onboarded.
  central services  istiod, gateways, Keycloak, Loki. Serve every application
                    without running on their nodes. Allocated — and the more
                    applications there are, the less each one carries.
  cluster fixed     control plane, system pools, load balancers, PVCs.

Writes:
  cluster_reports/<env>.json           artifact the renderer reads
  cluster_history/<env>/<monday>.json  committed to the data branch

Reports data, not conclusions. Which figure leads and how it is framed belongs
to the renderer.

Required environment:
  ENVIRONMENT             dev | test | prod
  AZURE_SUBSCRIPTION_ID   subscription to scan
  MANAGEMENT_ENDPOINT     set by azure_login.sh

Optional:
  CLUSTER_REPORTS_DIR     default: cluster_reports
  HISTORY_DIR             default: cluster_history
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
import prom                                              # noqa: E402
from history_io import update_manifest, write_if_changed  # noqa: E402

ENVIRONMENT  = os.environ.get("ENVIRONMENT", "").strip()
SUBSCRIPTION = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
ENDPOINT     = os.environ.get("MANAGEMENT_ENDPOINT", "").rstrip("/")
REPORTS_DIR  = Path(os.environ.get("CLUSTER_REPORTS_DIR", "cluster_reports"))
HISTORY_DIR  = Path(os.environ.get("HISTORY_DIR", "cluster_history"))
# The reporting window is a Monday-to-Sunday week, so this is fixed rather than
# configurable. It was an environment override, which could only ever make the
# Prometheus lookback and the actual cost window disagree while the report went
# on claiming they matched.
COST_DAYS    = 7

# Escape hatch for platform namespaces PLATFORM_NS_HINTS fails to match, since
# the hints are substrings of release names that vary between environments.
# Deliberately not a way to reclassify applications: an application that shares
# a nodepool instead of getting its own is still an application. Sharing is how
# workloads needing no cloud resources of their own get into the cluster, and
# says nothing about who wrote them.
PLATFORM_APP_NS = {n.strip() for n in
                   os.environ.get("PLATFORM_APP_NAMESPACES", "").split(",") if n.strip()}

# Namespaces that are Kubernetes itself rather than anything deployed.
SYSTEM_NS = {"kube-system", "kube-public", "kube-node-lease", "gatekeeper-system",
             "calico-system", "tigera-operator", "azure-arc"}

# Substrings marking a namespace as UDS Core or its supporting services. Matched
# on substrings because release names vary between environments.
# What ships in the UDS bundle, plus the cluster services that come with it.
# Matched on substrings because release names vary between environments.
#
# Deliberately a list of *services* rather than of applications: services are
# few, predictable and change rarely, while the application roster turns over
# constantly and would need maintaining by whoever deploys. Anything not named
# here is an application by default, which is the safer direction to be wrong
# in — a new application is costed immediately rather than silently absorbed
# into platform overhead.
PLATFORM_NS_HINTS = ("istio", "uds", "keycloak", "loki", "monitoring", "pepr",
                     "falco", "neuvector", "grafana", "authservice", "vector",
                     "promtail", "velero", "metrics-server", "zarf", "prometheus",
                     "cert-manager",
                     # data services in the bundle
                     "valkey", "nifi", "zookeeper")

# `aks-<pool>-<hash>-vmss`, and OS disks which repeat that prefix. Non-greedy so
# a pool name containing a hyphen still resolves.
POOL_FROM_RESOURCE = re.compile(r"^aks-(.+?)-\d+")

POOL_LABELS = ("agentpool", "kubernetes.azure.com/agentpool")


def _run(args: list, env: dict | None = None, timeout: int = 300) -> str:
    full = {**os.environ, **(env or {})}
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          check=True, env=full).stdout


def _az(args: list, timeout: int = 300):
    return json.loads(_run(["az"] + args + ["-o", "json"], timeout=timeout) or "null")


def _kubectl(args: list, kubeconfig: str, timeout: int = 120):
    out = _run(["kubectl"] + args + ["-o", "json"], env={"KUBECONFIG": kubeconfig},
               timeout=timeout)
    return json.loads(out or "null")


def cpu_qty(v) -> float:
    """Kubernetes CPU quantity to cores."""
    if not v:
        return 0.0
    v = str(v)
    if v.endswith("m"):
        return float(v[:-1]) / 1000
    if v.endswith("n"):
        return float(v[:-1]) / 1e9
    return float(v)


_MEM_UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40,
              "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def mem_qty(v) -> float:
    """Kubernetes memory quantity to bytes."""
    m = re.match(r"^(\d+(?:\.\d+)?)([A-Za-z]*)$", str(v or ""))
    return float(m.group(1)) * _MEM_UNITS.get(m.group(2), 1) if m else 0.0


def normalize_numbers(node, axis: str | None = None):
    """Round derived figures to the precision they actually carry.

    Every number here is a sum — of costs across resources, of requests across
    containers — and summing floats reintroduces binary error that lands in the
    stored history as 286.24999999999994. `round_costs` in history_io does this
    for the cost schema, but it keys on field names, and here the costs are
    keyed by pool name and the quantities by bucket, so none of them match.

    CPU to a thousandth of a core, which is the millicore the API reports.
    Memory to whole bytes. Everything else is money, to the cent.
    """
    if isinstance(node, dict):
        return {k: normalize_numbers(v, k if k in ("cpu", "mem") else axis)
                for k, v in node.items()}
    if isinstance(node, list):
        return [normalize_numbers(v, axis) for v in node]
    if isinstance(node, float):
        if axis == "cpu":
            return round(node, 3)
        if axis == "mem":
            return float(round(node))
        return round(node, 2)
    return node


# Filled by the last classify_pool call, so the caller can record which hosted
# namespaces were services and which were Kubernetes' own without repeating the
# matching. Not thread-safe, and does not need to be.
_kinds: dict = {}


def classify_pool(hosted: set, mode: str = "") -> tuple:
    """What a pool is, from the workloads scheduled on it.

    Never from its name. Pool names get reused and renamed, and an earlier
    version keyed on them well enough to look right and be wrong.

    `hosted` must exclude DaemonSet pods. Falco and ztunnel run on every node,
    so counting them made every pool look like it hosted the platform — and a
    pool running UDS Core plus one application was attributed entirely to that
    application.
    """
    system   = {n for n in hosted if n in SYSTEM_NS}
    platform = {n for n in hosted
                if n not in SYSTEM_NS
                and (n in PLATFORM_APP_NS or any(h in n for h in PLATFORM_NS_HINTS))}
    apps     = hosted - system - platform
    _kinds.clear()
    _kinds.update({"system": sorted(system), "platform": sorted(platform)})

    # AKS marks a pool System or User, and that is a statement of intent no
    # workload placement can overrule. A single application pod landing on a
    # System pool used to rename the whole pool after it — `sysupgrade` came
    # back as "application (azeiss)". Those pods are spillover: worth reporting,
    # not worth reclassifying a pool over.
    if mode == "System":
        kind = "platform" if platform else "system"
        return kind, [], sorted(apps)

    if platform and apps:
        return "mixed", sorted(apps), []
    if len(apps) == 1:
        return "application", sorted(apps), []
    if len(apps) > 1:
        return "shared", sorted(apps), []
    if platform:
        return "platform", [], []
    if system:
        return "system", [], []
    return "idle", [], []


def cluster_credentials(name: str, rg: str, tmpdir: str) -> str:
    """A kubeconfig for one cluster, in its own file.

    Always fetched rather than trusting whatever context the runner happens to
    carry: the ambient config points at one cluster, and an earlier probe read
    Azure data for one cluster against Kubernetes data from another because of
    exactly that.
    """
    path = str(Path(tmpdir) / f"kubeconfig-{name}")
    _run(["az", "aks", "get-credentials", "-g", rg, "-n", name,
          "--file", path, "--overwrite-existing"])
    client, secret = (os.environ.get("RUNNER_CLIENT_ID", ""),
                      os.environ.get("RUNNER_CLIENT_SECRET", ""))
    if client and secret:
        try:
            # Without explicit credentials the converted config falls back to an
            # interactive device login, which in CI hangs rather than failing.
            _run(["kubelogin", "convert-kubeconfig", "-l", "spn",
                  "--client-id", client, "--client-secret", secret,
                  "--kubeconfig", path], timeout=60)
        except Exception:
            pass          # not every image ships kubelogin; the config may work as-is
    return path


def pool_costs(node_rg: str, start: str, end: str) -> dict:
    """Cost for one cluster's node resource group, grouped by resource.

    A different query from the cost collector's: these resources are created by
    AKS and carry no portfolio tag, so grouping by tag returns nothing. Rows
    that do not resolve to a pool — the load balancer, the control plane, PVCs
    — are real cluster cost and are kept separately rather than dropped.
    """
    body = {
        "type": "ActualCost", "timeframe": "Custom",
        "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"},
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ResourceId"}],
        },
    }
    uri = (f"{ENDPOINT}/subscriptions/{SUBSCRIPTION}/resourceGroups/{node_rg}"
           f"/providers/Microsoft.CostManagement/query?api-version=2023-03-01")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(body, f)
        body_file = f.name
    try:
        raw = _az(["rest", "--method", "post", "--uri", uri,
                   "--body", f"@{body_file}",
                   "--headers", "Content-Type=application/json"])
    finally:
        os.unlink(body_file)

    by_pool: dict = defaultdict(float)
    other: dict = defaultdict(float)
    for row in (raw or {}).get("properties", {}).get("rows", []):
        cost, resource_id = row[0], str(row[1])
        name = resource_id.rsplit("/", 1)[-1]
        m = POOL_FROM_RESOURCE.match(name)
        if m:
            by_pool[m.group(1)] += cost
        else:
            other[name] += cost
    total = round(sum(by_pool.values()) + sum(other.values()), 2)
    return {"by_pool": dict(by_pool), "unattributed": dict(other), "total": total}


def collect_cluster(spec: dict, tmpdir: str, start: str, end: str,
                    end_ts: int) -> dict:
    """Everything known about one cluster. Raises on failure; the caller isolates."""
    name, rg = spec["name"], spec["resourceGroup"]
    node_rg  = spec.get("nodeResourceGroup", "")

    out = {
        "resource_group":      rg,
        "node_resource_group": node_rg,
        "kubernetes_version":  spec.get("kubernetesVersion", ""),
        "nodepool_config": {
            p["name"]: {"vm_size": p.get("vmSize"), "count": p.get("count"),
                        "mode": p.get("mode"), "autoscale": p.get("enableAutoScaling"),
                        "min": p.get("minCount"), "max": p.get("maxCount")}
            for p in spec.get("agentPoolProfiles", []) or []
        },
    }

    # Cost does not need the cluster to be reachable, so gather it first: an
    # unreachable cluster still costs money and should still report what it costs.
    try:
        out["cost"] = pool_costs(node_rg, start, end)
        out["cost"]["window"] = {"from": start, "to": end}
    except Exception as e:
        out["cost"] = {"error": str(e)}
        print(f"[WARN] {name}: cost query failed: {e}", file=sys.stderr)

    kubeconfig = cluster_credentials(name, rg, tmpdir)
    nodes = _kubectl(["get", "nodes"], kubeconfig)["items"]
    pods  = _kubectl(["get", "pods", "--all-namespaces"], kubeconfig)["items"]
    nss   = _kubectl(["get", "namespaces"], kubeconfig)["items"]

    node_pool: dict = {}
    pools: dict = defaultdict(lambda: {
        "nodes": 0, "vm_sizes": set(), "hosted": set(), "unrequested_pods": 0,
        "cpu": defaultdict(float), "mem": defaultdict(float)})

    for n in nodes:
        labels = n["metadata"].get("labels", {})
        pool   = next((labels[k] for k in POOL_LABELS if k in labels), "(unlabeled)")
        node_pool[n["metadata"]["name"]] = pool
        entry  = pools[pool]
        entry["nodes"] += 1
        entry["vm_sizes"].add(labels.get("node.kubernetes.io/instance-type", "?"))
        alloc, cap = n["status"].get("allocatable", {}), n["status"].get("capacity", {})
        entry["cpu"]["capacity"]    += cpu_qty(cap.get("cpu"))
        entry["cpu"]["allocatable"] += cpu_qty(alloc.get("cpu"))
        entry["mem"]["capacity"]    += mem_qty(cap.get("memory"))
        entry["mem"]["allocatable"] += mem_qty(alloc.get("memory"))

    ns_mode = {}
    for n in nss:
        labels, nsn = n["metadata"].get("labels", {}), n["metadata"]["name"]
        if labels.get("istio.io/dataplane-mode") == "ambient":
            ns_mode[nsn] = "ambient"
        elif labels.get("istio-injection") == "enabled":
            ns_mode[nsn] = "sidecar"
        elif "istio.io/rev" in labels:
            ns_mode[nsn] = "sidecar"
        else:
            ns_mode[nsn] = "none"

    def _ns_pool():
        return {"pods": 0, "unrequested_pods": 0,
                "cpu": defaultdict(float), "mem": defaultdict(float)}

    namespaces: dict = defaultdict(lambda: {
        "pods": 0, "sidecar_pods": 0, "mesh_pods": 0, "unrequested_pods": 0,
        "pools": set(), "cpu": defaultdict(float), "mem": defaultdict(float),
        # Namespace totals repeated against each pool a namespace touches read
        # as separate workloads and sum to more than exists. A namespace split
        # across pools has to be split in the figures too.
        "by_pool": defaultdict(_ns_pool)})

    for pod in pods:
        if pod.get("status", {}).get("phase") not in ("Running", "Pending"):
            continue
        nsn  = pod["metadata"]["namespace"]
        pool = node_pool.get(pod.get("spec", {}).get("nodeName"), "(unscheduled)")
        is_ds = any(o.get("kind") == "DaemonSet"
                    for o in pod["metadata"].get("ownerReferences", []) or [])

        containers = pod.get("spec", {}).get("containers", []) or []
        names      = [c.get("name") for c in containers]
        # A sidecar is istio-proxy *alongside* application containers. A pod
        # whose only container is istio-proxy is a waypoint or a gateway —
        # ambient mesh infrastructure. Conflating them reported every ambient
        # namespace as a failed migration.
        proxy_only = "istio-proxy" in names and len(names) == 1
        is_sidecar = "istio-proxy" in names and not proxy_only

        namespaces[nsn]["pods"] += 1
        namespaces[nsn]["pools"].add(pool)
        np_ = namespaces[nsn]["by_pool"][pool]
        np_["pods"] += 1
        if is_sidecar:
            namespaces[nsn]["sidecar_pods"] += 1
        if proxy_only and not is_ds:
            namespaces[nsn]["mesh_pods"] += 1
        if not is_ds and pool in pools:
            pools[pool]["hosted"].add(nsn)

        # A pod declaring no CPU request is invisible to the scheduler's
        # bin-packing and to any allocation done by request share: it consumes
        # real capacity while claiming none. That makes every requests-based
        # figure below an understatement, so the count travels with them.
        if not any(cpu_qty(((c.get("resources", {}) or {}).get("requests", {}) or {})
                           .get("cpu")) for c in containers):
            namespaces[nsn]["unrequested_pods"] += 1
            np_["unrequested_pods"] += 1
            if pool in pools:
                pools[pool]["unrequested_pods"] += 1

        for c in containers:
            req = (c.get("resources", {}) or {}).get("requests", {}) or {}
            cc, cm = cpu_qty(req.get("cpu")), mem_qty(req.get("memory"))
            if is_ds:
                bucket = "daemonset"
            elif c.get("name") == "istio-proxy":
                bucket = "waypoint" if proxy_only else "sidecar"
            else:
                bucket = "app"
            if pool in pools:
                pools[pool]["cpu"][bucket] += cc
                pools[pool]["mem"][bucket] += cm
            namespaces[nsn]["cpu"][bucket] += cc
            namespaces[nsn]["mem"][bucket] += cm
            np_["cpu"][bucket] += cc
            np_["mem"][bucket] += cm

    # Windowed averages, evaluated to close where the cost window closes. Absent
    # when the cluster has no Prometheus, which is a fact about the cluster
    # rather than a failure — requests and cost still stand without it.
    usage = prom.collect(kubeconfig, COST_DAYS, end_ts)
    # Everything the usage collector reports except the series maps themselves.
    # This was an allowlist of metadata field names, which meant adding a field
    # in prom.py and forgetting to add it here — so `window_days_covered` and
    # `aligned_to_cost_window` were computed, logged, and then dropped before
    # they reached the report, and neither warning the panel draws could ever
    # appear. Excluding the bulk instead lets new metadata through by default.
    _BULK = {"avg_nodes_by_pool", "cpu_by_namespace", "mem_by_namespace",
             "cpu_by_node", "mem_by_node", "cpu_by_namespace_node"}
    out["usage"] = {"available": bool(usage)}
    if usage:
        out["usage"].update({k: v for k, v in usage.items() if k not in _BULK})
        print(f"[INFO]   usage from {usage['source']}"
              f"{'' if usage['windowed'] else ' (single sample, not a window average)'}")

    cpu_by_node = (usage or {}).get("cpu_by_node", {})
    mem_by_node = (usage or {}).get("mem_by_node", {})
    avg_nodes   = (usage or {}).get("avg_nodes_by_pool", {})
    cpu_by_ns   = (usage or {}).get("cpu_by_namespace", {})
    mem_by_ns   = (usage or {}).get("mem_by_namespace", {})

    # Usage split to (namespace, pool), so a namespace spanning pools is
    # attributed to each rather than counted whole against both.
    ns_pool_used: dict = defaultdict(float)
    for key, value in ((usage or {}).get("cpu_by_namespace_node") or {}).items():
        ns_name, _, node_name = key.partition("\u0000")
        ns_pool_used[(ns_name, node_pool.get(node_name, ""))] += value

    pool_used: dict = defaultdict(lambda: {"cpu": 0.0, "mem": 0.0})
    for node_name, pool in node_pool.items():
        pool_used[pool]["cpu"] += cpu_by_node.get(node_name, 0.0)
        pool_used[pool]["mem"] += mem_by_node.get(node_name, 0.0)

    cost_by_pool = out.get("cost", {}).get("by_pool", {})
    out["pools"] = {}
    modes = {name: cfg.get("mode", "") for name, cfg in out["nodepool_config"].items()}
    for pool, e in pools.items():
        kind, apps, spillover = classify_pool(e["hosted"], modes.get(pool, ""))
        for axis in ("cpu", "mem"):
            for bucket in ("daemonset", "waypoint", "sidecar", "app"):
                e[axis].setdefault(bucket, 0.0)
            # What an application can actually use: allocatable less the
            # per-node agents it is obliged to host. Measuring against
            # allocatable overstates waste by 15-30%.
            e[axis]["usable"] = e[axis]["allocatable"] - e[axis]["daemonset"]
            e[axis]["requested"] = (e[axis]["app"] + e[axis]["sidecar"]
                                    + e[axis]["waypoint"])
            e[axis]["unclaimed"] = e[axis]["usable"] - e[axis]["requested"]
        if usage:
            # Two different gaps with two different owners. usable - requested
            # is capacity nobody asked for, which the platform team resolves by
            # resizing pools. requested - used is over-reservation, which the
            # teams that wrote the manifests resolve. Averaging them together
            # would point at nobody.
            for axis, used in (("cpu", pool_used[pool]["cpu"]),
                               ("mem", pool_used[pool]["mem"])):
                e[axis]["used"] = used
                e[axis]["over_requested"] = e[axis]["requested"] - used
                # A pool cannot use more than it has. When this fires the query
                # is wrong, not the cluster — it caught an aggregation ordering
                # bug that reported 58.77 cores used on a four-core node.
                if used > e[axis]["capacity"] * 1.1:
                    print(f"[WARN] {name}/{pool}: {axis} used {used:.2f} exceeds "
                          f"capacity {e[axis]['capacity']:.2f} — usage query suspect",
                          file=sys.stderr)
        out["pools"][pool] = {
            "nodes": e["nodes"], "vm_sizes": sorted(e["vm_sizes"]),
            "unrequested_pods": e["unrequested_pods"],
            "classification": kind, "applications": apps,
            "mode": modes.get(pool, ""), "spillover": spillover,
            # Which hosted namespaces were services and which were Kubernetes'
            # own, so placement can be judged without re-deriving the rules.
            "platform_namespaces": _kinds.get("platform", []),
            "system_namespaces": _kinds.get("system", []),
            "namespaces": sorted(e["hosted"]),
            "cpu": dict(e["cpu"]), "mem": dict(e["mem"]),
            "cost": cost_by_pool.get(pool, 0.0),
            # Average over the window, against `nodes` which is the count at
            # collection. Where they differ the pool resized, and spend belongs
            # to the average rather than to what happens to be running now.
            "avg_nodes": avg_nodes.get(pool),
        }

    out["namespaces"] = {
        n: {"pods": v["pods"], "sidecar_pods": v["sidecar_pods"],
            "mesh_pods": v["mesh_pods"], "unrequested_pods": v["unrequested_pods"],
            "pools": sorted(v["pools"]),
            "dataplane_mode": ns_mode.get(n, "none"),
            "cpu": dict(v["cpu"]), "mem": dict(v["mem"]),
            "used": ({"cpu": cpu_by_ns.get(n, 0.0), "mem": mem_by_ns.get(n, 0.0)}
                     if usage else None),
            "by_pool": {p: {"pods": d["pods"],
                            "unrequested_pods": d["unrequested_pods"],
                            "cpu": dict(d["cpu"]), "mem": dict(d["mem"]),
                            "used": ({"cpu": ns_pool_used.get((n, p), 0.0)}
                                     if usage else None)}
                        for p, d in v["by_pool"].items()}}
        for n, v in namespaces.items()
    }

    kinds = {p["classification"] for p in out["pools"].values()}
    has_apps = any(p["applications"] for p in out["pools"].values())
    if "platform" in kinds or "mixed" in kinds:
        out["state"] = "uds_with_apps" if has_apps else "uds_no_apps"
    else:
        out["state"] = "no_uds"
    return out


def main() -> None:
    if not ENVIRONMENT:
        sys.exit("[ERROR] ENVIRONMENT is not set")

    now = datetime.now(timezone.utc)
    # The most recently completed Monday-Sunday week, which is what the cost
    # collector reports. Two things were wrong with deriving a trailing seven
    # days ending yesterday instead:
    #
    #   * the window moved with the day of the run, so it matched the cost
    #     period only on the Monday schedule and the page could not state
    #     Kubernetes as a share of spend on any other day;
    #   * the shard was named for the Monday of the *current* week while
    #     holding the previous week's spend, so cluster history and cost
    #     history were offset by one week in their filenames. Joining them
    #     later would have been wrong, and wrong in a way nothing would show.
    #
    # Same derivation as query_azure_costs.sh, so the two cannot drift.
    period_end   = now.date() - timedelta(days=now.date().weekday() + 1)
    period_start = period_end - timedelta(days=6)
    week  = period_start.isoformat()
    end   = period_end.isoformat()
    start = period_start.isoformat()
    # Prometheus lookbacks are evaluated here so they close where cost closes.
    end_ts = int(datetime.fromisoformat(f"{end}T23:59:59+00:00").timestamp())

    try:
        specs = _az(["aks", "list"]) or []
    except Exception as e:
        sys.exit(f"[ERROR] could not list AKS clusters: {e}")
    print(f"[INFO] {len(specs)} cluster(s) in {ENVIRONMENT}; "
          f"cost window {start} to {end}")

    clusters: dict = {}
    failures = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for spec in specs:
            name = spec["name"]
            try:
                clusters[name] = collect_cluster(spec, tmpdir, start, end, end_ts)
                clusters[name]["status"] = "ok"
                c = clusters[name]
                print(f"[INFO] {name}: {c['state']}, {len(c['pools'])} pool(s), "
                      f"${c.get('cost', {}).get('total', 0):.2f} over {COST_DAYS} days")
            except Exception as e:
                failures += 1
                # An unreachable cluster is one cluster's problem. Reporting it
                # as such keeps the others' figures usable.
                clusters[name] = {"status": "failed", "state": "unreachable",
                                  "message": str(e)}
                print(f"[WARN] {name}: {e}", file=sys.stderr)

    if failures == 0:
        status = "ok"
    elif failures < len(specs):
        status = "partial"
    else:
        status = "failed"

    payload = normalize_numbers({
        "environment":   ENVIRONMENT,
        "collected_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "week":          week,
        "status":        status,
        "cost_window":   {"from": start, "to": end, "days": COST_DAYS},
        "clusters":      clusters,
    })

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / f"{ENVIRONMENT}.json").write_text(json.dumps(payload, indent=2))
    print(f"[INFO] Wrote {REPORTS_DIR}/{ENVIRONMENT}.json (status: {status})")

    shard = HISTORY_DIR / ENVIRONMENT / f"{week}.json"
    if write_if_changed(shard, payload, ignore_keys=("collected_utc",)):
        carried, pending = update_manifest(HISTORY_DIR, [shard])
        print(f"[INFO] Recorded {ENVIRONMENT}/{week}.json; {pending} file(s) awaiting commit")
    else:
        print(f"[INFO] {ENVIRONMENT}/{week}.json unchanged — nothing to commit")

    if status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
