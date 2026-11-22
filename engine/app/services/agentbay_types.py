"""Typed adapters at the AgentBay SDK boundary (``wuying-agentbay-sdk``, import name ``agentbay``).

The SDK leaves parts of its surface untyped: every ``BrowserOperator`` method takes an unannotated
``page=None`` parameter, and result payloads are plain dicts and lists. ``agentbay_client`` and the
AgentBay tool handlers reach those values only through the adapters below.

SDK and Pillow types are imported for annotations only: ``agentbay_client`` must disable the SDK's
logger override before the first ``agentbay`` import, and Pillow stays a lazy import.
"""

from typing import TYPE_CHECKING, Protocol, TypeIs, cast

from pydantic import BaseModel

if TYPE_CHECKING:
    from agentbay import ActOptions, ActResult, ExtractOptions, ObserveOptions, ObserveResult, Session
    from PIL import Image


class AgentBayBrowserOperator(Protocol):
    """The ``session.browser.operator`` calls the client makes, minus the unannotated ``page`` parameter."""

    def navigate(self, url: str) -> str: ...

    def screenshot(self, *, full_page: bool = True) -> str: ...

    def act(self, action_input: ActOptions) -> ActResult: ...

    def login(self, login_config: str, *, use_vision: bool | None = False) -> ActResult: ...

    def extract[SchemaT: BaseModel](self, options: ExtractOptions[SchemaT]) -> tuple[bool, object]: ...

    def observe(self, options: ObserveOptions) -> tuple[bool, list[ObserveResult]]: ...


def browser_operator(session: Session) -> AgentBayBrowserOperator:
    """Return the session's browser operator through the typed adapter.

    The SDK's bare ``ExtractOptions`` parameter is gradual, so ``BrowserOperator`` is assignable to the
    adapter but not a subtype of it; this is the one cast at the AgentBay boundary.
    """
    return cast("AgentBayBrowserOperator", session.browser.operator)


class ResizableImage(Protocol):
    """``PIL.Image.Image.resize`` without the numpy-array ``size`` form, whose type needs numpy installed."""

    def resize(self, size: tuple[int, int], resample: int | None = None) -> Image.Image: ...


def resize_image(image: ResizableImage, size: tuple[int, int], resample: int) -> Image.Image:
    """Return ``image.resize(size, resample)`` with a fully known signature."""
    return image.resize(size, resample)


def is_sdk_mapping(value: object) -> TypeIs[dict[object, object]]:
    """Narrow an SDK payload (or a client response field built from one) to a dict of unknown shape."""
    return isinstance(value, dict)


def is_sdk_list(value: object) -> TypeIs[list[object]]:
    """Narrow an SDK payload (or a client response field built from one) to a list of unknown items."""
    return isinstance(value, list)
