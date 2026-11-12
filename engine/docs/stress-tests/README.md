# Chat-turn database stress test

This directory records the 2026-11-12 stress test of the database work behind a chat turn and holds the scripts that reproduce it. The test drives the SQL a turn runs (intake, native dispatch claim and settlement, history reads, the dispatch reconciler and the agent gateway listing) against a production-sized dataset in a disposable PostgreSQL container, and compares the schema and SQL from before the fixes with the current ones.

| Path | Purpose |
|---|---|
| [`results-2026-11-12.md`](results-2026-11-12.md) | The recorded result: findings, fixes, before and after numbers, costs and open follow-ups |
| `run-stress.sh` | Reproduces the measurement, checks the fixed plans and exits non-zero when a check fails |
| `run-regression-tests.sh` | Runs the PostgreSQL tests that pin each fix |
| `lib.sh` | Disk guard and disposable-container helpers shared by both scripts |
| `container/` | The in-container driver (`stress.sh`), seed, pgbench workloads, EXPLAIN probes and report |

## Requirements

- Docker with a running daemon. `psql`, `pgbench` and `vacuumdb` come from the `postgres:18.6-alpine` image, so the host needs no PostgreSQL tools.
- Free space on the disk behind Docker: 20 GB for the recorded scale, 10 GB for `--quick` and the regression tests. The scripts refuse to start below that, and a watchdog stops the stress container if free space falls below 5 GB. A run at the recorded scale takes about 10 GB while it lasts, and Docker Desktop hands the space back once the container is gone. On macOS, Docker Desktop's VM disk grows on the host's data volume, and the VM goes read-only when that volume fills.
- `uv` for `run-regression-tests.sh`, as for any engine test run.
- Bash 3.2 or later; macOS's `/bin/bash` works.

The recorded run used a Docker VM with 10 CPUs and 8 GB of memory. Timings scale with the machine; the checks do not depend on speed.

## Verify the result

Run from `engine/`:

```bash
docs/stress-tests/run-regression-tests.sh   # the tests that pin each fix, against a disposable PostgreSQL
docs/stress-tests/run-stress.sh --quick     # 200 agents and 1M rows per history table, about 3 minutes
docs/stress-tests/run-stress.sh             # the recorded scale: 2,000 agents and 10M rows per table, about 12 minutes
```

The result is verified when:

1. `run-regression-tests.sh` exits 0 with no skipped tests.
2. `run-stress.sh` exits 0, and every check in its report reads `PASS` (the last check is informational).
3. Compared with [`results-2026-11-12.md`](results-2026-11-12.md), the plan shapes and buffer counts come out close to the recorded ones. After the fix there are no failed transactions, throughput is higher, and history reads and reconciler ticks are faster. Milliseconds, tps, the number of deadlocks before the fix, and intake and claim latency vary with the machine and between runs; [Reading the report](#reading-the-report) explains why.

Each script starts its own container with `--rm` and stops it on exit, Ctrl-C included, which also removes its volume. No other container is touched, and a container name already in use stops the script instead of being reused.

## What `run-stress.sh` does

1. Starts `maraclaw-stress` with durability off (`fsync`, `full_page_writes` and `synchronous_commit` off, `wal_level=minimal`), copies `container/` and the working tree's `scripts/schema_baseline.sql` into it, and runs `container/stress.sh` there.
2. Loads the baseline and drops the three indexes the fixes added. That is the pre-fix schema: the fixes changed nothing else in the baseline.
3. Seeds 2,000 agents across 20 tenants, each with its own owner, 10 web chat sessions and their reply bindings. Then it adds 5,000 completed turns, 5,000 chat messages and 5,000 history revisions per agent (10 million rows per table), written in time order across agents the way production traffic arrives. In the steady state each agent has two pending turns on each of lanes 1 to 5, and a delivered turn with one queued behind it on lane 6: 22,000 pending and 2,000 delivered rows.
4. Measures **before**. It records EXPLAIN probes in the steady state and again with 81 more pending turns per agent (184,000 pending). Then it runs a 60-second pgbench workload with the pre-fix intake lock order and the pre-fix pending-page SQL, and restores the steady state.
5. Reruns the baseline over the seeded database, as `python -m app.scripts.bootstrap_db` does on an existing one. Its `CREATE INDEX IF NOT EXISTS` statements build the three indexes; the report records how long that took and the index sizes.
6. Measures **after** the same way with the current SQL. It prints the report and copies `report.txt`, `plans.txt` (the full `EXPLAIN (ANALYZE, BUFFERS)` text of every probe) and the pgbench output and logs to `STRESS_OUT_DIR`, or to a new temporary directory. Keep these outputs out of `docs/`.

Options: `--quick` (200 agents), `--keep` (leave the container running), `--help`. Environment overrides:

| Variable | Default | Meaning |
|---|---|---|
| `STRESS_AGENTS` | 2000 (200 with `--quick`) | Agents, each with its own owner |
| `STRESS_HISTORY` | 5000 | Completed turns, chat messages and history revisions per agent |
| `STRESS_BACKLOG` | 81 | Extra pending turns per agent in the backlog phase |
| `STRESS_CLIENTS`, `STRESS_THREADS` | 64, 8 | pgbench clients and threads |
| `STRESS_SECONDS` | 60 | pgbench duration per variant |
| `STRESS_CONTAINER` | `maraclaw-stress` | Container name |
| `STRESS_IMAGE` | `postgres:18.6-alpine` | PostgreSQL image |
| `STRESS_OUT_DIR` | a new temporary directory | Where the report, plans and pgbench output go |
| `STRESS_MIN_FREE_GB` | 20 (10 with `--quick`) | Minimum free space on the Docker host disk |

### Workloads

The pgbench mix mirrors the SQL each step runs. Before and after differ only where the fixes changed the code:

| Script | Share | Mirrors |
|---|---|---|
| `bench-intake-before.sql`, `bench-intake-after.sql` | 40% | `chat_submissions.accept_submission`: `chat_session_dao.lock_for_agent` (after the fix it takes the agent row `FOR KEY SHARE` before the session `FOR UPDATE`), `find_submission`, `reserve_submission`, `user_dao.charge_message_quota`, the session update and the pending gateway insert |
| `bench-claim-settle.sql` | 40% | `openclaw_native_claims.claim_ordinary_turn` (shared tenant and owner locks, exclusive agent lock, `claim_ordinary_head` with `bind_openclaw_reply`), then, when the claim wins, `settle_native_report` and `record_report` |
| `bench-history.sql` | 20% | `chat_message_dao.list_recent`, 50 rows |
| `bench-reconcile-before.sql`, `bench-reconcile-after.sql` | one more client, once a second | One `openclaw_dispatch_reconciler` tick: the first `pending_dispatch_page` and the first `delivered_dispatch_page` |

The model covers database work only. Authorization and eligibility reads, Redis, HTTP and WebSocket handling, the guest `docker exec` and Python overhead are left out.

## Reading the report

**Query plans** lists each probe's time and buffers (shared hit plus read) before and after the fix, and its plan shape: the indexes it used, plus `Seq Scan` or `Sort` when present. The `Seq Scan` in every pending-page plan reads `personal_burst_batches`, which this dataset leaves empty.

| Probe | What it measures |
|---|---|
| `pending-page-1` | The first page of a reconciler cycle |
| `pending-page-mid` | A page that starts at the middle lane head, so heads remain ahead of it, as in most ticks |
| `pending-page-tail` | The page after the last lane head. Every pending row behind that head fails the head test, so this page walks them all. The fix does not speed it up; it now runs once per cycle instead of on every tick. |
| `pending-page-mid-generic` | The mid page as a generic plan, which a psycopg server-side prepared statement can get from its fifth execution |
| `pending-page-mid-generic-old-sql` | After the fix only: the pre-fix SQL on the fixed schema. In a generic plan its cursor cannot become an index condition, which is why the DAO change ships with the index. |
| `history` | `chat_message_dao.list_recent` for one 500-message conversation |
| `gateway-list` | `gateway_message_dao.list_recent`, behind `GET /api/agents/{agent_id}/gateway-messages` |

**Mixed workload** gives completed transactions, deadlocks, other failures and p50/p95/p99/max latency in milliseconds per workload. **pgbench runs** gives throughput and the `pg_stat_database` deadlock count per variant. Four effects matter when reading them:

- Before the fix, a deadlocked transaction waits `deadlock_timeout` (1 s) before PostgreSQL aborts one side, and clients that pick the same agent meanwhile queue behind it. At `--quick` scale (64 clients over 200 agents) these convoys have queued about 30 clients on one agent row and held transactions for up to 63 s. Stalled clients take load off the rest, so the before-fix p50 can come out lower than the after-fix one: compare throughput, failures, p99 and max instead.
- pgbench runs a closed loop: each client starts its next transaction when the last one ends. Before the fix, slow history reads and deadlock stalls tie clients up. After it, those clients move to intakes and claims, where row-lock contention rises, so intake and claim latency can go up while throughput doubles. The reproduction run in the results shows this.
- pgbench lets in-flight transactions finish after the run's duration. A before-fix run can overrun while a convoy drains, and its tps covers the whole elapsed time.
- The reconciler tick is timed from its scheduled start, so it includes any wait behind the previous tick.

**Checks** turns the result into pass or fail:

| Check | Passes when |
|---|---|
| pending dispatch pages walk `ix_gateway_messages_pending_order` without a sort | All six after-fix first, mid and generic mid pages (both phases) have an index condition on that index and no `Sort` node |
| recent chat history walks `ix_chat_messages_agent_conv_created` without a sort | Both after-fix history probes do |
| agent gateway listing walks `ix_gateway_messages_agent_created` without a scan or sort | Both after-fix gateway-list probes do, with no `Seq Scan` and no `Sort` |
| every fixed probe reads fewer buffers after the fix | All ten first, mid, generic mid, history and gateway-list pairs do |
| no deadlocks or other failed transactions after the fix | Every after-fix pgbench transaction completed |
| deadlocks reproduced before the fix | Informational. Deadlocks depend on timing, so a run can see none; the scope-lock test below reproduces the deadlock deterministically |

### Inspecting a run

With `--keep` the container stays up after the report:

```bash
docker exec -it maraclaw-stress psql -U stress
```

`stress_plan` holds each probe's plan as JSON (`plan`) and as text (`plan_text`), `stress_tx` the raw pgbench log lines, and `stress_meta`, `stress_run` and `stress_check` the rest of the report. The report reads the views `stress_plan_summary` and `stress_tx_parsed`. Remove the container with `docker stop maraclaw-stress`.

## Regression tests

`run-regression-tests.sh` starts `maraclaw-stress-tests` on a free localhost port, points the DSN variables of the CI `lifecycle-postgres` job at it and runs four test files; each fixture creates and drops its own schema. It fails when pytest fails, and also when any test is skipped, because a skipped PostgreSQL test never reached the database.

| Fix | Test |
|---|---|
| Intake locks the agent before the session | `tests/test_chat_submission_scope_locks_postgres.py::test_submission_locks_the_agent_before_the_session_native_work_writes` (claim and settle): a submission pauses after its session lock while a claim or settlement takes the agent lock, then both must finish |
| The intake expiry check no longer escalates to tenant and agent locks | `tests/test_agent_stop_enforcement.py::test_unmarked_expiry_check_refuses_without_the_locking_write` (no database) |
| Pending dispatch walk | `tests/test_gateway_lane_reads_postgres.py::test_pending_order_index_starts_the_dispatch_walk_at_its_cursor`: a generic plan of the page SQL needs an index condition on `ix_gateway_messages_pending_order` and no `Sort` |
| Agent gateway listing | `tests/test_gateway_lane_reads_postgres.py::test_agent_listing_index_serves_newest_first_without_a_sort` |
| Recent chat history | `tests/test_chat_history_postgres.py::test_agent_conversation_index_serves_recent_history_in_order` |

To see a test catch a regression, revert its fix in a scratch checkout and run that checkout's copy of the script. Without the `FOR KEY SHARE` statement in `ChatSessionDAO.lock_for_agent`, both scope-lock cases fail with `deadlock detected`, as they did before the fix. Without one of the three indexes in `scripts/schema_baseline.sql`, the plan test that names it fails.

The CI `lifecycle-postgres` job runs the scope-lock and lane-read files; `test_chat_history_postgres.py` is not in its list.

## Keeping the scripts current

The `-after` workloads, `bench-claim-settle.sql`, `bench-history.sql` and the probes in `explain.sql` copy SQL from `app/dao/chat_dao.py`, `gateway_message_dao.py`, `gateway_message_delivery_dao.py` and `user_dao.py`, and the lock sequences of `app/services/chat_submissions.py`, `openclaw_native_claims.py` and `openclaw_gateway_delivery.py`. Update them when that SQL or those lock orders change. The `-before` workloads record the pre-fix code and stay as they are. `stress.sh` and `report.sql` name the three fix indexes, so renaming one in the baseline means renaming it there too.
