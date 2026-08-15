"""TikTok Business MCP server configuration.

Per design spec §10.1 (BD-14): classify the official TikTok Business
MCP server and configure it as a local MCP tool source for read-only
and write-capable operations.

The official TikTok Business MCP server is classified as:
  - Read: read-only data retrieval (videos, comments, analytics)
  - Write: publishing and moderation actions

It is configured via ``hermes.json`` / ``hermes_config.toml`` as a
stdio MCP server with the TikTok Business access token.

This module does NOT implement the MCP server itself — it is a
configuration helper and classifier.  The official server is
distributed separately by TikTok.
"""

from __future__ import annotations

import logging
import os
import shutil
import json
from typing import Any, Dict, Optional

from plugins.bytedance.shared.errors import ProviderError

logger = logging.getLogger(__name__)

# MCP server classification (§10.1)
MCP_CLASSIFICATION_READ_ONLY = "read_only"
MCP_CLASSIFICATION_READ_WRITE = "read_write"
MCP_CLASSIFICATION_WRITE_ONLY = "write_only"

TIKTOK_BUSINESS_MCP_INFO = {
    "name": "TikTok Business MCP",
    "classification": MCP_CLASSIFICATION_READ_WRITE,
    "source": "official",  # official TikTok distribution
    "entry_point": "tiktok-business-mcp",
    "env_prefix": "TIKTOK_",
    "required_config": {
        "access_token": "TikTok-API-Access-Token (from TikTok Business Center)",
        "open_id": "TikTok-App-Open-ID",
    },
    "optional_config": {
        "base_url": "Custom TikTok API endpoint (default: standard)",
        "timeout": "Request timeout in seconds",
    },
    "safety_boundary": (
        "The official TikTok Business MCP runs as a child process.  "
        "Read-only scopes (analytics, video list) are safe to proxy "
        "through it.  Write scopes (publish, comment moderation) must "
        "pass through the immutable prepare/commit ledger (BD-15)."
    ),
    "pip_package": "tiktok-business-mcp",
    "setup_command": "pip install tiktok-business-mcp",
}


def get_mcp_classification() -> str:
    """Return the classification of the official TikTok Business MCP."""
    return MCP_CLASSIFICATION_READ_WRITE


def is_official() -> bool:
    """The TikTok Business MCP is officially distributed by TikTok."""
    return True


def check_mcp_available() -> bool:
    """Check if the official TikTok Business MCP is installed."""
    return shutil.which("tiktok-business-mcp") is not None


def build_mcp_config(
    *,
    access_token_secret_ref: str,
    open_id: str,
    base_url: Optional[str] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """Build a Hermes MCP server configuration entry for TikTok Business.

    Returns a dict suitable for ``mcpServers`` in ``hermes_config.toml``
    or the ``mcp.servers`` section in ``hermes.json``.
    """
    env: Dict[str, str] = {}
    if access_token_secret_ref:
        env["TIKTOK_ACCESS_TOKEN"] = f"secrets://{access_token_secret_ref}"
    if open_id:
        env["TIKTOK_OPEN_ID"] = open_id
    if base_url:
        env["TIKTOK_BASE_URL"] = base_url
    env["TIKTOK_TIMEOUT"] = str(timeout)

    return {
        "command": "tiktok-business-mcp",
        "env": env,
        "classification": MCP_CLASSIFICATION_READ_WRITE,
        "safety_boundary": TIKTOK_BUSINESS_MCP_INFO["safety_boundary"],
        "write_tools": [
            "tiktok_create_video",
            "tiktok_reply_comment",
            "tiktok_hide_comment",
            "tiktok_delete_comment",
        ],
        "read_tools": [
            "tiktok_list_videos",
            "tiktok_list_comments",
            "tiktok_get_analytics",
            "tiktok_get_video",
        ],
    }


def mcp_config_to_toml(mcp_config: Dict[str, Any]) -> str:
    """Serialize an MCP config entry to TOML format."""
    lines = []
    command = mcp_config.get("command", "tiktok-business-mcp")
    lines.append(f"command = {json.dumps(command)}")
    env = mcp_config.get("env", {})
    if env:
        lines.append("env = {")
        for k, v in env.items():
            lines.append(f'  {k} = {json.dumps(v)}')
        lines.append("}")
    return "\n".join(lines)
