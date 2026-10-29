# verify/ — matrix verification runs

A matrix row is verified only by running that migration with this tool, once on
an empty database and once on a seeded one. This directory holds what those
runs need and what they leave behind.

```
fixture/seed.sql    the seeded fixture, creatable at every source version 1.7 .. 2.29
fixture/check.sql   integrity checks, run at the target version after the upgrade
fixture/try.sh      proves the fixture at given versions on one image (not an upgrade)
evidence/           one JSON record per row and mode, committed
out/                full logs, local only (gitignored)
```

## The fixture

`seed.sql` builds, at whatever version is installed:

| Object | Shape |
|---|---|
| `fx_metrics` | hypertable, 1-day chunks, 7 days × 1440 minutes × 5 devices = 50,400 rows |
| `fx_hourly` | continuous aggregate: hourly `avg` and `count` per device (840 rows) |
| compression | `segmentby device`, `orderby time DESC`; the oldest 3 chunks compressed |
| `fx_baseline` | row count, exact `numeric` sum, aggregate rows, compressed chunks, seed version |

The syntax that changed across releases is chosen by probing the installed
extension, never assumed: the 1.x continuous aggregate form (`CREATE VIEW …`,
`REFRESH MATERIALIZED VIEW`, with `max_interval_per_job` raised so one refresh
covers the whole range) versus the 2.x form (`CREATE MATERIALIZED VIEW … WITH
NO DATA`, `refresh_continuous_aggregate`), and `by_range()` (2.13+) versus the
positional `create_hypertable`. One script covers every source version.

Not in it: `time_bucket_ng` aggregates (refused from 2.13; a gate concern),
retention, background policies. No policy is added, so no background job
changes the data between seed and check.

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

The fixture was proven at every source version the matrix needs before any
upgrade was attempted with it: seeded at the newest patch of each series on
that row's own image, then, for 2.x, checked at the same version (1.x can only
be checked after an upgrade, since `check.sql` is 2.x syntax).

```sh
bash verify/fixture/try.sh pg13.22-ts2.15.3 2.1 2.7 2.14
```

## Evidence records

Written by the harness, one per row and mode, at
`evidence/pg<major>/<source_ts>-to-<target_ts>.<empty|seeded>.json`. A re-run
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
