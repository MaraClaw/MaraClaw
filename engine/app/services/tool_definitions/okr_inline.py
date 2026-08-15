"""Seed rows extracted from builtin.py. Data only."""


from typing import Any

OKR_INLINE_TOOLS: list[dict[str, Any]] = [
        {
            "name": "get_okr",
            "display_name": "Get OKR Board",
            "description": (
                "Get the full OKR board for the current period. Returns all Objectives and Key Results "
                + "for the tenant, organized by company and member level. Includes objective_id values "
                + "for every Objective and kr_id values for every Key Result, so you can update existing "
                + "Objectives and KRs instead of creating duplicates. Used by the OKR Agent to generate "
                + "progress reports and monitor team performance."
            ),
            "category": "okr",
            "icon": "🎯",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "period_start": {
                        "type": "string",
                        "description": "Optional: ISO date string (YYYY-MM-DD) to filter by period start. Defaults to current period.",
                    },
                    "period_end": {
                        "type": "string",
                        "description": "Optional: ISO date string (YYYY-MM-DD) to filter by period end.",
                    },
                },
            },
            "config": {},
            "config_schema": {},
        },
        {
            "name": "get_my_okr",
            "display_name": "My OKR",
            "description": (
                "Get your own OKR Objectives and Key Results for the current period. "
                + "Returns a structured view of your goals, current progress values, plus objective_id and kr_id references "
                + "you need to update existing OKRs correctly. Call this before changing progress, KR content, "
                + "or Objective text so you reuse the current records instead of creating duplicates."
            ),
            "category": "okr",
            "icon": "🎯",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "period_start": {
                        "type": "string",
                        "description": "Optional: ISO date string (YYYY-MM-DD). Defaults to current period.",
                    },
                    "period_end": {
                        "type": "string",
                        "description": "Optional: ISO date string (YYYY-MM-DD).",
                    },
                },
            },
            "config": {},
            "config_schema": {},
        },
        {
            "name": "update_kr_progress",
            "display_name": "Update KR Progress",
            "description": (
                "Update the current progress value for a Key Result. Use get_my_okr first to obtain "
                + "the kr_id. The status (on_track / at_risk / behind / completed) is automatically "
                + "computed from the progress ratio, or you can override it explicitly. "
                + "A progress log entry is recorded for full audit history."
            ),
            "category": "okr",
            "icon": "📈",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "kr_id": {
                        "type": "string",
                        "description": "UUID of the Key Result to update. Get this from get_my_okr.",
                    },
                    "value": {
                        "type": "number",
                        "description": "New current value (e.g. 4.2 for a KR with target 5.0).",
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional note explaining the progress update (e.g. 'Completed weekly review session').",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["on_track", "at_risk", "behind", "completed"],
                        "description": "Optional: override the auto-computed status.",
                    },
                },
                "required": ["kr_id", "value"],
            },
            "config": {},
            "config_schema": {},
        },
        {
            "name": "update_kr_content",
            "display_name": "Update KR Content",
            "description": (
                "Update the content fields of one of YOUR OWN Key Results, such as title, target value, unit, "
                + "focus reference, or status. Use get_my_okr first to obtain the kr_id. "
                + "This tool is for changing KR definition/content, not reporting progress. "
                + "If the user says to change, revise, adjust, or replace an existing KR target or wording, "
                + "prefer this tool instead of create_key_result."
            ),
            "category": "okr",
            "icon": "✏️",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "kr_id": {
                        "type": "string",
                        "description": "UUID of the Key Result to update (from get_my_okr).",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional new KR title.",
                    },
                    "target_value": {
                        "type": "number",
                        "description": "Optional new target value.",
                    },
                    "unit": {
                        "type": "string",
                        "description": "Optional new unit label.",
                    },
                    "focus_ref": {
                        "type": "string",
                        "description": "Optional new focus file reference.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["on_track", "at_risk", "behind", "completed"],
                        "description": "Optional explicit status override.",
                    },
                },
                "required": ["kr_id"],
            },
            "config": {},
            "config_schema": {},
        },
        {
            # collect_okr_progress - legacy OKR Agent heartbeat collection path.
            # This replaces the need to contact each member individually.
            "name": "collect_okr_progress",
            "display_name": "Collect OKR Progress",
            "description": (
                "Legacy batch sync for reported KR progress. Prefer direct OKR tools such as "
                + "get_my_okr and update_kr_progress for new work. Returns a summary of how many "
                + "KRs were updated."
            ),
            "category": "okr",
            "icon": "📊",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
            "config": {"okr_agent_only": True},
            "config_schema": {},
        },
        {
            # generate_okr_report - OKR Agent calls this to produce the structured report.
            # The tool writes the report to WorkReport table and returns the markdown content
            # so the Agent can choose to post it to Plaza or send it to specific channels.
            "name": "generate_okr_report",
            "display_name": "Generate OKR Report",
            "description": (
                "Generate a structured OKR progress report (daily or weekly) for the current "
                + "period. The report summarizes all Objectives and Key Results, highlights items "
                + "at risk or behind, and shows overall team health metrics. The report is saved "
                + "to the database and to your workspace/reports/ folder. Returns the full report "
                + "markdown so you can post it to Plaza or share with the team."
            ),
            "category": "okr",
            "icon": "📋",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "report_type": {
                        "type": "string",
                        "enum": ["daily", "weekly"],
                        "description": "Whether to generate a daily or weekly report.",
                    },
                },
                "required": ["report_type"],
            },
            "config": {"okr_agent_only": True},
            "config_schema": {},
        },
        {
            # get_okr_settings - lets OKR Agent read the tenant's OKR configuration so it
            # can determine whether reports are due, what time they're scheduled, etc.
            "name": "get_okr_settings",
            "display_name": "Get OKR Settings",
            "description": (
                "Read the OKR configuration for this team, including whether daily/weekly "
                + "reports are enabled, the configured report time, period frequency, and more. "
                + "Use this at the start of your heartbeat to decide whether a report is due today."
            ),
            "category": "okr",
            "icon": "⚙️",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
            "config": {"okr_agent_only": True},
            "config_schema": {},
        },
        {
            # create_objective - OKR Agent uses this after conversation-based confirmation
            # to create an O for the company, a user, or an agent. Only OKR Agent has this tool.
            "name": "create_objective",
            "display_name": "Create Objective",
            "description": (
                "Create an OKR Objective for the company, a specific user, or a specific agent. "
                + "Call this after confirming the objective with the relevant person through conversation. "
                + "Use this only when a new Objective needs to be created for the period. "
                + "If the person already has a matching Objective and just wants to revise it, use update_objective instead. "
                + "owner_type must be 'company', 'user', or 'agent'. "
                + "owner_id is not required for company-level objectives. "
                + "period_start and period_end must be ISO date strings (YYYY-MM-DD)."
            ),
            "category": "okr",
            "icon": "🎯",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The objective title (concise, inspiring, directional).",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional detailed description of the objective.",
                    },
                    "owner_type": {
                        "type": "string",
                        "enum": ["company", "user", "agent"],
                        "description": "Who this objective belongs to.",
                    },
                    "owner_id": {
                        "type": "string",
                        "description": "UUID of the owner. Try to use this if available in context.",
                    },
                    "owner_name": {
                        "type": "string",
                        "description": "Optional fallback: the exact display name of the human/agent. Use this ONLY if you don't have their UUID.",
                    },
                    "period_start": {
                        "type": "string",
                        "description": "ISO date string for the start of the OKR period (e.g. '2026-04-01').",
                    },
                    "period_end": {
                        "type": "string",
                        "description": "ISO date string for the end of the OKR period (e.g. '2026-06-30').",
                    },
                },
                "required": ["title", "owner_type", "period_start", "period_end"],
            },
            "config": {"okr_agent_only": True},
            "config_schema": {},
        },
        {
            # create_key_result - OKR Agent creates a measurable KR under a confirmed objective.
            "name": "create_key_result",
            "display_name": "Create Key Result",
            "description": (
                "Create a Key Result (KR) under an existing Objective. "
                + "Get the objective_id first using get_okr. "
                + "Use this only for a brand-new KR. If the user is revising the wording, target value, unit, "
                + "or focus reference of an existing KR, use update_kr_content instead. "
                + "target_value is the goal number (e.g. 50000 for 50000 followers). "
                + "unit is optional but recommended for clarity (e.g. '%', 'NPS', '万元', 'followers')."
            ),
            "category": "okr",
            "icon": "🔑",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "objective_id": {
                        "type": "string",
                        "description": "UUID of the parent Objective.",
                    },
                    "title": {
                        "type": "string",
                        "description": "The KR title (specific, measurable outcome).",
                    },
                    "target_value": {
                        "type": "number",
                        "description": "The target number to achieve (e.g. 50000).",
                    },
                    "unit": {
                        "type": "string",
                        "description": "Optional unit label (e.g. '%', 'followers', '万元', 'NPS score').",
                    },
                    "focus_ref": {
                        "type": "string",
                        "description": "Optional: basename of the focus file that tracks this KR (e.g. 'content_quality').",
                    },
                },
                "required": ["objective_id", "title", "target_value"],
            },
            "config": {"okr_agent_only": True},
            "config_schema": {},
        },
        {
            # update_objective - available to ALL agents, but with ownership enforcement:
            # regular agents can only modify their own O; OKR Agent can modify any O.
            "name": "update_objective",
            "display_name": "Update Objective",
            "description": (
                "Modify an Objective's title, description, status, or period dates. "
                + "Regular agents can only update their own Objectives - call get_my_okr first "
                + "to get your objective_id. The OKR Agent can update any member's Objective. "
                + "Only provide the fields you want to change. If the request is to revise an existing OKR's "
                + "goal text rather than create a new one, prefer this tool over create_objective."
            ),
            "category": "okr",
            "icon": "✏️",
            "is_default": True,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "objective_id": {
                        "type": "string",
                        "description": "UUID of the Objective to update. Get from get_my_okr (own) or get_okr (any).",
                    },
                    "title": {
                        "type": "string",
                        "description": "New title for the objective.",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["draft", "active", "completed", "archived"],
                        "description": "New status for the objective.",
                    },
                    "period_start": {
                        "type": "string",
                        "description": "New period start date (YYYY-MM-DD).",
                    },
                    "period_end": {
                        "type": "string",
                        "description": "New period end date (YYYY-MM-DD).",
                    },
                },
                "required": ["objective_id"],
            },
            "config": {},
            "config_schema": {},
        },
        {
            # update_any_kr_progress - OKR Agent exclusive: update KR for any member.
            # Unlike update_kr_progress (self-report), this can update anyone's KR.
            # Used after collecting progress data through conversation.
            "name": "update_any_kr_progress",
            "display_name": "Update Any KR Progress",
            "description": (
                "Update the progress value of any team member's Key Result. "
                + "This is the OKR Agent's exclusive version of update_kr_progress - it can update "
                + "KRs belonging to any user or agent, not just the caller's own. "
                + "Use this ONLY after confirming the value with the KR owner through conversation. "
                + "Get kr_id from get_okr. Optionally provide a note explaining the source."
            ),
            "category": "okr",
            "icon": "📈",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "kr_id": {
                        "type": "string",
                        "description": "UUID of the Key Result to update. Get from get_okr.",
                    },
                    "value": {
                        "type": "number",
                        "description": "New current value for this KR.",
                    },
                    "note": {
                        "type": "string",
                        "description": "Source or context note (e.g. 'Reported by user in weekly check-in').",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["on_track", "at_risk", "behind", "completed"],
                        "description": "Optional: override the auto-computed status.",
                    },
                },
                "required": ["kr_id", "value"],
            },
            "config": {"okr_agent_only": True},
            "config_schema": {},
        },
        {
            # generate_monthly_okr_report - OKR Agent exclusive: produce the monthly summary report.
            # Called automatically by the monthly_okr_report system cron trigger, or on-demand.
            "name": "generate_monthly_okr_report",
            "display_name": "Generate Monthly OKR Report",
            "description": (
                "Generate the monthly OKR progress summary report. Covers all Objectives and Key "
                + "Results for the current period, highlights completed and at-risk items, and provides "
                + "a closing action note. Saved to WorkReport (report_type='monthly') and "
                + "workspace/reports/. Returns the full Markdown so you can send it to admins."
            ),
            "category": "okr",
            "icon": "📅",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
            "config": {"okr_agent_only": True},
            "config_schema": {},
        },
        {
            # upsert_member_daily_report - OKR Agent exclusive: create or revise a member daily report.
            "name": "upsert_member_daily_report",
            "display_name": "Upsert Member Daily Report",
            "description": (
                "Create or update the final normalized daily report for any member in the company. "
                + "Use this after discussing progress with the member and distilling their update into "
                + "one concise final report. The stored content should stay within 2000 characters."
            ),
            "category": "okr",
            "icon": "📝",
            "is_default": False,
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "report_date": {
                        "type": "string",
                        "description": "Report date in YYYY-MM-DD format.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Final concise daily report content. Keep it within 2000 characters.",
                    },
                    "member_type": {
                        "type": "string",
                        "enum": ["user", "agent"],
                        "description": "Member type. Defaults to user if omitted.",
                    },
                    "member_id": {
                        "type": "string",
                        "description": "UUID of the member. Preferred when available.",
                    },
                    "member_name": {
                        "type": "string",
                        "description": "Member display name. Use when you do not have the UUID.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Optional source tag such as okr_agent_assisted or manual.",
                    },
                },
                "required": ["report_date", "content"],
            },
            "config": {"okr_agent_only": True},
            "config_schema": {},
        },
]
