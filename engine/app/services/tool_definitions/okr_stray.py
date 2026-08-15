"""Seed rows extracted from builtin.py. Data only."""


from typing import Any

OKR_STRAY_TOOLS: list[dict[str, Any]] = [
        {
            "name": "update_kr_content",
            "display_name": "Update KR Content",
            "description": (
                "Update the content fields of one of YOUR OWN Key Results. "
                + "Call get_my_okr first to obtain the kr_id, then change title, target_value, unit, "
                + "focus_ref, or status as needed. This does not record a progress update."
            ),
            "category": "okr",
            "icon": "✏️",
            "is_default": True,
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
                        "description": "Optional new focus reference.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["on_track", "at_risk", "behind", "completed"],
                        "description": "Optional explicit status value.",
                    },
                },
                "required": ["kr_id"],
            },
            "config": {},
            "config_schema": {},
        },
]
