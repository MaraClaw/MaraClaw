"""Deployment builtin tool definitions."""

DEPLOY_BUILTIN_TOOLS = [
    {
        "name": "vercel_deploy",
        "display_name": "Deploy to Vercel",
        "description": "Deploy a project from workspace to Vercel. Supports two modes: 'upload' (direct file upload, no GitHub needed) or 'github' (push to GitHub repo, Vercel auto-deploys). Returns the deployment URL.",
        "category": "deploy",
        "icon": "🚀",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Vercel project name (will be created if not exists)",
                },
                "source_dir": {
                    "type": "string",
                    "description": "Directory in workspace containing the project, e.g. 'workspace/my-app'",
                },
                "deploy_method": {
                    "type": "string",
                    "enum": ["upload", "github"],
                    "description": "'upload': direct file upload (simple, no GitHub needed). 'github': push to GitHub repo and let Vercel auto-deploy (better for version control and CI/CD). Default: 'upload'.",
                },
                "github_repo": {
                    "type": "string",
                    "description": "GitHub repo in 'owner/repo' format. Required when deploy_method='github'.",
                },
                "framework": {
                    "type": "string",
                    "description": "Framework preset: 'nextjs', 'vite', 'static', etc.",
                    "enum": ["nextjs", "vite", "nuxtjs", "static", "remix", "astro"],
                },
                "production": {
                    "type": "boolean",
                    "description": "If true, deploy to production. Default false (preview).",
                },
            },
            "required": ["project_name", "source_dir"],
        },
        "config": {"vercel_token": ""},
        "config_schema": {
            "fields": [
                {
                    "key": "vercel_token",
                    "label": "Vercel Access Token",
                    "type": "password",
                    "default": "",
                    "help_text": "Get from https://vercel.com/account/tokens",
                }
            ]
        },
    },
    {
        "name": "vercel_list_deployments",
        "display_name": "List Vercel Deployments",
        "description": "List recent deployments for a Vercel project. Shows status, URL, and creation time.",
        "category": "deploy",
        "icon": "📋",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {"project_name": {"type": "string", "description": "Vercel project name"}},
            "required": ["project_name"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "vercel_get_deploy_logs",
        "display_name": "Get Deploy Logs",
        "description": "Get build logs and runtime logs for a Vercel deployment. Useful for debugging failed deployments.",
        "category": "deploy",
        "icon": "📜",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {"deployment_id": {"type": "string", "description": "Deployment ID or URL"}},
            "required": ["deployment_id"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "vercel_set_env",
        "display_name": "Set Environment Variable",
        "description": "Set an environment variable for a Vercel project. Use for database URLs, API keys, and other secrets.",
        "category": "deploy",
        "icon": "🔐",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string"},
                "key": {"type": "string", "description": "Environment variable name, e.g. DATABASE_URL"},
                "value": {"type": "string", "description": "Environment variable value"},
                "target": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["production", "preview", "development"]},
                    "description": "Deployment targets. Default: all.",
                },
            },
            "required": ["project_name", "key", "value"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "vercel_manage_domain",
        "display_name": "Manage Domain",
        "description": "Check domain availability/pricing, or bind a custom domain to a Vercel project.",
        "category": "deploy",
        "icon": "🌐",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["check", "bind"],
                    "description": "'check' to check availability/price, 'bind' to add domain to project",
                },
                "domain": {"type": "string", "description": "Domain name, e.g. 'myapp.com'"},
                "project_name": {"type": "string", "description": "Required for 'bind' action"},
            },
            "required": ["action", "domain"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "neon_create_database",
        "display_name": "Create Postgres Database",
        "description": "Create a new Neon Postgres database. Returns the DATABASE_URL connection string. Use vercel_set_env to inject it into your Vercel project.",
        "category": "deploy",
        "icon": "🐘",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Name for the Neon project"},
                "database_name": {"type": "string", "description": "Database name, default 'neondb'"},
                "region": {
                    "type": "string",
                    "description": "Region: 'aws-us-east-1', 'aws-eu-central-1', etc.",
                    "default": "aws-us-east-1",
                },
                "org_id": {
                    "type": "string",
                    "description": "Optional: Neon Organization ID. If not provided and you belong to multiple organizations, the tool will automatically list them for you to choose.",
                },
            },
            "required": ["project_name"],
        },
        "config": {"neon_api_key": ""},
        "config_schema": {
            "fields": [
                {
                    "key": "neon_api_key",
                    "label": "Neon API Key",
                    "type": "password",
                    "default": "",
                    "help_text": "Get from https://console.neon.tech/app/settings/api-keys",
                }
            ]
        },
    },
]
