"""Platform-wide service for URL resolution and host type detection."""

import re
from typing import Protocol, override

from fastapi import Request

from app.config import get_settings


class PublicBaseUrlNotConfiguredError(Exception):
    """An absolute public URL requires deployment-owned configuration."""

    @override
    def __str__(self) -> str:
        return "PUBLIC_BASE_URL is not configured on this server. Set PUBLIC_BASE_URL in the deployment settings."


class _TenantSsoTarget(Protocol):
    slug: str
    sso_domain: str | None


class PlatformService:
    """Service to handle platform-wide settings and URL resolution."""

    def is_ip_address(self, host: str) -> bool:
        """Check if a host is an IP address (IPv4)."""
        # Strip protocol and port if present
        h = host.split("://")[-1].split(":")[0].split("/")[0]
        # Basic IPv4 regex
        ip_pattern = re.compile(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$")
        return bool(ip_pattern.match(h))

    async def get_public_base_url(self, request: Request | None = None) -> str:
        """Resolve Settings.PUBLIC_BASE_URL or raise PublicBaseUrlNotConfiguredError."""
        base_url = get_settings().PUBLIC_BASE_URL.strip().rstrip("/")
        if not base_url:
            raise PublicBaseUrlNotConfiguredError
        return base_url

    async def get_tenant_sso_base_url(
        self,
        tenant: _TenantSsoTarget,
        request: Request | None = None,
        *,
        sso_redirect_enabled: bool = True,
    ) -> str:
        """Generate the SSO base URL for a tenant based on IP/Domain logic.

        ``sso_redirect_enabled`` should be pre-resolved by the caller via
        ``system_setting_dao.is_sso_custom_domain_redirect_enabled()`` so this
        method never issues an extra DB round-trip for the setting.
        """
        base_url = await self.get_public_base_url(request)
        if sso_redirect_enabled and tenant.sso_domain not in (None, ""):
            return tenant.sso_domain.rstrip("/")

        if not sso_redirect_enabled:
            return base_url

        # Parse protocol and host
        # Example: http://1.2.3.4:8000 or http://maraclaw.ai
        parts = base_url.split("://")
        if len(parts) < 2:
            return base_url

        protocol = parts[0]
        host_port = parts[1]

        # Split host and port
        host_parts = host_port.split(":")
        host = host_parts[0]
        port = f":{host_parts[1]}" if len(host_parts) > 1 else ""

        if self.is_ip_address(host):
            # IP: No subdomain, just base URL
            return base_url
        # Domain: {tenant_slug}.{domain}
        # Special case for localhost: keep it as is or handle it
        if host == "localhost":
            return f"{protocol}://{host}{port}"

        # Generic logic: if host has a subdomain,
        # we strip the first component to form a base for tenant subdomains.
        h_parts = host.split(".")
        target_host = ".".join(h_parts[1:]) if len(h_parts) > 2 else host

        return f"{protocol}://{tenant.slug}.{target_host}{port}"


# Global instance
platform_service = PlatformService()
