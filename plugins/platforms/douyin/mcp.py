"""Douyin MCP server stub.

This is a classification and configuration helper for a Douyin MCP
server.  The actual MCP server implementation would be provided as
a separate stdio process.  This module provides the configuration
template and classification boundary per design spec §11.6.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from plugins.bytedance_ops.mcp_config import (
    MCP_CLASSIFICATION_READ_ONLY,
    MCP_CLASSIFICATION_READ_WRITE,
)

logger = logging.getLogger(__name__)

# Douyin MCP server classification
DOUYIN_MCP_CLASSIFICATION = MCP_CLASSIFICATION_READ_ONLY

DOUYIN_MCP_INFO = {
    "name": "Douyin Content MCP",
    "classification": DOUYIN_MCP_CLASSIFICATION,
    "source": "community",  # no official Douyin MCP exists
    "entry_point": "douyin-mcp",
    "env_prefix": "DOUYIN_MCP_",
    "required_config": {
        "access_token": "Douyin user access token",
        "open_id": "Douyin user open_id",
        "client_key": "Douyin client key",
    },
    "safety_boundary": (
        "This server only performs read operations (content listing, "
        "analytics).  All write operations (posting, messaging) must "
        "pass through the immutable prepare/commit ledger."
    ),
    "pip_package": "douyin-mcp-server",
    "setup_command": "pip install douyin-mcp-server",
}


def build_douyin_mcp_config(
    *,
    access_token_secret_ref: str,
    open_id: str,
    client_key: str,
) -> Dict[str, Any]:
    """Build a Hermes MCP server configuration entry for Douyin."""
    return {
        "command": "douyin-mcp",
        "env": {
            "DOUYIN_MCP_ACCESS_TOKEN": f"secrets://{access_token_secret_ref}",
            "DOUYIN_MCP_OPEN_ID": open_id,
            "DOUYIN_MCP_CLIENT_KEY": client_key,
        },
        "classification": DOUYIN_MCP_CLASSIFICATION,
        "safety_boundary": DOUYIN_MCP_INFO["safety_boundary"],
        "read_tools": [
            "douyin_list_content",
            "douyin_get_analytics",
        ],
    }
