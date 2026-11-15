"""R31: the selector, StatefulSet and container, from Patroni's own settings.

The pods here are laid out as the archived ``timescaledb-single`` chart does:
labels ``app``, ``cluster-name`` and ``release``, none of which the defaults
select on.
"""

from __future__ import annotations

import json

import pytest

from tsupgrade import cli
from tsupgrade.discover import DiscoveryError, parse_labels, resolve_names
from tsupgrade.preflight import PreflightError, PreflightOptions, check_context

CHART_LABELS = {"app": "tsdb-timescaledb", "cluster-name": "tsdb", "release": "tsdb"}
CHART_SELECTOR = "app=tsdb-timescaledb,cluster-name=tsdb,release=tsdb"


def _pod(name, scope="tsdb", labels=None, owner="tsdb", container="timescaledb", env_labels=None):
    labels = CHART_LABELS if labels is None else labels
    env = [{"name": "PATRONI_SCOPE", "value": scope}] if scope else []
    if env_labels is not False:
        env.append({"name": "PATRONI_KUBERNETES_LABELS",
                    "value": env_labels if env_labels else json.dumps(labels)})
    return {"metadata": {"name": name, "labels": dict(labels),
                         "ownerReferences": [{"kind": "StatefulSet", "name": owner}] if owner else []},
            "spec": {"containers": [{"name": "pgbackrest", "env": []},
                                    {"name": container, "env": env}]}}


class PodsKube:
    def __init__(self, pods):
        self.pods = pods
        self.asked = []

    def json(self, args, namespaced=True):
        self.asked.append(args)
        assert args[:2] == ["get", "pods"], args
        if "-l" in args:
            want = dict(kv.split("=", 1) for kv in args[args.index("-l") + 1].split(","))
            return {"items": [p for p in self.pods
                              if all(p["metadata"]["labels"].get(k) == v for k, v in want.items())]}
        return {"items": self.pods}


def test_the_charts_names_are_discovered_when_the_defaults_find_nothing():
    names = resolve_names(PodsKube([_pod("tsdb-0"), _pod("tsdb-1")]), "timescale")
    assert (names.selector, names.statefulset, names.container) == (CHART_SELECTOR, "tsdb", "timescaledb")
    text = "\n".join(names.discovered)
    assert "Patroni scope tsdb, from PATRONI_SCOPE on tsdb-0, tsdb-1" in text
    assert CHART_SELECTOR + " (PATRONI_KUBERNETES_LABELS)" in text


def test_the_defaults_are_kept_when_they_find_pods():
    kube = PodsKube([_pod("tsdb-0", labels={"application": "patroni"})])
    names = resolve_names(kube, "timescale")
    assert (names.selector, names.statefulset, names.container, names.discovered) == (
        "application=patroni", "tsdb", "timescaledb", [])
    assert kube.asked == [["get", "pods", "-l", "application=patroni"]]


def test_a_given_selector_is_never_second_guessed():
    kube = PodsKube([_pod("tsdb-0")])
    names = resolve_names(kube, "timescale", selector="app=other")
    assert names.selector == "app=other" and not names.discovered
    assert kube.asked == []


def test_given_flags_win_over_what_is_discovered():
    names = resolve_names(PodsKube([_pod("tsdb-0")]), "timescale", statefulset="mine", container="pg")
    assert (names.selector, names.statefulset, names.container) == (CHART_SELECTOR, "mine", "pg")


def test_two_patroni_clusters_are_refused_and_listed():
    pods = [_pod("a-0", scope="alpha", owner="a", labels={"cluster-name": "alpha"}),
            _pod("b-0", scope="beta", owner="b", labels={"cluster-name": "beta"})]
    with pytest.raises(DiscoveryError) as err:
        resolve_names(PodsKube(pods), "timescale")
    message = str(err.value)
    assert "2 Patroni clusters" in message
    assert "scope alpha (statefulset a, selector cluster-name=alpha)" in message
    assert "scope beta (statefulset b, selector cluster-name=beta)" in message


@pytest.mark.parametrize("pods,said", [
    ([], "none sets PATRONI_SCOPE"),
    ([_pod("x-0", scope=None)], "none sets PATRONI_SCOPE"),
    ([_pod("tsdb-0", env_labels=False)], "PATRONI_KUBERNETES_LABELS: none"),
    ([_pod("tsdb-0", owner=None)], "owning StatefulSet: none"),
    ([_pod("tsdb-0"), _pod("tsdb-1", container="postgres")], "container with PATRONI_SCOPE: postgres, timescaledb"),
])
def test_what_cannot_be_discovered_is_refused(pods, said):
    with pytest.raises(DiscoveryError, match=said):
        resolve_names(PodsKube(pods), "timescale")


@pytest.mark.parametrize("value,labels", [
    ('{"app": "x", "release": "y"}', {"app": "x", "release": "y"}),
    ("{app: x, cluster-name: 'tsdb', release: \"y\"}", {"app": "x", "cluster-name": "tsdb", "release": "y"}),
    ("app=x", {}),
    ("", {}),
])
def test_patronis_labels_are_read_as_json_or_a_yaml_flow_mapping(value, labels):
    assert parse_labels(value) == labels


def test_a_flow_mapping_in_the_pod_spec_gives_the_same_selector():
    pod = _pod("tsdb-0", env_labels="{app: tsdb-timescaledb, cluster-name: tsdb, release: tsdb}")
    assert resolve_names(PodsKube([pod]), "timescale").selector == CHART_SELECTOR


# --- where it shows ---------------------------------------------------------------

class ContextKube:
    def run(self, args, namespaced=True):
        return 0, "kind-x" if args[:2] == ["config", "current-context"] else "", ""

    def json(self, args, namespaced=True):
        raise PreflightError("no")


def test_preflights_cluster_check_says_what_was_discovered():
    names = resolve_names(PodsKube([_pod("tsdb-0")]), "timescale")
    result = check_context(ContextKube(), PreflightOptions("x", discovered=tuple(names.discovered)))
    assert any("discovered      : Patroni scope tsdb" in d for d in result.details)


def test_the_cli_passes_discovered_names_to_preflight(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "Kube", lambda *a: PodsKube([_pod("tsdb-0")]))
    monkeypatch.setattr(cli, "run_preflight", lambda opts: seen.append(opts) or [])
    cli.main(["preflight", "--target-image", "x"])
    assert seen[0].selector == CHART_SELECTOR and seen[0].statefulset == "tsdb"
    assert seen[0].discovered


def test_the_cli_refuses_apply_when_two_clusters_are_found(monkeypatch, tmp_path, capsys):
    pods = [_pod("a-0", scope="alpha", owner="a"), _pod("b-0", scope="beta", owner="b")]
    monkeypatch.setattr(cli, "Kube", lambda *a: PodsKube(pods))
    monkeypatch.setattr(cli, "apply_plan", lambda *a, **k: pytest.fail("apply ran"))
    plan = tmp_path / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    assert cli.main(["apply", str(plan)]) == cli.EXIT_CHECK_FAILED
    assert "2 Patroni clusters" in capsys.readouterr().err


def test_a_cluster_that_cannot_be_listed_keeps_the_defaults_for_the_checks_to_report(monkeypatch):
    seen = []

    class Down:
        def json(self, args, namespaced=True):
            raise PreflightError("connection refused")

    monkeypatch.setattr(cli, "Kube", lambda *a: Down())
    monkeypatch.setattr(cli, "run_preflight", lambda opts: seen.append(opts) or [])
    cli.main(["preflight", "--target-image", "x"])
    assert (seen[0].selector, seen[0].container) == ("application=patroni", "timescaledb")
