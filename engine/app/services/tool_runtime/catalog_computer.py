"""OS-aware computer catalog presentation helpers."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Awaitable, Callable
from typing import NotRequired, TypedDict

from app.core.tool_types import ToolDefinition


class ComputerToolConfig(TypedDict, total=False):
    """Computer tool configuration attributes used by catalog presentation."""

    os_type: NotRequired[str]


async def get_computer_os_type(
    agent_id: uuid.UUID,
    *,
    get_tool_config: Callable[[uuid.UUID, str], Awaitable[ComputerToolConfig | None]],
) -> str:
    """Return the configured computer OS, defaulting to AgentBay's Windows default."""
    try:
        config = await get_tool_config(agent_id, "agentbay_browser_navigate")
        return config["os_type"] if config and "os_type" in config else "windows"
    except Exception:
        return "windows"


def patch_computer_tool_descriptions(tools: list[ToolDefinition], os_type: str) -> list[ToolDefinition]:
    """Return tools with an OS-specific copy of the AgentBay file-transfer definition."""
    if os_type == "windows":
        desktop_path = r"C:\Users\Administrator\Desktop"
        home_path = r"C:\Users\Administrator"
        computer_os_label = "Windows"
    else:
        desktop_path = "/home/wuying/Desktop"
        home_path = "/home/wuying"
        computer_os_label = "Linux"

    new_file_transfer_desc = (
        (
            "Transfer a file between any two endpoints: the agent workspace, "
            + "the AgentBay browser environment, the cloud desktop (computer), or the code sandbox.\n\n"
            + f"COMPUTER ENVIRONMENT OS: {computer_os_label}\n"
            + f"VERIFIED PATH CONVENTIONS for the computer environment ({computer_os_label}):\n"
            + f"- computer desktop: {desktop_path}\\<filename>  (e.g. {desktop_path}\\report.xlsx)\n"
            + f"- computer home:    {home_path}\\<filename>\n\n"
            + "Other environments (Linux-based, user 'wuying', HOME=/home/wuying/):\n"
            + "- code env:     /home/wuying/<filename>  (e.g. /home/wuying/data.csv)\n"
            + "- browser env:  /home/wuying/下载/<filename>  (download folder)\n"
            + "- workspace:    relative path, e.g. 'workspace/data.csv'\n\n"
            + "Transfer directions:\n"
            + "- workspace -> env: upload a workspace file into a cloud environment\n"
            + "- env -> workspace: download a file from a cloud environment into the workspace\n"
            + "- env A -> env B:   transfer between environments (transparent backend temp)"
        )
        if os_type == "windows"
        else (
            "Transfer a file between any two endpoints: the agent workspace, "
            + "the AgentBay browser environment, the cloud desktop (computer), or the code sandbox.\n\n"
            + f"COMPUTER ENVIRONMENT OS: {computer_os_label}\n"
            + f"VERIFIED PATH CONVENTIONS for the computer environment ({computer_os_label}):\n"
            + f"- computer desktop: {desktop_path}/<filename>  (e.g. {desktop_path}/report.xlsx)\n"
            + f"- computer home:    {home_path}/<filename>\n\n"
            + "Other environments (also Linux, user 'wuying'):\n"
            + "- code env:     /home/wuying/<filename>  (e.g. /home/wuying/data.csv)\n"
            + "- browser env:  /home/wuying/下载/<filename>  (download folder)\n"
            + "- workspace:    relative path, e.g. 'workspace/data.csv'\n\n"
            + "Transfer directions:\n"
            + "- workspace -> env: upload a workspace file into a cloud environment\n"
            + "- env -> workspace: download a file from a cloud environment into the workspace\n"
            + "- env A -> env B:   transfer between environments (transparent backend temp)"
        )
    )

    patched: list[ToolDefinition] = []
    for tool in tools:
        function = tool["function"]
        if function["name"] == "agentbay_file_transfer":
            tool = copy.deepcopy(tool)
            tool["function"]["description"] = new_file_transfer_desc
            properties = tool["function"]["parameters"]["properties"]
            if "from_path" in properties:
                properties["from_path"]["description"] = _path_hint("Source", os_type)
            if "to_path" in properties:
                properties["to_path"]["description"] = _path_hint("Destination", os_type)
        patched.append(tool)
    return patched


def _path_hint(direction: str, os_type: str) -> str:
    if os_type == "windows":
        return (
            f"{direction} path. Relative if workspace (e.g. 'workspace/data.csv'). "
            + r"Absolute if env: computer → C:\Users\Administrator\Desktop\file, "
            + "code → /home/wuying/file, browser → /home/wuying/下载/file."
        )
    return (
        f"{direction} path. Relative if workspace (e.g. 'workspace/data.csv'). "
        + "Absolute if env: computer → /home/wuying/Desktop/file, "
        + "code → /home/wuying/file, browser → /home/wuying/下载/file."
    )
