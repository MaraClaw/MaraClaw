# Chat-turn database stress test

This directory preserves the 2026-11-12 chat-turn database stress test and contains the current database-only stress runner. The runner exercises intake, native dispatch claim and settlement, history reads, the dispatch reconciler and the agent gateway listing in disposable PostgreSQL. Today's after workload models organization-member intake; the before workload remains the historical control. The recorded before/after timings are evidence from the original workload, not a baseline for current intake throughput.

| Path | Purpose |
|---|---|
| [`results-2026-11-12.md`](results-2026-11-12.md) | The recorded result: findings, fixes, before and after numbers, costs and open follow-ups |
| [`summary-2026-11-12.md`](summary-2026-11-12.md) | One-page summary of that result, with the test hardware |
| `run-stress.sh` | Measures the historical control and current workload, checks the fixed plans and exits non-zero when a check fails |
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
3. Compare the plan shapes and buffer counts with [`results-2026-11-12.md`](results-2026-11-12.md) only at comparable scale. Do not use the mixed-workload timings or TPS to reproduce the archived speedup: the after intake now does different work. Deadlocks in the before arm are timing-dependent; zero after-fix SQL failures is still checked.

Each script starts its own container with `--rm` and stops it on exit, Ctrl-C included, which also removes its volume. No other container is touched, and a container name already in use stops the script instead of being reused.

## What `run-stress.sh` does

1. Starts `maraclaw-stress` with durability off (`fsync`, `full_page_writes` and `synchronous_commit` off, `wal_level=minimal`), copies `container/` and the working tree's `scripts/schema_baseline.sql` into it, and runs `container/stress.sh` there.
2. Loads the baseline and drops the four indexes the fixes added. That is the pre-fix schema: the fixes changed nothing else in the baseline.
3. Seeds 2,000 agents across 20 tenants, each with an owner, a colleague, 10 chat sessions and the reply bindings of the two canonical ones. Sessions 1 and 6 are the owner's and the colleague's canonical web conversations (`web`, primary, not a group, as `chat_session_dao.insert_primary_platform` creates them), 2, 3, 7 and 8 are IM group chats, and 4, 5, 9 and 10 are agent-to-agent sessions. Then it adds 5,000 completed turns, 5,000 chat messages and 5,000 history revisions per agent (10 million rows per table), written in time order across agents the way production traffic arrives. In the steady state each agent has two pending turns on each of lanes 1 to 5, and a delivered turn with one queued behind it on lane 6: 22,000 pending and 2,000 delivered rows.
4. Measures **before**. It records EXPLAIN probes in the steady state and again with 81 more pending turns per agent (184,000 pending). Then it runs a 60-second pgbench workload with the historical intake (session-first lock order and per-user quota update) and the pre-fix pending-page SQL, and restores the seeded queue and quota counters.
5. Reruns the baseline over the seeded database, as `python -m app.scripts.bootstrap_db` does on an existing one. Its `CREATE INDEX IF NOT EXISTS` statements build the four indexes; the report records how long that took and the index sizes.
6. Measures **after** with the fixed plans and current organization-member intake SQL. It prints the report and copies `report.txt`, `plans.txt` (the full `EXPLAIN (ANALYZE, BUFFERS)` text of every probe) and the pgbench output and logs to `STRESS_OUT_DIR`, or to a new temporary directory. Keep these outputs out of `docs/`.

Options: `--quick` (200 agents), `--keep` (leave the container running), `--help`. Environment overrides:

| Variable | Default | Meaning |
|---|---|---|
| `STRESS_AGENTS` | 2000 (200 with `--quick`) | Agents, each with its own owner |
| `STRESS_HISTORY` | 5000 | Completed turns, chat messages and history revisions per agent |
| `STRESS_BACKLOG` | 81 | Extra pending turns per agent in the backlog phase |
| `STRESS_CLIENTS`, `STRESS_THREADS` | 64, 8 | pgbench clients and threads |
| `STRESS_SECONDS` | 60 | pgbench duration per variant |
| `STRESS_DAILY_MESSAGE_LIMIT` | 500 | Positive per-agent daily cap for the after fixture. The default is a snapshot of the seeded Ultra tier's cap; check `app/records/organization_plan.py` for current policy. |
| `STRESS_CONTAINER` | `maraclaw-stress` | Container name |
| `STRESS_IMAGE` | `postgres:18.6-alpine` | PostgreSQL image |
| `STRESS_OUT_DIR` | a new temporary directory | Where the report, plans and pgbench output go |
| `STRESS_MIN_FREE_GB` | 20 (10 with `--quick`) | Minimum free space on the Docker host disk |

### Workloads

The pgbench mix approximates the database work of each step. The intake arms also differ in which quota they charge:

| Script | Share | Mirrors |
|---|---|---|
| `bench-intake-before.sql`, `bench-intake-after.sql` | 40% | **Before:** historical session-first lock, reservation and user-counter update. **After:** agent `FOR KEY SHARE` then session `FOR UPDATE`, submission lookup/reservation, stored workspace gate, daily agent charge, session touch and pending gateway insert. |
| `bench-claim-settle.sql` | 40% | The claim of one lane head, then, when the claim wins, `settle_native_report` and `record_report`. Lanes 1 and 6 (canonical web sessions) take the human-lane path of `openclaw_shared_dispatch.claim_shared_turn`: shared tenant, owner and agent locks, then `lock_human_lane` on the session, which settlement takes again. The other lanes take `openclaw_native_claims.claim_ordinary_turn` (shared tenant and owner locks, exclusive agent lock, `claim_ordinary_head` with `bind_openclaw_reply`) |
| `bench-history.sql` | 20% | `chat_message_dao.list_recent`, 50 rows |
| `bench-reconcile-before.sql`, `bench-reconcile-after.sql` | one more client, once a second | One `openclaw_dispatch_reconciler` tick: the first `pending_dispatch_page` and the first `delivered_dispatch_page` |

In the after arm, the sender is a member of the ordinary agent's seeded Ultra tenant, so no per-user message quota is charged. New work checks `agent_workspace_usage.over_since`; no measurement or no overage marker allows it through. The atomic upsert charges `agent_daily_message_usage` once per accepted turn, keyed by agent with a local calendar date. It uses the genesis org admin's timezone (`Asia/Ho_Chi_Minh` in the seed), not the sender's or tenant row's timezone; missing or empty timezone uses UTC. A different date restarts the count at one. Workspace or daily-cap refusal rolls back the reserved message and leaves the session and queue untouched. An exact replay commits without writes or quota checks; a conflicting fingerprint or missing post-conflict reservation rolls back. Random submission IDs make replays rare in the default mix.

This is a database-only simulation of the seeded use case, not a full submission or a production TPS test. It reads the tenant tier but takes its numeric cap from the fixture input; it does not run the Python plan resolver, invalid-timezone handling, workspace measurement, authorization, eligibility and expiry checks, intake history reconstruction, title generation, routing, Redis, HTTP/WebSockets or the guest. The seed assumes existing writable sessions and valid IANA timezone names, and uses a fixed synthetic payload/fingerprint.

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

In the after arm pgbench counts a replay or a deliberate quota rollback as a completed SQL iteration, not an accepted turn. Check **after committed intake reservations** under **Scale and timings** for the count of accepted simulated submissions. A small cap or a long run can turn intake into mostly refusals. The archived workload instead updated the user counter with an effectively unreachable cap in both arms; do not compare intake or mixed-workload TPS with that record or attribute a difference between today's arms solely to the original fixes.

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
| delivered dispatch pages walk `ix_gateway_messages_delivered_order` without a sort | All four after-fix custom and generic first delivered pages (both phases) have an index condition on that index and no `Sort` node |
| every fixed probe reads fewer buffers after the fix | All fourteen first, mid, generic mid, delivered, history and gateway-list pairs do |
| no deadlocks or other failed transactions after the fix | Both after-fix pgbench processes exited successfully and their logs recorded no SQL failures |
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
| Pending dispatch walk | `tests/test_gateway_lane_reads_postgres.py::test_pending_order_index_starts_the_dispatch_walk_at_its_cursor`: a generic plan of the page SQL needs an index condition on `ix_gateway_messages_pending_order` and no `Sort`. `test_pending_page_past_the_last_head_scans_a_bounded_stretch`: past the last lane head, both generic plans of the page read the same 100 rows and buffers with 300 or 3,000 rows queued behind it |
| Delivered dispatch walk | `tests/test_gateway_lane_reads_postgres.py::test_delivered_page_starts_at_its_cursor_as_reply_bindings_grow`: the generic plan of the page starts with an index condition on `ix_gateway_messages_delivered_order` and reads the same buffers with 500 or 5,000 reply bindings |
| Agent gateway listing | `tests/test_gateway_lane_reads_postgres.py::test_agent_listing_index_serves_newest_first_without_a_sort` |
| Recent chat history | `tests/test_chat_history_postgres.py::test_agent_conversation_index_serves_recent_history_in_order` |

To see a test catch a regression, revert its fix in a scratch checkout and run that checkout's copy of the script. Without the `FOR KEY SHARE` statement in `ChatSessionDAO.lock_for_agent`, both scope-lock cases fail with `deadlock detected`, as they did before the fix. Without one of the four indexes in `scripts/schema_baseline.sql`, the plan test that names it fails.

The CI `lifecycle-postgres` job runs the scope-lock and lane-read files; `test_chat_history_postgres.py` is not in its list.

## Keeping the scripts current

The `-after` workloads, `bench-claim-settle.sql`, `bench-history.sql` and the probes in `explain.sql` follow the SQL in `app/dao/chat_dao.py`, `gateway_message_dao.py` and `gateway_message_delivery_dao.py`, and the lock sequences in `app/services/chat_submissions.py`, `openclaw_native_claims.py` and `openclaw_gateway_delivery.py`. For intake quota handling also check `quota_guard.py`, `agent_workspace_quota.py`, `agent_message_quota.py`, `user_dao.py` (genesis lookup) and `agent_daily_message_usage_dao.py` (atomic charge). Keep the `-before` SQL and recorded results as historical evidence. `stress.sh` and `report.sql` name the four fix indexes, so renaming one in the baseline means renaming it there too.
