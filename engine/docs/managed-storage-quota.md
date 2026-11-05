# Managed S3 storage quota

Hard per-agent byte quota on files persisted through the managed S3 backend. The numbers live only in the plan catalog (`app/services/organization_plans.py`, enums in `app/records/organization_plan.py`; see `organization-pricing.md`): `storage_limit_bytes` per agent and `max_file_size_bytes` per file, read as `plan_for(tier_for_tenant(agent.tenant_id))` at every reservation.

## Scope

- Counted: the logical bytes of the **current** objects under `<agent UUID>/…` in the managed S3 bucket. Not counted: local materialized copies, historical object versions, external provider writes, shared tenant keys (`enterprise_info_<tenant UUID>/…`) and anything a guest writes directly into its bind-mounted workspace. There is no XFS/FUSE/rlimit enforcement; direct guest writes are explicitly out of scope.
- **Exclusive ownership.** Everything under an agent's UUID prefix must be created, replaced and deleted through `get_storage_backend()` (`storage_runtime/facade.py`), whose S3 primary is `ManagedS3StorageBackend` (`quota_s3.py`). The configured bucket and base prefix are stable for this deployment. External writers, lifecycle expiration, replication and manual changes must not mutate the managed namespace. Analytics exports use a separate non-agent prefix. This is a fresh-deployment ledger, with no migration/backfill for preexisting objects.
- Only `STORAGE_BACKEND=s3` is metered. The local backend keeps its behavior. With `STORAGE_LOCAL_FALLBACK_ENABLED` the S3 primary stays the metered one, so read-through promotions are charged and can be refused.
- Agents whose organization has no paid tier (legacy, unlinked, system) have no file or total ceiling. Their managed S3 objects are recorded so assigning a paid destination can check current usage. Non-agent keys remain unmetered. A key whose first segment is a UUID spelled any other way fails closed (`ManagedKeyError`).
- Existing upload and parser ceilings are unchanged; a tier file ceiling does not make every route accept that size.

## Ledger (`agent_storage_usage`, `agent_storage_objects`)

No foreign keys, so accounting outlives the agent, user and tenant rows. The usage row is the per-agent serialization point (`SELECT … FOR UPDATE`, `lock_timeout` 5 s) and `charged_bytes` always equals the sum of each object's `GREATEST(committed_bytes, reserved_bytes)`. Reservations commit on a connection of the **durable pool** (`app/db/pool.py` `durable_pool`, `session.durable_transaction`), never on the request's connection: the request transaction is neither committed early nor needed to be durable, and a saturated request pool cannot starve a holder that is waiting for it.

## Protocol (`quota_ledger.py`, `quota_s3.py`)

1. **Reserve** (one transaction under the usage lock): resolve the agent's current plan, refuse `size > max_file_size_bytes` (413) or growth past `storage_limit_bytes` (507), then charge `max(current, size)` and commit. Overwrites charge only the growth; shrinking and same-size rewrites never double-charge; an agent already over its allowance may shrink but not grow. A pending row blocks the key.
2. **Provider call**, only after the reservation committed. Reserve, call and settle run inside one shielded scope, so cancellation cannot strand a write the provider may have applied.
3. **Settle**: `LANDED` commits the new size (a delete drops the row and refunds); `REJECTED` restores the previous bytes; `UNKNOWN` keeps the full charge. `REJECTED` means the provider cannot have applied the request: a 4xx, a connection that never opened, an unsupported capability, an exhausted 409 conflict race. Automatic SDK retries are disabled, including environment overrides; only definitive 409 create conflicts retry. A create that loses to an existing object preserves its winner and adopts the provider's size. Everything else (5xx, timeouts, resets, cancellation, unexpected errors) is `UNKNOWN`.
4. **Pending keys stay blocked.** Every mutation of an unresolved key raises 503 `storage_accounting_pending`. No HEAD result, ETag match, listing result, age or restart can fence a provider request that might arrive later. Both writes and deletes retain their conservative charge indefinitely. Version-token checks run inside the committed key claim, so another managed mutation cannot interleave between check and provider call.
5. A ledger failure for an agent that might be paid (database, pool, lock timeout, ambiguous tier link) raises 503 `storage_accounting_unavailable` before the provider is called.

## Deletes

`delete` refunds only after the provider answered; a refused or failed delete leaves the charge. Even an absent key gets a committed delete claim so a concurrent create cannot race the provider call. `delete_tree` follows `ContinuationToken` through every page, sends claimed keys to `delete_objects`, and refunds only keys the response lists under `Deleted`. Keys under `Errors`, keys held by any unresolved operation, and keys missing from the response stay charged and make the call raise `StorageDeleteIncompleteError` after the listing is exhausted. A ledger check also detects pending writes not yet visible in S3. Deleting the empty prefix is refused so no tree delete can bypass accounting.

## Company transfer

`quota_transfer.require_destination_storage`, called by `company_transfer_disposition.apply_disposition` for paid transfers before any migration write, locks the moving agents' usage rows inside the transfer transaction and refuses with 409 `transfer_destination_storage_exceeded` when charged bytes (reservations included) or the largest known object exceed a tiered destination. A writer that reserved earlier is therefore counted; a writer that arrives later waits for the transfer to finish and then reserves against the destination tier.

## HTTP contract (`app/main.py`)

`StorageQuotaError` subclasses carry `status_code` and a safe `detail` of `code`, `message` and byte counts (never keys, provider text or credentials): 413 `storage_file_too_large`, 507 `storage_quota_exceeded`, 503 `storage_accounting_unavailable` / `storage_accounting_pending` / `storage_delete_incomplete`, 400 `storage_key_invalid`.

## Open decision

An uncertain operation, including a worker dying between reservation and call, keeps its charge and blocks its key indefinitely. Retiring such rows would require a separate provider-fencing or namespace-retirement contract; neither an operator's guess nor a time horizon proves that the old request cannot arrive later. No automatic retirement or reconciliation is supplied. Rows are indexed through `ix_agent_storage_objects_pending`.

## Tests

Live PostgreSQL (`MARACLAW_TEST_POSTGRES_DSN`, plus `BOOTSTRAP_TEST_DATABASE_URL` for the transfer module) with a loopback S3 wire server (`tests/s3_wire_server.py`, real botocore requests, scripted faults): `test_managed_storage_{quota,ambiguity,tree,transfer,api}_postgres.py`, `test_company_transfer_storage_postgres.py`; offline `test_storage_quota_outcomes.py`. Fixture-only scaled limits keep total-byte boundaries cheap; the catalog's real per-tier numbers are proven separately.
