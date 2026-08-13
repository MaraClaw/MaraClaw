# ClawSec OpenClaw Skills

MaraClaw vendors a protection-focused subset of
[Prompt Security ClawSec](https://github.com/prompt-security/clawsec) so local
OpenClaw agents can run signed advisory checks, drift detection, audits, and
install gates without depending on a live ClawHub install at bootstrap time.

## What is bootstrapped

Vendored packages live under `app/services/clawsec_skill_files/`:

| Package | Role | Default on agents |
| --- | --- | --- |
| `clawsec-suite` | Signed advisory feed, guarded installs, setup scripts | yes |
| `soul-guardian` | Workspace file drift / integrity | yes |
| `openclaw-audit-watchdog` | Scheduled OpenClaw security audits | yes |
| `clawsec-scanner` | Dependency / SAST / static-hook scanning | yes |
| `clawsec-clawhub-checker` | ClawHub reputation gate for installs | yes |
| `clawtributor` | Community incident reporting | catalog only |

Excluded on purpose: NanoClaw / Hermes / Picoclaw packages, `claw-release`
author tooling, and `*-traffic-guardian` specification-only packages.

## License

ClawSec is **AGPL-3.0**. See:

- `app/services/clawsec_skill_files/LICENSE.AGPL-3.0`
- `app/services/clawsec_skill_files/NOTICE`
- upstream commit pin in `.upstream-commit` / `manifest.json`

## Runtime wiring

- Env flag: `CLAWSEC_SKILLS_ENABLED` (default `true` in settings / `.env.example`)
- Seeder: `app/services/clawsec_runtime.seed_clawsec_skills()`
- Startup order in `app/main.py`:
  1. `seed_skills()`
  2. `seed_gogcli_skill()`
  3. `seed_clawsec_skills()`
  4. `push_default_skills_to_existing_agents()`

Default packages are stored as builtin `Skill` rows with `is_default=True`, so:

- new agents receive them during agent creation
- existing agents missing those folders get them on the next default-skills sync

## Operator notes

- After deploy, restart the backend so startup seeding refreshes the skill
  registry and pushes missing default skill trees into agent workspaces.
- Agents still need Node/Python tooling mentioned by each skill for full
  runtime behavior (for example `node` for suite scripts, `python3` for
  soul-guardian).
- Suite hook activation (`scripts/setup_advisory_hook.mjs`) remains an
  explicit, reviewable agent/operator step, matching upstream ClawSec guidance.
