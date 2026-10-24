# Member timezone preference

`users.timezone` is a nullable member-profile preference, not a global identity or
company setting. `GET /api/auth/me` returns `timezone: string | null`.
`PATCH /api/auth/me` accepts IANA timezone keys such as `UTC`, `Etc/GMT-7`, and
`Asia/Shanghai`. Validation uses `app.services.schedule_time.validate_timezone`
(Python `ZoneInfo`); empty and unknown keys return HTTP 422 identifying `timezone`.

Omitting `timezone` from a PATCH preserves the saved preference. Sending
`{"timezone": null}` clears it. `null` means unset; clients use UTC as their
fallback. The backend does not replace it with a company or server timezone.
This preference does not change existing schedules or schedule runtime behavior.
User writes keep the existing session-version invalidation; new snapshots include
the preference, while older snapshots without the key load it as unset.

## Existing deployment prerequisite

`scripts/schema_baseline.sql` creates the nullable `timezone VARCHAR(100)` column
for fresh databases only. Re-running bootstrap does **not** upgrade an existing
`users` table. Before deploying this code against an existing database, an operator
must arrange the additive column change through the deployment's normal reviewed
database-change process:

```sql
ALTER TABLE users ADD COLUMN timezone VARCHAR(100);
```

Leave the column nullable with no default or backfill, so existing members remain
unset. This note is a manual prerequisite, not an automatic migration or bootstrap
patch; no live database change is performed by this feature.
