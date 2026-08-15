"""TikTok Business data models and constants."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, FrozenSet


# Provider identifiers — these are the provider's exact vocabulary and
# remain visible in metadata.  They are not renamed to TikTok equivalents
# for Douyin (per design spec §11.2).
PROVIDER_TIKTOK_BUSINESS = "tiktok_business"
PROVIDER_TIKTOK_CREATOR = "tiktok_creator"
PROVIDER_DOUYIN = "douyin"


class TikTokBusinessAPI:
    """Constants for TikTok Business API v1.3 endpoints."""

    BASE_URL = "https://business-api.tiktok.com"
    VERSION = "v1.3"

    # Messaging endpoints
    SEND_MESSAGE = "/v1.3/message/send/"
    LIST_CONVERSATIONS = "/v1.3/message/conversation/list/"
    LIST_MESSAGES = "/v1.3/message/content/list/"
    UPLOAD_MEDIA = "/v1.3/message/media/upload/"
    DOWNLOAD_MEDIA = "/v1.3/message/media/download/"
    GET_CAPABILITIES = "/v1.3/message/capabilities/get/"

    # Webhook management
    WEBHOOK_UPDATE = "/v1.3/webhook/update/"
    WEBHOOK_LIST = "/v1.3/webhook/list/"
    WEBHOOK_DELETE = "/v1.3/webhook/delete/"

    # Auto messages
    AUTO_MESSAGE_CREATE = "/v1.3/message/auto_message/create/"
    AUTO_MESSAGE_UPDATE = "/v1.3/message/auto_message/update/"
    AUTO_MESSAGE_STATUS = "/v1.3/message/auto_message/status/update/"
    AUTO_MESSAGE_LIST = "/v1.3/message/auto_message/get/"
    AUTO_MESSAGE_DELETE = "/v1.3/message/auto_message/delete/"
    AUTO_MESSAGE_SORT = "/v1.3/message/auto_message/sort/"

    # Comment-to-message
    CTM_GET = "/v1.3/message/direct_reply/get/"
    CTM_UPDATE = "/v1.3/message/direct_reply/update/"

    # Organic / Accounts
    TOKEN_INFO = "/v1.3/tt_user/token_info/get/"
    BUSINESS_GET = "/v1.3/business/get/"
    VIDEO_LIST = "/v1.3/business/video/list/"
    VIDEO_SETTINGS = "/v1.3/business/video/settings/"
    COMMENT_LIST = "/v1.3/business/comment/list/"
    COMMENT_REPLY_LIST = "/v1.3/business/comment/reply/list/"
    COMMENT_CREATE = "/v1.3/business/comment/create/"
    COMMENT_REPLY_CREATE = "/v1.3/business/comment/reply/create/"
    COMMENT_IMAGE_UPLOAD = "/v1.3/business/comment/image/upload/"
    COMMENT_HIDE = "/v1.3/business/comment/hide/"
    COMMENT_DELETE = "/v1.3/business/comment/delete/"
    COMMENT_LIKE = "/v1.3/business/comment/like/"
    VIDEO_PUBLISH = "/v1.3/business/video/publish/"
    PHOTO_PUBLISH = "/v1.3/business/photo/publish/"
    PUBLISH_STATUS = "/v1.3/business/publish/status/"
    HASHTAG_SUGGEST = "/v1.3/business/hashtag/suggestion/"
    LOCATION_TAGS = "/v1.3/business/publish/location/"

    # Account webhook config
    ACCOUNT_WEBHOOK_CONFIG = "/v1.3/account/webhook/config/"

    # Conversation & folders
    CONVERSATION_LIST = "/v1.3/message/conversation/list/"
    CONVERSATION_GET = "/v1.3/message/conversation/get/"
    FOLDER_LIST = "/v1.3/message/folder/list/"
    FOLDER_CONVERSATIONS = "/v1.3/message/folder/conversation/list/"
    MESSAGE_LIST = "/v1.3/message/list/"
    MESSAGE_STATUS = "/v1.3/message/status/get/"
    ACCOUNT_IDENTITY = "/v1.3/tt_user/info/get/"

    # Creator posting (§10.3)
    CREATOR_POST = "/v1.3/business/video/create/"
    CREATOR_AUTH_URL = "https://open.tiktok.com/platform/auth/connect?"
    CREATOR_TOKEN = "/v1.3/business/token/oauth/token/"


# Module-level endpoint aliases (for client imports)
SEND_MESSAGE = TikTokBusinessAPI.SEND_MESSAGE
LIST_CONVERSATIONS = TikTokBusinessAPI.LIST_CONVERSATIONS
LIST_MESSAGES = TikTokBusinessAPI.LIST_MESSAGES
UPLOAD_MEDIA = TikTokBusinessAPI.UPLOAD_MEDIA
DOWNLOAD_MEDIA = TikTokBusinessAPI.DOWNLOAD_MEDIA
GET_CAPABILITIES = TikTokBusinessAPI.GET_CAPABILITIES
WEBHOOK_UPDATE = TikTokBusinessAPI.WEBHOOK_UPDATE
WEBHOOK_LIST = TikTokBusinessAPI.WEBHOOK_LIST
WEBHOOK_DELETE = TikTokBusinessAPI.WEBHOOK_DELETE
AUTO_MESSAGE_CREATE = TikTokBusinessAPI.AUTO_MESSAGE_CREATE
AUTO_MESSAGE_UPDATE = TikTokBusinessAPI.AUTO_MESSAGE_UPDATE
AUTO_MESSAGE_STATUS = TikTokBusinessAPI.AUTO_MESSAGE_STATUS
AUTO_MESSAGE_LIST = TikTokBusinessAPI.AUTO_MESSAGE_LIST
AUTO_MESSAGE_DELETE = TikTokBusinessAPI.AUTO_MESSAGE_DELETE
AUTO_MESSAGE_SORT = TikTokBusinessAPI.AUTO_MESSAGE_SORT
CTM_GET = TikTokBusinessAPI.CTM_GET
CTM_UPDATE = TikTokBusinessAPI.CTM_UPDATE
TOKEN_INFO = TikTokBusinessAPI.TOKEN_INFO
BUSINESS_GET = TikTokBusinessAPI.BUSINESS_GET
VIDEO_LIST = TikTokBusinessAPI.VIDEO_LIST
VIDEO_SETTINGS = TikTokBusinessAPI.VIDEO_SETTINGS
COMMENT_LIST = TikTokBusinessAPI.COMMENT_LIST
COMMENT_CREATE = TikTokBusinessAPI.COMMENT_CREATE
COMMENT_REPLY_CREATE = TikTokBusinessAPI.COMMENT_REPLY_CREATE
COMMENT_HIDE = TikTokBusinessAPI.COMMENT_HIDE
COMMENT_DELETE = TikTokBusinessAPI.COMMENT_DELETE
COMMENT_LIKE = TikTokBusinessAPI.COMMENT_LIKE
COMMENT_REPLY_LIST = TikTokBusinessAPI.COMMENT_REPLY_LIST
COMMENT_IMAGE_UPLOAD = TikTokBusinessAPI.COMMENT_IMAGE_UPLOAD
VIDEO_PUBLISH = TikTokBusinessAPI.VIDEO_PUBLISH
PHOTO_PUBLISH = TikTokBusinessAPI.PHOTO_PUBLISH
PUBLISH_STATUS = TikTokBusinessAPI.PUBLISH_STATUS
ACCOUNT_WEBHOOK_CONFIG = TikTokBusinessAPI.ACCOUNT_WEBHOOK_CONFIG
CONVERSATION_LIST = TikTokBusinessAPI.CONVERSATION_LIST
CONVERSATION_GET = TikTokBusinessAPI.CONVERSATION_GET
FOLDER_LIST = TikTokBusinessAPI.FOLDER_LIST
FOLDER_CONVERSATIONS = TikTokBusinessAPI.FOLDER_CONVERSATIONS
MESSAGE_LIST = TikTokBusinessAPI.MESSAGE_LIST
MESSAGE_STATUS = TikTokBusinessAPI.MESSAGE_STATUS
ACCOUNT_IDENTITY = TikTokBusinessAPI.ACCOUNT_IDENTITY
CREATOR_POST = TikTokBusinessAPI.CREATOR_POST
CREATOR_AUTH_URL = TikTokBusinessAPI.CREATOR_AUTH_URL
CREATOR_TOKEN = TikTokBusinessAPI.CREATOR_TOKEN


# (End of module-level endpoint aliases)



class TikTokScope(str, enum.Enum):
    """TikTok Business Messaging permission scopes.

    These are the exact scope names from TikTok's Business Messaging
    API documentation.  The plugin inspects actual granted scopes and
    activates only matching features.
    """

    READ = "business_messaging_read"
    SEND = "business_messaging_send"
    AUTO_MESSAGE_SETTING = "business_messaging_auto_message_setting"
    ACCOUNT_MANAGEMENT = "business_account_management"
    VIDEO_CREATE = "video_create"
    VIDEO_LIST = "video_list"
    COMMENT = "comment"
    HASHTAG = "hashtag"
    LOCATION_TAG = "location_tag"


# Scope → feature capability mapping
SCOPE_TO_FEATURE: Dict[str, str] = {
    TikTokScope.READ.value: "read",
    TikTokScope.SEND.value: "send",
    TikTokScope.AUTO_MESSAGE_SETTING.value: "auto_message",
    TikTokScope.ACCOUNT_MANAGEMENT.value: "account_management",
    TikTokScope.VIDEO_CREATE.value: "publish_video",
    TikTokScope.VIDEO_LIST.value: "list_posts",
    TikTokScope.COMMENT.value: "comments",
    TikTokScope.HASHTAG.value: "hashtags",
    TikTokScope.LOCATION_TAG.value: "location_tags",
}


class AutoMessageStatus(str, enum.Enum):
    """Auto-message status values (§4.4)."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


@dataclass(frozen=True)
class AutoMessageCreatePayload:
    """Payload for creating an auto-message (§4.4)."""

    name: str
    content: str
    status: AutoMessageStatus = AutoMessageStatus.ACTIVE
    priority: int = 0


@dataclass(frozen=True)
class ConversationCapability:
    """Capability snapshot for a conversation (design spec §6.5).

    The outbound path receives a capability decision, not a boolean
    buried inside adapter code.
    """

    provider: str
    account_alias: str
    conversation_id: str
    can_send: bool
    allowed_message_types: frozenset[str]
    max_messages_remaining: Optional[int]
    expires_at: Optional[datetime]
    source_event_id: Optional[str]
    reason_code: Optional[str]
    fetched_at: datetime


@dataclass(frozen=True)
class TikTokMessage:
    """A single normalized TikTok message."""

    message_id: str
    conversation_id: str
    sender_id: str
    sender_is_self: bool
    message_type: str  # "text", "image", "video", "unsupported"
    text: Optional[str]
    media_url: Optional[str]
    media_type: Optional[str]
    created_at: datetime
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class TikTokConversation:
    """A TikTok conversation."""

    conversation_id: str
    peer_id: str
    peer_display_name: str
    last_message_at: datetime
    message_count: int = 0
    capability: Optional[ConversationCapability] = None


@dataclass(frozen=True)
class AccountConfig:
    """Account-level configuration for a TikTok Business account."""

    provider: str
    profile: str
    account_alias: str
    provider_account_id: str
    access_token_secret: str
    webhook_secret: Optional[str] = None
    route_id: Optional[str] = None
    home_conversation: Optional[str] = None
    allowed_users: List[str] = field(default_factory=list)
    allow_all_users: bool = False
    manage_webhook: bool = False
    region: Optional[str] = None
    api_version: str = "v1.3"


def scope_set_from_token_info(token_info: dict) -> FrozenSet[str]:
    """Extract the set of granted scopes from a TikTok token info response.

    TikTok's /tt_user/token_info/get returns scopes as a list or
    space-separated string in the ``scope`` field.
    """
    raw = token_info.get("scope") or token_info.get("scopes") or ""
    if isinstance(raw, list):
        return frozenset(raw)
    if isinstance(raw, str):
        return frozenset(raw.split())
    return frozenset()


def capabilities_from_scopes(scopes: FrozenSet[str]) -> Dict[str, bool]:
    """Map granted scopes to feature capabilities."""
    result: Dict[str, bool] = {
        "read": False,
        "send": False,
        "auto_message": False,
        "account_management": False,
        "publish_video": False,
        "list_posts": False,
        "comments": False,
        "hashtags": False,
        "location_tags": False,
    }
    for scope in scopes:
        feature = SCOPE_TO_FEATURE.get(scope)
        if feature:
            result[feature] = True
    return result
