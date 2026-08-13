# app/core/logging

Process-wide logging service. Application modules import `logger` from here, never from `loguru`.

## Public API

- `from app.core.logging import logger` for drop-in `debug` / `info` / `warning` / `error` / `exception` / `opt` / `bind`.
- `get_logger(__name__)` when the module name should be bound without a stack walk for the name.
- `configure_logging()` runs on package import and again in `app.main` lifespan (passes `Settings.LOG_*`). It also installs the stdlib intercept.
- `get_trace_id` / `set_trace_id` / `new_trace_id` for request and background-task correlation.
- Background jobs that are not inside an HTTP request should call `new_trace_id()` before emitting related logs.

## Performance

- Level is checked before message formatting and caller capture.
- A bounded queue (`LOG_QUEUE_SIZE`, default 8192) plus a daemon writer thread keep stdout I/O off the event loop. Overflow is dropped and counted.
- Missing trace ids render as `-`. Do not allocate a UUID per line.
- Exception tracebacks are rendered on the writer thread.
- This package must not import `app.config` (circular: settings → sandbox config → logger).

## Environment

- `LOG_LEVEL` (default `DEBUG` when `DEBUG=true`, otherwise `INFO`)
- `LOG_FORMAT` = `text` | `json`
- `LOG_QUEUE_SIZE`
- `LOG_COLOR` (default: TTY detection)

## Compatibility

- `app.core.logging_config` re-exports this package for older imports.
- Bundled skill-creator payloads under `app/services/skill_creator_files/` may still import `loguru`; do not "fix" those.
- Do not add new `logging.getLogger(...)` call sites in app code.
