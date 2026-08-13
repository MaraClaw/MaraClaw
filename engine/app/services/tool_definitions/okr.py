"""OKR builtin tool definitions."""

# ── OKR Tools ────────────────────────────────────────────────────────────────
# These three tools are global builtins available to ALL agents.
# OKR Agent-exclusive management tools (create_objective, create_key_result, etc.)
# are injected separately via agent_seeder when the OKR Agent is created.

OKR_BUILTIN_TOOLS = [
    {
        "name": "get_okr",
        "display_name": "Get OKR",
        "description": (
            "Read the full OKR board for the current period: company-level Objectives and "
            "Key Results, plus every member's (human and agent) individual O and KRs with "
            "current progress values. Includes objective_id for each Objective and kr_id for "
            "each Key Result. Use this to understand company direction, update existing OKRs, "
            "and see how others are tracking before planning your own work."
        ),
        "category": "okr",
        "icon": "🎯",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "period_start": {
                    "type": "string",
                    "description": "Optional ISO date (YYYY-MM-DD). Defaults to the current period start.",
                },
                "period_end": {
                    "type": "string",
                    "description": "Optional ISO date (YYYY-MM-DD). Defaults to the current period end.",
                },
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "get_my_okr",
        "display_name": "Get My OKR",
        "description": (
            "Read your own Objectives and Key Results for the current period, including "
            "kr_id values needed to update progress. Call this before update_kr_progress "
            "to get the correct kr_id."
        ),
        "category": "okr",
        "icon": "🎯",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {},
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "update_kr_progress",
        "display_name": "Update KR Progress",
        "description": (
            "Update the current progress value of one of YOUR OWN Key Results. "
            "Call get_my_okr first to obtain the kr_id. "
            "A progress log entry is created automatically for history tracking."
        ),
        "category": "okr",
        "icon": "📈",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "kr_id": {
                    "type": "string",
                    "description": "UUID of the Key Result to update (from get_my_okr).",
                },
                "value": {
                    "type": "number",
                    "description": "New current value (e.g. 3500 for a follower count, 75 for a percentage).",
                },
                "note": {
                    "type": "string",
                    "description": "Optional note explaining the progress update.",
                },
            },
            "required": ["kr_id", "value"],
        },
        "config": {},
        "config_schema": {},
    },
]
