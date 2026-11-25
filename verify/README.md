# verify/ — matrix verification runs

A matrix row is verified only by running that migration with this tool, once on
an empty database and once on a seeded one. This directory holds what those
runs need and what they leave behind.

These runs are not the test suite (`python -m pytest`, no cluster needed). A
run is the end-to-end test of the tool on a real cluster, and its record is an
observation that a matrix verdict is written from. The test suite then holds
each verdict to its records.

```
harness.py          runs matrix rows end to end with plan + apply, writes evidence
fixture/seed.sql    the seeded fixture, creatable at every source version 1.7 .. 2.29
fixture/check.sql   integrity checks, run at the target version after the upgrade
fixture/views.sql   optional layer: a user view on every timescaledb_information view
fixture/try.sh      proves the fixture at given versions on one image (not an upgrade)
evidence/           one JSON record per row and mode, committed
probe_sessions.py   measures open sessions across an extension update (not a row)
probes/sessions/    one JSON record per probe case, committed
out/                full logs, plans and apply reports, local only (gitignored)
```

## The harness

```sh
python verify/harness.py --list                 # what each row resolves to; touches nothing
python verify/harness.py --pg 15 --from 2.13    # one row, empty and seeded
python verify/harness.py                        # all 98 same-PG rows
```

Options: `--mode empty|seeded|both`, `--cluster` (default `tsdb-verify`),
`--replicas` (default 2, so rollouts and session cycling cover a replica),
`--resume` (skip rows and modes whose record still speaks for this checkout:
clean, and no change since to the code that decides a run),
`--history`, `--control` (below).

**Major changes.** `--to-pg 16` runs the rows that change to pg16 instead of
the same-PG ones. The source is staged on an image of its own major, and
`plan` is offered every measured image carrying a major in between, which is
where it finds the `pg_upgrade` images. Their records name the target major
(`2.11-to-pg16-2.30.seeded.json`). `--link` runs them with `pg_upgrade --link`
(records `link-empty` and `link-seeded`). `apply` offers `--link`, so a
verified major change needs those records as well as the copying ones.

```sh
python verify/harness.py --pg 12 --from 2.11 --to-pg 16            # copy
python verify/harness.py --pg 12 --from 2.11 --to-pg 16 --link     # pg_upgrade --link
```

**History.** `--history 2.13.1` stages the source the way a long-lived
database reaches it: created at 2.13.1 (and seeded there), then updated to the
source with one `ALTER EXTENSION`, in a session of its own. Everything after
staging is the same. The records are `history-empty` and `history-seeded`.
Some TimescaleDB bugs exist only for a database with that history (#10272 needs
one created below 2.15), so a row can pass created fresh and fail updated.

**Controls.** `--control direct` (with `--history`) replaces `plan` and
`apply` with a single `ALTER EXTENSION … UPDATE TO '<target>'`. It is a
negative control: it shows what a step the planner inserts protects against,
so its records (`direct-history-empty`, `direct-history-seeded`) are expected
to fail, and they never verify a row. Only a row that runs on one image can be
controlled; anything else needs a rollout, which is `apply`'s job.

```sh
python verify/harness.py --pg 15 --from 2.20 --history 2.13.1                  # with the stop
python verify/harness.py --pg 15 --from 2.20 --history 2.13.1 --control direct # without it
```

**Images and releases.** A row names series (`pg15 2.13 → 2.28`); the harness
makes it exact from `data/image-contents.json`. The target is the newest patch
of the target series, on the image that ships it. The source is the newest
patch of the source series on that same image when it ships one. Otherwise
it is staged on the same-major image that ships it and reaches furthest, and
`plan` is offered every same-major image as a bridge (`--via`). Images are
only used for the PostgreSQL major in their tag. Rows no measured image can
reach (the five `→ 2.30` rows below pg16) are listed as skipped.

**Per row and mode:**

1. `tsupgrade plan --verification-run`, offline. If it refuses, the refusal
   is the row's record (`stage: plan`) and no cluster is touched.
2. The cluster is brought onto the row's first image. If it runs anything
   else, or the previous run changed PostgreSQL major (which leaves
   `pg_upgrade`'s old directory, or a rollback's set-aside one, on the
   disk), its namespace is deleted and recreated with `scripts/up.sh`. Every
   image the plan names is side-loaded into the node first.
3. Database `verify` is dropped, recreated, and the source release installed
   with `CREATE EXTENSION … VERSION` (in a separate session from the `DROP`).
   On a seeded run, `seed.sql` follows. With `--history`, the release
   installed and seeded is the history one, then it is updated to the source.
4. `tsupgrade apply --acknowledge-verification-run --database verify`.
5. `extversion` is read back; on a seeded run `check.sql` runs, even after a
   failed apply, so the record shows what state the data was left in.
6. The record is written. A run passes only if `apply` passed, `extversion` is
   the target, and (seeded) all eight checks print `pass`.

**Isolation.** The row database is the only one with TimescaleDB: after every
cluster reset the harness drops the extension from `postgres` and
`template1`. Preflight inspects every database, so another row's leftover
objects (a non-finalized aggregate from a pre-2.7 seed, say) would otherwise
block an unrelated row. The harness never touches `tsdb-upgrader`, the
long-lived `make up` cluster; it deletes namespaces.

**Pinned images.** Plans pin every image by digest and `apply` rolls out
`image@sha256:...`. In kind a side-loaded image is known only by its tag, so
after loading one the harness also names it by its registry digest (`ctr
images tag`), once `docker image inspect` confirms the local image was pulled
from that digest. The cluster is staged on the pinned reference too, so the
first rollout of a single-image row is still a no-op.

**Routes into the matrix.** `python verify/harness.py --write-routes` copies
each verified row's route from its cited records (empty, seeded, and the
link runs) into `data/upgrade-matrix.json`. A row whose records name no
route, or different ones, keeps none, and `plan` treats it as unestablished
until it is run again.

**Measured times.** `python verify/harness.py --times` prints how long
`apply` and `rollback` took (each record's `timings`): median and range in
seconds per kind of run, then per kind of run and route shape. It reads only
passing, timed records, controls and size runs apart. A third table lists each
row that has size runs, at every size it ran. The README's tables are its
output, and `tests/test_harness.py` fails when they differ.

**Size runs.** `--devices N` seeds N devices of 10,080 rows each instead of
5, seeded only, copy or `--link` (records `scale-seeded-<rows>`,
`scale-link-seeded-<rows>`, which name their `seed`). The checks are the
usual ones, judged against the larger baseline. Seeding and `check.sql` get
ten more minutes per million rows. A size run is evidence for `--times`, never
for a row's verdict.

```sh
python verify/harness.py --pg 15 --to-pg 16 --devices 5000 --link
```

**Fresh evidence.** A verdict has to come from the code that ships. Before a
release is tagged, every row is run again so all records name one commit.
`tests/test_shipped_data.py` flags any cited record from a dirty tree, or one
whose commit has been followed by a change to the code that decides a run (`evidence.VERDICT_CODE`: `plan`, `apply`, `steps`, `majors`,
`drift`, `gates`, `preflight` and the `pg_upgrade.sh` script `apply` runs;
not `rollback`, which runs only after a route has finished). On a development
branch it warns; under `make release-check` and on a tagged checkout it fails.
The check is skipped until the first `v*` tag. `--resume` uses the same rule.

**Registries.** Every run passes `apply --use-cached-images`: the images are
side-loaded, so a registry that does not answer is the network, not the row's
result. The node's image list lags, so the harness first waits until it shows
every image the run needs. `--image-registry` and `--use-cached-images` get their
own evidence with no download and no registry. `--mirror` names the loaded images
on `mirror.invalid`, a host that can never resolve, and runs the whole upgrade
from there (`mirror-empty`, `mirror-seeded`). `--mirror-refused` is the same run
without `--use-cached-images`, and passes only if `apply` stops at its first
preflight, says what can be done and leaves the cluster where it was
(`mirror-refused-empty`, `mirror-refused-seeded`). No row cites these. A real pull
through a real mirror is not among them: a registry that answers is stubbed in
`tests/test_image_sources.py`.

```sh
python verify/harness.py --pg 12 --from 2.11 --to-pg 13 --mirror
python verify/harness.py --pg 12 --from 2.11 --to-pg 13 --mirror-refused
```

**Old continuous aggregates.** Every run also passes `--migrate-caggs`: a route
that reaches 2.25 with the old-format aggregate of a pre-2.7 seed migrates it on
the way, and the fixture checks judge the result. `--caggs-refused` is the same
without the flag. Seeded, it passes only if `apply` stops before changing
anything and names the aggregate (`caggs-refused-seeded`); empty, there is no
aggregate to stop for, and it passes only if the upgrade goes through
(`caggs-refused-empty`). No row cites them.

**Aggregates on `time_bucket_ng`.** `--ng` adds the `ng.sql` layer (below) to
the staged database, at a source from 2.8 to 2.14. No update past 2.14 gets
through those aggregates, so `apply` rebuilds them on `time_bucket` before
anything else, and `ng_check.sql` judges the result (`ng-empty`, `ng-seeded`).
`--ng-refused` is the same without `--migrate-caggs`, and passes only if `apply`
stops before changing anything, names them, shows the form it would rebuild
each on, and leaves them as they were (`ng-refused-empty`, `ng-refused-seeded`).
No row cites them.

```sh
python verify/harness.py --pg 15 --from 2.12 --from 2.14 --ng
python verify/harness.py --pg 15 --from 2.12 --ng-refused
```

**What is not a row result.** If staging or seeding fails, that is the harness
or the environment, not the transition. It writes no record; it prints
`ERROR`, logs it, and moves on. `verify/out/runs.jsonl` has one line per row
and mode for every invocation, errors included.

Records carry the fields `evidence.py` requires, plus:

| Field | Meaning |
|---|---|
| `stage` | where the run stopped: `plan`, `apply`, `verify`, `check`, or `done` |
| `source_pg_version` | the server the source was staged on (differs from `pg_version` on a bridged row) |
| `pods` | each instance afterwards: role, image, `imageID` digest, server version |
| `checks` | the fixture checks, seeded runs only |
| `baseline` | the seed's own summary line |
| `plan`, `apply_exit`, `replicas`, `started_at`, `finished_at` | as named; the run's start and end include the cluster reset, staging and seeding |
| `timings` | UTC start and end, to the second, of `forward` (the `apply` process, or a control's one ALTER) and, on a rollback run, `rollback`; records made before 2026-11-25 have none |
| `route` | the route the plan took, as the matrix row cites it: the start image, then each update and `pg_upgrade`, every image pinned by digest |

A record's `log` and `plan.path` point into `verify/out/`, which is
gitignored: they exist on the machine that ran the row, not in a clone. The
record carries what the verdict rests on (results, checks, versions, images,
route, failure lines verbatim); the log is for whoever is debugging a run.

`images[].digest` is the local repository digest of the image that was loaded
into kind (the one to pin). `pods[].image_id` is what the node reports running:
for a side-loaded image that is kind's `import-<date>` name with the digest of
the imported image, which differs from the repository digest. Both are kept.

## The fixture

`seed.sql` builds, at whatever version is installed:

| Object | Shape |
|---|---|
| `fx_metrics` | hypertable, 1-day chunks, 7 days × 1440 minutes × 5 devices = 50,400 rows |
| `fx_hourly` | continuous aggregate: hourly `avg` and `count` per device (840 rows) |
| compression | `segmentby device`, `orderby time DESC`; the oldest 3 chunks compressed |
| jobs | `fx_job_ok` and `fx_job_fail`, daily, each run once by the scheduler before seeding ends: a success, a failure in `job_stats` and (from 2.9) `job_errors`. From 2.0; 1.x has no `add_job` |
| `fx_baseline` | row count, exact `numeric` sum, aggregate rows, compressed chunks, jobs, seed version |

The syntax that changed across releases is chosen by probing the installed
extension, never assumed: the 1.x continuous aggregate form (`CREATE VIEW …`,
`REFRESH MATERIALIZED VIEW`, with `max_interval_per_job` raised so one refresh
covers the whole range) versus the 2.x form (`CREATE MATERIALIZED VIEW … WITH
NO DATA`, `refresh_continuous_aggregate`), and `by_range()` (2.13+) versus the
positional `create_hypertable`. One script covers every source version.

Not in it: `time_bucket_ng` aggregates (the `ng.sql` layer below), retention,
background policies. No policy is added and the jobs touch no table, so
nothing changes the data between seed and check.

`views.sql` is an optional layer, not part of the default seed: a user view on
every `timescaledb_information` view. Most update scripts drop those views, so
it blocks nearly every row by design. It exists to exercise preflight's
`information_views` gate, in plain-container repros.

`ng.sql` is the layer for `--ng` and `--ng-refused`, on an empty or seeded
database at a source from 2.8 to 2.14: three aggregates on `time_bucket_ng`
(a month and a week in `Europe/Madrid` on `timestamptz`, a day on `timestamp`,
the shape without a catalog row before 2.15), with a refresh policy, a comment,
an owner and a grant, and their rows as baseline tables. No update past 2.14
gets through them. `ng_check.sql`, run with `-v rebuilt=true|false`, checks the
rows (averages within 1e-9: the same rows summed in another order differ in the
last digits), whether they are on `time_bucket` now or still on `time_bucket_ng`,
that the policy, comment, owner, grant and real-time setting survived, and,
after a rebuild, that a new row is materialized by a refresh.

A source below 2.7 creates a *non-finalized* aggregate, because that was the
only form then. The 2.24 → 2.25 update script refuses those, so seeded runs
from old sources are expected to meet that gate. That is TimescaleDB behaviour
for the row's evidence, not a fixture defect.

`check.sql` prints one `check|<name>|pass|fail|<detail>` line per check. A
check that errors prints nothing, and a missing line counts as a failure:

| Check | Asserts |
|---|---|
| `version` | `extversion` is the expected target (with `-v expect=…`) |
| `rows` | count and exact sum equal the baseline, compressed chunks included |
| `still_compressed` | the upgrade decompressed nothing |
| `cagg_survived` | the materialization is intact before any refresh |
| `cagg_matches` | after a full refresh, the aggregate equals the query run fresh |
| `write_and_refresh` | new rows insert, and a windowed refresh picks them up |
| `decompress` | compressed chunks decompress and give back the baseline exactly |
| `jobs` | both jobs are still registered, and their stats still show the success and the failure (`job_errors` is not compared: from 2.15 it reads a table the update rebuilds) |

The three aggregate checks read only what is materialized, since a real-time
aggregate would otherwise compute missing buckets on the fly and hide a loss.
`check.sql` first sets `materialized_only`. An aggregate in the old
(non-finalized) format cannot be altered from 2.14.0 ("Continuous Aggregates
with partials is not supported anymore"), so for one of those the checks read
the view below the aggregate's watermark instead, which is exactly the part a
real-time view takes from the materialization, and say so in their detail
(`old format, cannot be altered: read below the watermark`).

The fixture was proven at every source version the matrix needs before any
upgrade was attempted with it: seeded at the newest patch of each series on
that row's own image, then, for 2.x, checked at the same version (1.x can only
be checked after an upgrade, since `check.sql` is 2.x syntax).

```sh
bash verify/fixture/try.sh pg13.22-ts2.15.3 2.1 2.7 2.14
```

## Evidence records

Written by the harness, one per row and mode, at
`evidence/pg<major>/<source_ts>-to-<target_ts>.<key>.json`, where the key is
`empty`, `seeded`, `link-empty`, `link-seeded`, `history-empty`,
`history-seeded`, a control's `direct-history-empty` /
`direct-history-seeded`, or one of the flag runs' keys named above
(`rollback-*`, `mirror-*`, `caggs-refused-*`, `ng-*`). A re-run
overwrites; git keeps the earlier record. The format is defined and validated
in [`tools/tsupgrade/evidence.py`](../tools/tsupgrade/evidence.py) and kept
loose on purpose — required fields only, anything else passes through:

- the transition (the matrix row's keys) and the mode
- every image used, with its digest
- the exact PostgreSQL version the extension ran on
- the exact source release staged, the target asked for, and the version reached
- the upgrader's commit, and whether the tree was dirty
- pass or fail; on failure, a short plain explanation and the verbatim error lines
- the path to the full log

Records are evidence, not verdicts. A verdict is written into the matrix from
its row's records, and `tests/test_shipped_data.py` checks that each verified
row agrees with them (`test_every_verified_row_agrees_with_its_evidence`).

## Probes

A probe measures a property of TimescaleDB that the tool's behaviour or
messages rest on. It is not a matrix row and writes nothing under `evidence/`.

`probe_sessions.py` measures what sessions do across an extension update, the
two rules preflight prints and the reason `apply` cycles sessions. Per case,
on the same disposable kind cluster as the harness (primary and replica):

1. `verify` is seeded at the source release, and one session on each pod
   loads the library and waits;
2. the update runs from a fresh `psql -X` session on the primary, and the
   replica replays it;
3. each waiting session runs the same statements twice; the library it has
   mapped is read from `/proc/<pid>/maps` before the update, after it, and
   after the statements; fresh sessions run the statements once;
4. `verify` is staged at the source again, and the update is run in a
   session that has already loaded the library.

```bash
python verify/probe_sessions.py --list
python verify/probe_sessions.py --case pg15-2.20.3-2.28.3
```

What the four cases (2.5.2 → 2.11.2 on PostgreSQL 12, 2.20.3 → 2.28.3 on 15,
2.25.2 → 2.30.1 and 2.30.0 → 2.30.1 on 17) showed, every time:

- a session open during the update keeps the old library mapped, on the
  replica as on the primary, and the server ends it at its next statement,
  whatever the statement, with `FATAL: "timescaledb" already loaded with a
  different version`. It fails once and is gone; it does not keep failing;
- a fresh session works on every instance;
- an update in a session that has loaded the library is refused with
  `cannot be updated after the old version has already been loaded`, and the
  version stays where it was.

Not measured: a session inside an open transaction at the time of the update,
and what a connection pool does with the ended sessions.
`tests/test_session_probes.py` fails if a record stops showing the above.
