# app/services/linkup_skill_files

Vendored official [Linkup](https://github.com/LinkupPlatform/skills) OpenClaw
skills. Agents use this workspace knowledge to call the Linkup APIs directly;
there is no engine-side Linkup function-calling tool.

## File Convention

- `linkup-search/`, `linkup-fetch/`, `linkup-research/`, and `linkup-extract/`
  are the official skill packages (`SKILL.md` + `references/`).
- Folder names are the canonical skill keys used by `linkup_runtime` and
  generated `openclaw.json` `skills.entries`.
- `manifest.json` records the upstream repo/commit and default-install list.
- `.upstream-commit` pins the Git SHA used for the last sync.

## Editing Rules

- Prefer refreshing from upstream over rewriting skill prose.
- Do not add an engine Python search handler for Linkup.

## Seeding

- `seed_linkup_skills()` runs at startup after `seed_skills()` and before
  `push_default_skills_to_existing_agents()`.
- All four packages are marked `is_default=True` so new and existing agents
  receive them.
