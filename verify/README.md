# verify/ — matrix verification runs

A matrix row is verified only by running that migration with this tool, once on
an empty database and once on a seeded one. This directory holds what those
runs need and what they leave behind.

```
harness.py          runs matrix rows end to end with plan + apply, writes evidence
fixture/seed.sql    the seeded fixture, creatable at every source version 1.7 .. 2.29
fixture/check.sql   integrity checks, run at the target version after the upgrade
fixture/views.sql   optional layer: a user view on every timescaledb_information view
fixture/try.sh      proves the fixture at given versions on one image (not an upgrade)
evidence/           one JSON record per row and mode, committed
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
`--resume` (skip rows and modes that already have a record from a clean tree),
`--history`, `--control` (below).

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
   else, its namespace is deleted and recreated with `scripts/up.sh`. Every
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
| `plan`, `apply_exit`, `replicas`, `started_at`, `finished_at` | as named |

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

Not in it: `time_bucket_ng` aggregates (refused from 2.13; a gate concern),
retention, background policies. No policy is added and the jobs touch no
table, so nothing changes the data between seed and check.

`views.sql` is an optional layer, not part of the default seed: a user view on
every `timescaledb_information` view. Most update scripts drop those views, so
it blocks nearly every row by design. It exists to exercise preflight's
`information_views` gate, in plain-container repros.

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
`empty`, `seeded`, `history-empty`, `history-seeded`, or a control's
`direct-history-empty` / `direct-history-seeded`. A re-run
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

Records are evidence, not verdicts. Nothing maps them onto the matrix's verdict
enum yet; that is decided after the runs show which failures actually occur.
