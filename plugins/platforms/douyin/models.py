"""Douyin Open Platform data models and constants."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, FrozenSet


PROVIDER_DOUYIN = "douyin"

# TikTok Business Messaging scopes (mapped to Douyin equivalents)
SCOPE_IM_DIRECT_MESSAGE = "im.direct_message"
SCOPE_IM_SEND_MSG = "im_send_msg"
SCOPE_IM_RECEIVE_MSG = "im_receive_msg"
SCOPE_IM_ENTER_DIRECT_MSG = "im_enter_direct_msg"
SCOPE_IM_RECALL_MSG = "im_recall_msg"
SCOPE_INFO_SNS = "info_sns"
SCOPE_INFO_CALLBACK = "info_callback"

# All required scopes for basic IM functionality
REQUIRED_IM_SCOPES: FrozenSet[str] = frozenset({
    SCOPE_IM_DIRECT_MESSAGE,
    SCOPE_IM_SEND_MSG,
    SCOPE_IM_RECEIVE_MSG,
})

# Scopes for content operations
SCOPE_VIDEO_CREATE = "video.create"
SCOPE_VIDEO_LIST = "video.list"
SCOPE_VIDEO_UPDATE = "video.update"
SCOPE_COMMENT = "comment"
SCOPE_COMMENT_REPLY = "comment_reply"


class DouyinAPI:
    """Constants for Douyin Open Platform API endpoints."""

    # Two base URLs depending on operation type
    OPEN_BASE = "https://open.douyin.com"
    IM_BASE = "https://open.douyin.com"  # Same for IM

    # IM OpenAPI endpoints
    SEND_PRIVATE_MSG = "/open_api/v2.0/im/send_private_msg/"
    SEND_GROUP_MSG = "/open_api/v2.0/im/send_group_msg/"
    RECEIVE_MSG = "/open_api/v2.0/im/receive_msg/"
    ENTER_DIRECT_MSG = "/open_api/v2.0/im/enter_direct_msg/"
    RECALL_MSG = "/open_api/v2.0/im/recall_msg/"
    LIST_CONVERSATION = "/open_api/v2.0/im/list_conv/"
    MSG_READ = "/open_api/v2.0/im/msg_read/"

    # Content endpoints
    VIDEO_LIST = "/open_api/v2.0/video/list/"
    VIDEO_CREATE = "/open_api/v2.0/video/create/"
    VIDEO_UPDATE = "/open_api/v2.0/video/update/"
    VIDEO_DELETE = "/open_api/v2.0/video/delete/"

    # Comment endpoints
    COMMENT_LIST = "/open_api/v2.0/comment/list/"
    COMMENT_CREATE = "/open_api/v2.0/comment/create/"
    COMMENT_REPLY_LIST = "/open_api/v2.0/comment/reply/list/"
    COMMENT_REPLY_CREATE = "/open_api/v2.0/comment/reply/create/"

    # User info
    USER_INFO = "/open_api/v2.0/user/info/"

    # Media
    IMAGE_DOWNLOAD = "/open_api/v2.0/media/image/download/"
    VIDEO_DOWNLOAD = "/open_api/v2.0/media/video/download/"

    # Webhook events
    WEBHOOK_SUBSCRIBE = "/open_api/v2.0/webhook/subscribe/"
    WEBHOOK_UNSUBSCRIBE = "/open_api/v2.0/webhook/unsubscribe/"
    WEBHOOK_LIST = "/open_api/v2.0/webhook/list/"

    # Token
    ACCESS_TOKEN = "/open_api/v2.0/oauth/access_token/"


@dataclass(frozen=True)
class DouyinSendGrant:
    """Scene-aware send grant for Douyin (design spec §11.4).

    Douyin documents multiple messaging scenes with different
    prerequisites and timing.  The adapter represents them as
    explicit grants.
    """

    scene: str  # "im_reply_msg", "im_enter_direct_msg", "im_b2b_direct_message", "unknown"
    source_event_id: Optional[str]
    conversation_short_id: Optional[str]
    reply_message_id: Optional[str]
    expires_at: Optional[datetime]
    remaining_count: Optional[int]
    eligible: bool
    reason: Optional[str]


@dataclass(frozen=True)
class DouyinMessage:
    """A single normalized Douyin message."""

    message_id: str
    conversation_id: str
    from_user_id: str
    to_user_id: str
    message_type: str  # "text", "image", "video", "unsupported"
    text: Optional[str]
    media_url: Optional[str]
    media_type: Optional[str]
    created_at: datetime
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class DouyinConversation:
    """A Douyin conversation."""

    conversation_id: str
    peer_id: str
    display_name: str
    last_message_at: datetime
    message_count: int = 0


@dataclass(frozen=True)
class DouyinAccountConfig:
    """Account-level configuration for a Douyin app."""

    provider: str
    profile: str
    account_alias: str
    open_id: str
    client_key: str
    client_secret: str
    access_token_secret: str
    refresh_token_secret: str
    webhook_secret: str
    route_id: Optional[str] = None
    home_conversation: Optional[str] = None
    allowed_users: List[str] = field(default_factory=list)
    allow_all_users: bool = False
    region: Optional[str] = None
