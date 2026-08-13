# app/services/clawsec_skill_files

This directory is not a normal backend service package. It is the vendored
[ClawSec](https://github.com/prompt-security/clawsec) OpenClaw security skill
catalog used to protect MaraClaw-managed OpenClaw agents.

## License

Upstream ClawSec is **AGPL-3.0**. See `LICENSE.AGPL-3.0` and `NOTICE` in this
directory. Do not strip license notices when refreshing the package tree.

## File Convention

- Each skill subdirectory is a full multi-file ClawSec package (`SKILL.md`,
  `skill.json`, scripts, hooks, advisories, etc.).
- Subdirectory names are canonical skill folder names used by
  `clawsec_runtime.clawsec_skill_folder_names()` and seeded into the `skills`
  table via `seed_clawsec_skills()`.
- `manifest.json` records the upstream commit, the full vendored skill list, and
  which packages are default-installed (`default_skills`) vs catalog-only.
- `.upstream-commit` pins the Git SHA used for the last sync.

## What Is Vendored

OpenClaw protection subset only:

- `clawsec-suite` (signed advisory feed + guarded installs)
- `soul-guardian` (workspace file drift / integrity)
- `openclaw-audit-watchdog` (scheduled OpenClaw audits)
- `clawsec-scanner` (dependency / SAST / static-hook scanning)
- `clawsec-clawhub-checker` (ClawHub reputation gate)
- `clawtributor` (catalog-only community reporting)

Excluded on purpose: NanoClaw / Hermes / Picoclaw packages, `claw-release`
author tooling, and `*-traffic-guardian` specification-only packages.

## Editing Rules

- Do not rewrite skill payloads for backend style cleanup.
- Do not add `AGENTS.md` under skill subdirectories (`hooks/`, `scripts/`, `lib/`). This file is the catalog root.
- Prefer refreshing from upstream over hand-editing scripts or advisories.
- Keep tests under upstream `test/` out of this tree; runtime packages only.
- If adding or removing a skill folder, update `manifest.json` and
  `tests/test_clawsec_runtime.py`.

## Seeding

- Controlled by `CLAWSEC_SKILLS_ENABLED` (default `true`).
- `seed_clawsec_skills()` runs at startup after `seed_skills()` and before
  `push_default_skills_to_existing_agents()`.
- Default packages are marked `is_default=True` so new agents receive them and
  existing agents get missing packages on the next default-skills sync.
