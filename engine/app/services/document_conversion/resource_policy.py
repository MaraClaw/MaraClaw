"""The one rule deciding which resources an HTML conversion may load.

A ``data:`` URL is self-contained and always allowed. A ``file:`` URL, or a
reference resolved against a local base, is allowed only when its resolved
path, symlinks included, is a regular file inside the conversion workspace.
Every other scheme is refused, so no renderer fetches anything over the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import override
from urllib.parse import unquote, urljoin, urlsplit


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    """Containment rule bound to one resolved conversion workspace root."""

    root: Path

    @classmethod
    def for_workspace(cls, workspace: Path) -> ResourcePolicy:
        return cls(workspace.resolve(strict=True))

    def contained_file(self, url: str) -> Path | None:
        """Return the resolved in-workspace file an absolute ``file:`` URL names, else None."""
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        if parts.scheme.lower() != "file" or parts.netloc not in ("", "localhost"):
            return None
        raw_path = unquote(parts.path)
        if not raw_path.startswith("/"):
            return None
        try:
            resolved = Path(raw_path).resolve(strict=True)
        except OSError, RuntimeError, ValueError:
            return None
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            return None
        return resolved

    def permitted_url(self, url: str) -> str | None:
        """Return the URL a renderer may open for ``url``, or None when it is refused.

        A permitted ``file:`` URL is rewritten to its resolved path so the
        opened file is the one that was checked.
        """
        try:
            scheme = urlsplit(url).scheme.lower()
        except ValueError:
            return None
        if scheme == "data":
            return url
        resolved = self.contained_file(url)
        return resolved.as_uri() if resolved is not None else None

    def contained_reference(self, reference: str, base_dir: Path) -> Path | None:
        """Resolve a markup reference against a local directory the way a browser would."""
        base_url = base_dir.resolve().as_uri() + "/"
        return self.contained_file(urljoin(base_url, reference.strip()))


def weasyprint_url_fetcher(policy: ResourcePolicy) -> object:
    """Build a WeasyPrint ``URLFetcher`` that only opens what ``policy`` permits.

    Network protocols and redirects are disabled in the base fetcher as well,
    and a refused URL raises inside ``fetch`` so WeasyPrint logs it and renders
    the document without that resource.
    """
    from weasyprint.urls import URLFetcher, URLFetcherResponse

    class ContainedUrlFetcher(URLFetcher):
        @override
        def fetch(self, url: str, headers: dict[str, str] | None = None) -> URLFetcherResponse:
            permitted = policy.permitted_url(url)
            if permitted is None:
                raise ValueError(f"Resource outside the conversion workspace was not loaded: {url}")
            response: object = super().fetch(permitted, headers)
            if not isinstance(response, URLFetcherResponse):
                raise TypeError("WeasyPrint URLFetcher returned an unexpected response type")
            return response

    return ContainedUrlFetcher(allowed_protocols=("data", "file"), allow_redirects=False)
