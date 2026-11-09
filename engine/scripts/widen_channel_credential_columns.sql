-- Run explicitly on an existing installation before writing authenticated
-- channel credentials. Fresh installations use schema_baseline.sql.
-- Widening preserves every existing value; it does not authenticate legacy data.
ALTER TABLE channel_configs
    ALTER COLUMN app_secret TYPE TEXT,
    ALTER COLUMN encrypt_key TYPE TEXT,
    ALTER COLUMN verification_token TYPE TEXT;
