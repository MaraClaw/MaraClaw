# app/services/skill_creator_files

This directory is not a normal backend service package. It is a bundled skill-creator toolkit with flattened generated paths.

## File Convention

- Flattened names such as `scripts__run_eval.py`, `agents__grader.md`, and `eval-viewer__viewer.html` represent nested upstream paths.
- Imports may reference generated layout names such as `scripts.run_eval` even though files are flattened here.
- Do not "fix" flattened imports or filenames unless changing the generation/packaging model intentionally.
- `app/services/skill_creator_content.py` maps flattened filenames back to logical paths; additions may require updating that map.
- `pyproject.toml` excludes this directory from `ty`; that is intentional because these files are bundled/generated skill payloads, not normal backend service modules.

## Script Roles

- Packaging: `scripts__package_skill.py`, `scripts__quick_validate.py`, `scripts__utils.py`.
- Eval/optimization: `scripts__run_eval.py`, `scripts__run_loop.py`, `scripts__improve_description.py`.
- Reports/benchmarking: `scripts__generate_report.py`, `scripts__aggregate_benchmark.py`, `eval-viewer__generate_review.py`.
- Prompts/schemas/examples: `agents__*.md`, `references__schemas.md`, `content_research_writer__SKILL.md`.

## Side Effects

- Scripts can invoke external tools/models such as `claude -p`, Anthropic APIs, browser opening, process pools, and a local HTTP review server.
- Generated outputs can include `.skill`, `results.json`, `report.html`, live temp HTML reports, benchmark files, logs, feedback data, and embedded output files.
- Packaging intentionally excludes root-level `evals/`; do not treat missing evals in `.skill` output as a bug.
- Do not type-clean or Ruff-clean this tree just to satisfy backend checks; preserve CLI arguments, output schemas, viewer placeholders, temp `.claude/commands`, `CLAUDECODE` env handling, and process cleanup behavior.

## Tests

- There is no focused test coverage for these scripts today.
- If adding tests, isolate CLI behavior with temp directories and mocks. Do not require live model/tool execution.
