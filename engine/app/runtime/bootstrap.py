"""Ordered genesis and optional seeds, preserving their startup failure policy."""

import shutil
from pathlib import Path

from app.config import get_settings
from app.core.logging import logger


async def bootstrap() -> None:
    from app.services.platform_admin_seeder import PlatformAdminSeedError, ensure_platform_admin
    from app.services.system_org_seeder import SystemOrgSeedError, ensure_system_orgs

    try:
        _ = await ensure_system_orgs()
    except SystemOrgSeedError:
        logger.error("[startup] System organization seed failed: no default end-user org")
        raise
    except Exception as exc:
        logger.warning("[startup] System organization seed failed: {}", exc)
    try:
        _ = await ensure_platform_admin()
    except PlatformAdminSeedError:
        logger.error("[startup] Platform admin seed failed: configuration or credential policy error")
        raise
    except Exception as exc:
        logger.error("[startup] Platform admin seed failed: {}", exc)
        raise

    try:
        from app.dao.tenant_dao import tenant_dao

        data_dir = Path(get_settings().AGENT_DATA_DIR)
        old_dir = data_dir / "enterprise_info"
        if old_dir.exists() and any(old_dir.iterdir()):
            tenant = await tenant_dao.get_first_by_created_at()
            if tenant:
                new_dir = data_dir / f"enterprise_info_{tenant.id}"
                if not new_dir.exists():
                    _ = shutil.copytree(old_dir, new_dir)
                    logger.info("[startup] Migrated enterprise_info for tenant {}", tenant.id)
    except Exception as exc:
        logger.warning("[startup] enterprise_info migration failed: {}", exc)

    try:
        from app.services.tool_seeder import clean_orphaned_mcp_tools, seed_builtin_tools

        await seed_builtin_tools()
        await clean_orphaned_mcp_tools()
    except Exception as exc:
        logger.warning("[startup] Builtin tools seed or cleanup failed: {}", exc)
    try:
        from app.services.tool_seeder import get_atlassian_api_key, seed_atlassian_rovo_config

        await seed_atlassian_rovo_config()
        key = await get_atlassian_api_key()
        if key:
            from app.services.resource_discovery import seed_atlassian_rovo_tools

            await seed_atlassian_rovo_tools(key)
    except Exception as exc:
        logger.warning("[startup] Atlassian tools seed failed: {}", exc)
    try:
        from app.services.template_seeder import seed_agent_templates

        await seed_agent_templates()
    except Exception as exc:
        logger.warning("[startup] Agent templates seed failed: {}", exc)
    try:
        from app.services.clawsec_runtime import seed_clawsec_skills
        from app.services.gogcli_runtime import seed_gogcli_skill
        from app.services.linkup_runtime import seed_linkup_skills
        from app.services.skill_seeder import push_default_skills_to_existing_agents, seed_skills

        _ = await seed_skills()
        _ = await seed_gogcli_skill(None)
        _ = await seed_clawsec_skills(None)
        _ = await seed_linkup_skills(None)
        _ = await push_default_skills_to_existing_agents()
    except Exception as exc:
        logger.warning("[startup] Skills seed failed: {}", exc)
    try:
        from app.services.agent_seeder import seed_default_agents

        await seed_default_agents()
    except Exception as exc:
        logger.warning("[startup] Default agents seed failed: {}", exc)
    try:
        from app.services.agent_seeder import seed_okr_agent

        await seed_okr_agent()
    except Exception as exc:
        logger.warning("[startup] OKR Agent seed failed: {}", exc)
    try:
        from app.services.agent_seeder import patch_existing_okr_agent

        await patch_existing_okr_agent()
    except Exception as exc:
        logger.warning("[startup] OKR Agent patch failed: {}", exc)
