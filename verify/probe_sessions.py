"""Measure what an open session does after ALTER EXTENSION timescaledb UPDATE.

The claim under test (UPGRADE-RESEARCH.md, trap 6, graded D/C): sessions open
before the update keep the old shared object loaded and keep failing until
they are cycled, on replicas too, since the catalog change arrives by WAL.
``apply`` cycles sessions after every update on the strength of it, and the
plan and runbook state it as fact.

For each case: a cluster on one image, primary and replica; ``verify`` seeded
at the source release; one session on each pod that loads the library and
then waits; the update run from a fresh session on the primary; the replica
caught up; then each waiting session runs the same statements twice, and fresh
sessions run them once. The library each old session has mapped is read from
``/proc/<pid>/maps`` before the update, after it, and after the statements.

Then trap 5 (graded D): the database is staged at the source again and the
update is run in a session that has already loaded the library.

Writes one JSON record per case under ``verify/probes/sessions/``. This is a
probe of TimescaleDB, not a matrix row: it never touches ``verify/evidence/``.

    python verify/probe_sessions.py --list
    python verify/probe_sessions.py --case pg15-2.20.3-2.28.3
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
from harness import CONTAINER, DATABASE, Cluster, HarnessError, Log, _now, pin, stage  # noqa: E402

REPO = harness.REPO
PROBES = REPO / "verify" / "probes" / "sessions"
OUT = harness.OUT / "probes"
APP = "probe-old"

# (name, image, from, to). Spread over the loader's history: an old series
# that crosses six minors, a recent long jump, a recent short one, and a patch.
CASES = (
    ("pg12-2.5.2-2.11.2", "timescale/timescaledb-ha:pg12.22-ts2.11.2", "2.5.2", "2.11.2"),
    ("pg15-2.20.3-2.28.3", "timescale/timescaledb-ha:pg15.18-ts2.28.3", "2.20.3", "2.28.3"),
    ("pg17-2.25.2-2.30.1", "timescale/timescaledb-ha:pg17.11-ts2.30.1", "2.25.2", "2.30.1"),
    ("pg17-2.30.0-2.30.1", "timescale/timescaledb-ha:pg17.11-ts2.30.1", "2.30.0", "2.30.1"),
)

# What the old sessions run to load the library before the update.
WARM = (
    ("pid", "SELECT pg_backend_pid();"),
    ("extversion", "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';"),
    ("hypertable", "SELECT count(*) FROM fx_metrics;"),
    ("time_bucket", "SELECT time_bucket(INTERVAL '1 hour', TIMESTAMPTZ '2026-01-01 00:30+00');"),
    ("cagg", "SELECT count(*) FROM fx_hourly;"),
)


def statements(primary: bool, round_: int) -> List[Tuple[str, str]]:
    """What every session runs after the update; writes only on the primary."""
    out = [
        ("pid", "SELECT pg_backend_pid();"),
        ("extversion", "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';"),
        ("hypertable", "SELECT count(*) FROM fx_metrics;"),
        ("time_bucket", "SELECT time_bucket(INTERVAL '1 hour', TIMESTAMPTZ '2026-01-01 00:30+00');"),
        ("cagg", "SELECT count(*) FROM fx_hourly;"),
        ("information", "SELECT count(*) FROM timescaledb_information.hypertables;"),
    ]
    if primary:
        out.append(("insert", "INSERT INTO fx_metrics VALUES "
                    "(TIMESTAMPTZ '2026-02-01 00:00+00' + INTERVAL '{0} minute', 1, 1.0);".format(round_)))
    return [("r{0}.{1}".format(round_, name), sql) for name, sql in out]


def script(token: str, primary: bool) -> str:
    """The old session: warm up, signal, wait for go, run twice, signal, wait."""
    lines = []
    for name, sql in WARM:
        lines += ["\\echo @@ warm.{0}".format(name), sql]
    lines += [
        "\\! touch /tmp/{0}.ready".format(token),
        "\\! n=0; while [ ! -f /tmp/{0}.go ] && [ $n -lt 1800 ]; do sleep 0.2; n=$((n+1)); done".format(token),
    ]
    for round_ in (1, 2):
        for name, sql in statements(primary, round_):
            lines += ["\\echo @@ {0}".format(name), sql]
    lines += [
        "\\echo @@ end",
        "\\! touch /tmp/{0}.done".format(token),
        "\\! n=0; while [ ! -f /tmp/{0}.exit ] && [ $n -lt 1800 ]; do sleep 0.2; n=$((n+1)); done".format(token),
    ]
    return "\n".join(lines) + "\n"


def split(output: str) -> List[Dict[str, str]]:
    """``@@ name`` markers into steps; text before the first marker is kept."""
    steps: List[Dict[str, str]] = []
    current = {"step": "(start)", "output": []}  # type: Dict[str, Any]
    for line in output.splitlines():
        if line.startswith("@@ "):
            steps.append({"step": current["step"], "output": "\n".join(current["output"]).strip()})
            current = {"step": line[3:].strip(), "output": []}
        else:
            current["output"].append(line)
    steps.append({"step": current["step"], "output": "\n".join(current["output"]).strip()})
    return [s for s in steps if s["step"] != "(start)" or s["output"]]


class Probe:
    def __init__(self, cluster: Cluster, log: Log) -> None:
        self.cluster = cluster
        self.log = log

    def exec(self, pod: str, *argv: str, input: Optional[str] = None,
             timeout: float = 120) -> Tuple[int, str, str]:
        return self.cluster.kubectl("exec", "-i", pod, "-c", CONTAINER, "--", *argv,
                                    input=input, timeout=timeout, log=self.log)

    def sql(self, pod: str, query: str, database: str = DATABASE) -> Tuple[int, str, str]:
        return self.exec(pod, "psql", "-U", "postgres", "-X", "-qtA", "-d", database, "-c", query)

    def maps(self, pod: str) -> Dict[str, Any]:
        rc, out, err = self.sql(pod, "SELECT pid FROM pg_stat_activity WHERE application_name = '{0}'".format(APP))
        pids = out.split() if rc == 0 else []
        if len(pids) != 1:
            return {"pid": None, "libraries": None, "note": "sessions named {0}: {1}".format(APP, pids or err.strip())}
        rc, out, err = self.exec(pod, "sh", "-c",
                                 "grep -o '[^ /]*timescaledb[^ /]*\\.so' /proc/{0}/maps | sort -u".format(pids[0]))
        return {"pid": int(pids[0]), "libraries": out.split() if rc in (0, 1) else None,
                "note": err.strip() or None}

    def wait_file(self, pod: str, path: str, timeout: float = 300) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.exec(pod, "test", "-f", path)[0] == 0:
                return True
            time.sleep(1)
        return False

    def caught_up(self, primary: str, replica: str, version: str, timeout: float = 300) -> bool:
        rc, lsn, _ = self.sql(primary, "SELECT pg_current_wal_lsn()")
        deadline = time.monotonic() + timeout
        while rc == 0 and time.monotonic() < deadline:
            rc2, ok, _ = self.sql(replica, "SELECT pg_last_wal_replay_lsn() >= '{0}'::pg_lsn AND "
                                  "(SELECT extversion FROM pg_extension WHERE extname = 'timescaledb') = '{1}'"
                                  .format(lsn.strip(), version))
            if rc2 == 0 and ok.strip() == "t":
                return True
            time.sleep(1)
        return False

    def fresh(self, pod: str, primary: bool) -> Dict[str, Any]:
        lines = []
        for name, sql in statements(primary, 1):
            lines += ["\\echo @@ {0}".format(name.replace("r1.", "")), sql]
        rc, out, err = self.exec(pod, "sh", "-c", "psql -U postgres -X -At -d {0} 2>&1".format(DATABASE),
                                 input="\n".join(lines) + "\n")
        return {"exit": rc, "steps": split(out + err)}


def run_case(name: str, image: str, source: str, target: str, cluster_name: str,
             replicas: int) -> Path:
    contents = harness.load_contents()
    reference = pin(image, contents)
    log = Log(OUT / (name + ".log"))
    cluster = Cluster(cluster_name, replicas, contents)
    cluster.ensure(reference, log)
    cluster.used = True
    stage(cluster, SimpleNamespace(source=source, history=None, ng=False), "seeded", log)

    primary = cluster.leader()
    pods = [p["metadata"]["name"] for p in cluster.pods()]
    replica = next(p for p in pods if p != primary)
    probe = Probe(cluster, log)
    if not probe.caught_up(primary, replica, source):
        raise HarnessError("the replica {0} did not replay the seeded database".format(replica))
    token = "probe-{0}".format(int(time.time()))
    for pod in (primary, replica):
        probe.exec(pod, "sh", "-c", "rm -f /tmp/probe-*")

    log.section("old sessions")
    procs = {}
    for pod, is_primary in ((primary, True), (replica, False)):
        argv = ["kubectl", "--context", cluster.context, "-n", harness.NAMESPACE, "exec", "-i", pod,
                "-c", CONTAINER, "--", "sh", "-c",
                "PGAPPNAME={0} psql -U postgres -X -At -d {1} 2>&1".format(APP, DATABASE)]
        # Bytes, not text: on Windows a text pipe writes \r\n, and psql then
        # runs `\! touch /tmp/x.ready\r`, a different file.
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        proc.stdin.write(script(token, is_primary).encode("utf-8"))
        proc.stdin.close()
        procs[pod] = proc
    for pod in procs:
        if not probe.wait_file(pod, "/tmp/{0}.ready".format(token)):
            raise HarnessError("the old session on {0} never finished warming up".format(pod))
    before = {pod: probe.maps(pod) for pod in procs}

    log.section("update {0} -> {1}".format(source, target))
    rc, out, err = cluster.psql(DATABASE, "-q", sql="ALTER EXTENSION timescaledb UPDATE TO '{0}'".format(target),
                                log=log)
    alter = {"exit": rc, "output": (out + err).strip()}
    replayed = probe.caught_up(primary, replica, target) if rc == 0 else False
    after_update = {pod: probe.maps(pod) for pod in procs}

    log.section("go")
    for pod in procs:
        probe.exec(pod, "touch", "/tmp/{0}.go".format(token))
    after_statements = {}
    for pod, proc in procs.items():
        # A session the server ended exits psql early; the done file never comes.
        deadline = time.monotonic() + 300
        while proc.poll() is None and time.monotonic() < deadline:
            if probe.exec(pod, "test", "-f", "/tmp/{0}.done".format(token))[0] == 0:
                break
            time.sleep(1)
        after_statements[pod] = probe.maps(pod)
        probe.exec(pod, "touch", "/tmp/{0}.exit".format(token))
    sessions = {}
    for pod, proc in procs.items():
        try:
            raw, _ = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            raw, _ = proc.communicate()
        output = raw.decode("utf-8", "replace").replace("\r\n", "\n")
        log.section("old session on " + pod)
        log.write(output)
        sessions[pod] = {
            "role": "primary" if pod == primary else "replica",
            "exit": proc.returncode,
            "libraries_before_update": before[pod],
            "libraries_after_update": after_update[pod],
            "libraries_after_statements": after_statements[pod],
            "steps": split(output),
        }

    log.section("fresh sessions")
    fresh = {pod: dict(probe.fresh(pod, pod == primary), role="primary" if pod == primary else "replica")
             for pod in (primary, replica)}

    # Trap 5: the update run in a session that has already loaded the library.
    log.section("same-session update")
    stage(cluster, SimpleNamespace(source=source, history=None, ng=False), "seeded", log)
    primary = cluster.leader() or primary
    lines = ["\\echo @@ load", "SELECT count(*) FROM fx_metrics;",
             "\\echo @@ update", "ALTER EXTENSION timescaledb UPDATE TO '{0}';".format(target),
             "\\echo @@ extversion", "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';"]
    rc, out, _ = probe.exec(primary, "sh", "-c", "psql -U postgres -X -At -d {0} 2>&1".format(DATABASE),
                            input="\n".join(lines) + "\n")
    rc2, ext, _ = probe.sql(primary, "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
    same_session = {"exit": rc, "steps": split(out),
                    "extversion_after": ext.strip() if rc2 == 0 else None}

    rc, version, _ = cluster.psql("postgres", "-qtA", sql="SHOW server_version", log=log)
    record = {
        "probe": "old-sessions-after-extension-update",
        "case": name,
        "image": reference,
        "server_version": version.strip() if rc == 0 else None,
        "from": source,
        "to": target,
        "replicas": replicas,
        "seeded": True,
        "upgrader": harness.upgrader_revision(),
        "recorded_at": _now(),
        "update": alter,
        "replica_caught_up": replayed,
        "old_sessions": sessions,
        "fresh_sessions": fresh,
        "same_session_update": same_session,
    }
    PROBES.mkdir(parents=True, exist_ok=True)
    path = PROBES / (name + ".json")
    path.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8", newline="\n")
    return path


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--case", action="append", default=[], help="case name; repeatable; default all")
    ap.add_argument("--cluster", default="tsdb-verify", help="kind cluster name")
    ap.add_argument("--replicas", type=int, default=2, help="pods, primary included")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)
    chosen = [c for c in CASES if not args.case or c[0] in args.case]
    if args.list or not chosen:
        for c in CASES:
            print("  ".join(c))
        return 0 if chosen or args.list else 2
    failed = 0
    for name, image, source, target in chosen:
        print("{0}: {1} {2} -> {3}".format(name, image, source, target), flush=True)
        try:
            print("  wrote", run_case(name, image, source, target, args.cluster, args.replicas), flush=True)
        except HarnessError as exc:
            failed += 1
            print("  could not run:", exc, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
