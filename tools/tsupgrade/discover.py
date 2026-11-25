"""Find the deployment's names from Patroni's own settings.

``preflight``, ``apply`` and ``rollback`` need the pods' label selector, the
StatefulSet and the database container. The defaults are this repository's
(``application=patroni``, ``tsdb``, ``timescaledb``); a chart names them
otherwise (the archived ``timescaledb-single`` labels its pods ``app``,
``cluster-name`` and ``release``). Patroni already states them in the pod
spec, so when no flag names them and the defaults find no pod, they are read
from there:

* the pods are those with a container whose environment sets ``PATRONI_SCOPE``;
* the selector is ``PATRONI_KUBERNETES_LABELS``, the labels Patroni itself
  selects its members by;
* the StatefulSet is the pods' owner;
* the container is the one carrying that environment.

More than one Patroni cluster in the namespace is refused, listing them: the
flags have to say which. Only ``kubectl get pods`` is used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["DEFAULT_CONTAINER", "DEFAULT_SELECTOR", "DEFAULT_STATEFULSET", "DiscoveryError",
           "Names", "parse_labels", "resolve_names"]

DEFAULT_SELECTOR = "application=patroni"
DEFAULT_STATEFULSET = "tsdb"
DEFAULT_CONTAINER = "timescaledb"


class DiscoveryError(Exception):
    """The names could not be found; nothing was read beyond the pod list."""


@dataclass
class Names:
    selector: str
    statefulset: str
    container: str
    # What was discovered and from where, for preflight's "Cluster and
    # namespace" check and apply's report; empty when nothing was.
    discovered: List[str] = field(default_factory=list)


def parse_labels(value: str) -> Dict[str, str]:
    """``PATRONI_KUBERNETES_LABELS``: Patroni reads it as YAML, so JSON or a
    flow mapping such as ``{app: x, release: y}``."""
    try:
        doc = json.loads(value)
    except ValueError:
        doc = None
        body = value.strip()
        if body.startswith("{") and body.endswith("}"):
            doc = {}
            for item in filter(None, (i.strip() for i in body[1:-1].split(","))):
                key, sep, val = item.partition(":")
                if not sep:
                    return {}
                doc[key.strip().strip("'\"")] = val.strip().strip("'\"")
    if not isinstance(doc, dict):
        return {}
    return {str(k): str(v) for k, v in doc.items()}


def _env(container: Dict[str, Any], name: str) -> Optional[str]:
    return next((e.get("value") for e in container.get("env") or [] if e.get("name") == name), None)


def _patroni_pods(pods: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per Patroni scope: the selector, the owning StatefulSets, the
    containers, and the pods, as the pod specs state them."""
    found: Dict[str, Dict[str, Any]] = {}
    for pod in pods:
        meta = pod.get("metadata", {})
        for c in pod.get("spec", {}).get("containers") or []:
            scope = _env(c, "PATRONI_SCOPE")
            if not scope:
                continue
            entry = found.setdefault(scope, {"labels": set(), "owners": set(), "containers": set(),
                                             "pods": []})
            labels = parse_labels(_env(c, "PATRONI_KUBERNETES_LABELS") or "")
            entry["labels"].add(",".join("{0}={1}".format(k, v) for k, v in sorted(labels.items())))
            entry["owners"].update(r.get("name") for r in meta.get("ownerReferences") or []
                                   if r.get("kind") == "StatefulSet")
            entry["containers"].add(c.get("name"))
            entry["pods"].append(meta.get("name"))
    return found


def resolve_names(kube: Any, namespace: str, selector: Optional[str] = None,
                  statefulset: Optional[str] = None, container: Optional[str] = None) -> Names:
    """The flags as given, the defaults for any not given when the selector
    finds pods, or else what Patroni's pods say. Raises :class:`DiscoveryError`."""
    chosen = selector or DEFAULT_SELECTOR
    if selector or kube.json(["get", "pods", "-l", chosen]).get("items"):
        # Named, or the defaults find the pods: nothing to discover. A given
        # selector that matches nothing is left for the checks to report.
        return Names(chosen, statefulset or DEFAULT_STATEFULSET, container or DEFAULT_CONTAINER)

    found = _patroni_pods(kube.json(["get", "pods"]).get("items", []))
    if not found:
        raise DiscoveryError(
            "no pod matches the default selector {0} in namespace {1}, and none sets "
            "PATRONI_SCOPE; name them with --selector, --statefulset and --container".format(
                DEFAULT_SELECTOR, namespace))
    if len(found) > 1:
        raise DiscoveryError(
            "namespace {0} has {1} Patroni clusters: {2}; say which with --selector, "
            "--statefulset and --container".format(namespace, len(found), "; ".join(
                "scope {0} (statefulset {1}, selector {2})".format(
                    scope, "/".join(sorted(o for o in e["owners"] if o)) or "?",
                    " or ".join(sorted(e["labels"])) or "?")
                for scope, e in sorted(found.items()))))
    scope, entry = next(iter(found.items()))
    problems: List[str] = []
    one: Dict[str, str] = {}
    for key, what in (("labels", "PATRONI_KUBERNETES_LABELS"), ("owners", "owning StatefulSet"),
                      ("containers", "container with PATRONI_SCOPE")):
        values = {v for v in entry[key] if v}
        if len(values) != 1:
            problems.append("{0}: {1}".format(what, ", ".join(sorted(values)) or "none"))
        else:
            one[key] = values.pop()
    if problems:
        raise DiscoveryError("the pods of Patroni scope {0} ({1}) do not state one {2}; name them "
                             "with --selector, --statefulset and --container".format(
                                 scope, ", ".join(sorted(entry["pods"])), "; ".join(problems)))
    names = Names(one["labels"], statefulset or one["owners"], container or one["containers"])
    names.discovered = [
        "discovered      : Patroni scope {0}, from PATRONI_SCOPE on {1}".format(
            scope, ", ".join(sorted(entry["pods"]))),
        "  selector      : {0} (PATRONI_KUBERNETES_LABELS)".format(names.selector),
        "  statefulset   : {0} (the pods' owner)".format(names.statefulset),
        "  container     : {0}".format(names.container),
    ]
    return names
