"""bytedance-ops plugin registration.

Per design spec §10: registers Hermes tools for TikTok Organic,
TikTok Business Messaging admin, TikTok creator posting, and Douyin
operations.  All mutating tools use the immutable prepare/commit ledger.
"""

from __future__ import annotations

import logging
from typing import Any

from plugins.bytedance_ops.schemas import ALL_SCHEMAS
from plugins.bytedance_ops.tiktok_organic import TikTokOrganicOps
from plugins.bytedance_ops.tiktok_creator import TikTokCreatorOps
from plugins.bytedance_ops.tiktok_admin import TikTokBusinessAdmin
from plugins.bytedance_ops.douyin_content import DouyinOps

logger = logging.getLogger(__name__)


def register_tools(ctx: Any) -> None:
    """Register all bytedance-ops tools with the Hermes tools registry.

    Tools are classified:
    - Read: no approval gate
    - Prepare: creates immutable intent (VALIDATED)
    - Commit: requires APPROVED intent, performs provider call
    """
    organic = TikTokOrganicOps(
        profile=ctx.profile if hasattr(ctx, "profile") else "default",
        accounts={},
    )
    creator = TikTokCreatorOps(
        profile=ctx.profile if hasattr(ctx, "profile") else "default",
        accounts={},
    )
    admin = TikTokBusinessAdmin(
        profile=ctx.profile if hasattr(ctx, "profile") else "default",
        accounts={},
    )
    douyin = DouyinOps(
        profile=ctx.profile if hasattr(ctx, "profile") else "default",
        accounts={},
    )

    # TikTok Organic read tools
    ctx.register_tool(
        "tiktok_account_get",
        TikTokOrganicOps.account_get,
        schema=ALL_SCHEMAS[0],
        classification="read",
        tool_group="bytedance",
    )
    ctx.register_tool(
        "tiktok_posts_list",
        TikTokOrganicOps.posts_list,
        schema=ALL_SCHEMAS[1],
        classification="read",
        tool_group="bytedance",
    )
    ctx.register_tool(
        "tiktok_comments_list",
        TikTokOrganicOps.comments_list,
        schema=ALL_SCHEMAS[2],
        classification="read",
        tool_group="bytedance",
    )
    ctx.register_tool(
        "tiktok_publish_status",
        TikTokOrganicOps.publish_status,
        schema=ALL_SCHEMAS[9],
        classification="read",
        tool_group="bytedance",
    )

    # TikTok Organic prepare/commit tools
    ctx.register_tool(
        "tiktok_comment_reply_prepare",
        TikTokOrganicOps.comment_reply_prepare,
        schema=ALL_SCHEMAS[3],
        classification="prepare",
        tool_group="bytedance",
    )
    ctx.register_tool(
        "tiktok_comment_reply_commit",
        TikTokOrganicOps.comment_reply_commit,
        schema=ALL_SCHEMAS[4],
        classification="commit",
        approval_required=True,
        tool_group="bytedance",
    )
    ctx.register_tool(
        "tiktok_post_prepare",
        TikTokOrganicOps.post_prepare,
        schema=ALL_SCHEMAS[7],
        classification="prepare",
        tool_group="bytedance",
    )
    ctx.register_tool(
        "tiktok_post_commit",
        TikTokOrganicOps.post_commit,
        schema=ALL_SCHEMAS[8],
        classification="commit",
        approval_required=True,
        tool_group="bytedance",
    )
    ctx.register_tool(
        "tiktok_comment_moderate_prepare",
        TikTokOrganicOps.comment_moderate_prepare,
        schema=ALL_SCHEMAS[5],
        classification="prepare",
        tool_group="bytedance",
    )
    ctx.register_tool(
        "tiktok_comment_moderate_commit",
        TikTokOrganicOps.comment_moderate_commit,
        schema=ALL_SCHEMAS[6],
        classification="commit",
        approval_required=True,
        tool_group="bytedance",
    )

    # TikTok creator posting (§10.3)
    ctx.register_tool(
        "tiktok_creator_connect",
        TikTokCreatorOps.creator_connect,
        schema=ALL_SCHEMAS[10],
        classification="write",
        tool_group="bytedance",
    )
    ctx.register_tool(
        "tiktok_creator_post_prepare",
        TikTokCreatorOps.creator_post_prepare,
        schema=ALL_SCHEMAS[11],
        classification="prepare",
        tool_group="bytedance",
    )
    ctx.register_tool(
        "tiktok_creator_post_commit",
        TikTokCreatorOps.creator_post_commit,
        schema=ALL_SCHEMAS[12],
        classification="commit",
        approval_required=True,
        tool_group="bytedance",
    )

    # TikTok admin tools (§4.3–§4.7)
    ctx.register_tool(
        "tiktok_auto_messages_list",
        TikTokBusinessAdmin.auto_messages_list,
        schema={"type": "function", "function": {
            "name": "tiktok_auto_messages_list",
            "parameters": {"type": "object", "properties": {
                "account_alias": {"type": "string"},
            }, "required": ["account_alias"]},
        }},
        classification="read",
        tool_group="bytedance_admin",
    )
    # ... additional admin tools registered similarly

    # Douyin operations (§11)
    ctx.register_tool(
        "douyin_account_get",
        DouyinOps.account_get,
        schema=ALL_SCHEMAS[13],
        classification="read",
        tool_group="bytedance",
    )
    ctx.register_tool(
        "douyin_scopes_get",
        DouyinOps.scopes_get,
        schema=ALL_SCHEMAS[14],
        classification="read",
        tool_group="bytedance",
    )
    ctx.register_tool(
        "douyin_content_list",
        DouyinOps.content_list,
        schema=ALL_SCHEMAS[15],
        classification="read",
        tool_group="bytedance",
    )
    ctx.register_tool(
        "douyin_message_capability_get",
        DouyinOps.message_capability_get,
        schema=ALL_SCHEMAS[16],
        classification="read",
        tool_group="bytedance",
    )
