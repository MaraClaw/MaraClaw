from app.api.agentbay_control import _perform_drag


class DesktopClient:
    def __init__(self) -> None:
        self._image_type = "linux_latest"
        self.drag_calls: list[tuple[int, int, int, int]] = []

    async def computer_drag_mouse(self, from_x: int, from_y: int, to_x: int, to_y: int) -> dict[str, bool]:
        self.drag_calls.append((from_x, from_y, to_x, to_y))
        return {"success": True}


async def test_perform_drag_uses_computer_api_for_desktop_sessions() -> None:
    client = DesktopClient()
    result = await _perform_drag(client, 10, 20, 30, 40)
    assert client.drag_calls == [(10, 20, 30, 40)]
    assert result == {
        "success": True,
        "method": "computer_drag",
        "output": "Dragged (10,20) -> (30,40)",
    }
