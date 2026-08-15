"""Schema definitions for bytedance-ops tools."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Tool schemas for the bytedance-ops plugin.
# These define the Hermes tool interface — they are registered with
# the tools registry and exposed to the agent.

TIKTOK_ACCOUNT_GET_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tiktok_account_get",
        "description": "Read TikTok Business Account profile and granted scopes.",
        "parameters": {
            "type": "object",
            "properties": {
                "account_alias": {
                    "type": "string",
                    "description": "Local account alias to inspect",
                },
            },
            "required": ["account_alias"],
        },
    },
}

TIKTOK_POSTS_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tiktok_posts_list",
        "description": "List posts on a TikTok Business Account.",
        "parameters": {
            "type": "object",
            "properties": {
                "account_alias": {"type": "string"},
                "cursor": {"type": "string", "description": "Pagination cursor"},
                "page_size": {"type": "integer", "default": 20, "maximum": 100},
            },
            "required": ["account_alias"],
        },
    },
}

TIKTOK_COMMENTS_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tiktok_comments_list",
        "description": "List comments on a TikTok post.",
        "parameters": {
            "type": "object",
            "properties": {
                "account_alias": {"type": "string"},
                "video_id": {"type": "string", "description": "Post video ID"},
                "cursor": {"type": "string"},
                "page_size": {"type": "integer", "default": 50, "maximum": 100},
            },
            "required": ["account_alias", "video_id"],
        },
    },
}

TIKTOK_COMMENT_REPLY_PREPARE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tiktok_comment_reply_prepare",
        "description": "Build an immutable comment reply intent (no provider side effect).",
        "parameters": {
            "type": "object",
            "properties": {
                "account_alias": {"type": "string"},
                "comment_id": {"type": "string"},
                "text": {"type": "string", "description": "Reply text"},
            },
            "required": ["account_alias", "comment_id", "text"],
        },
    },
}

TIKTOK_COMMENT_REPLY_COMMIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tiktok_comment_reply_commit",
        "description": "Publish an approved comment reply.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent_id": {"type": "string", "description": "Approval token from prepare"},
            },
            "required": ["intent_id"],
        },
    },
}

TIKTOK_COMMENT_MODERATE_PREPARE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tiktok_comment_moderate_prepare",
        "description": "Prepare a comment moderation action (hide/delete/like).",
        "parameters": {
            "type": "object",
            "properties": {
                "account_alias": {"type": "string"},
                "comment_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["hide", "unhide", "delete", "like", "unlike"],
                },
            },
            "required": ["account_alias", "comment_id", "action"],
        },
    },
}

TIKTOK_COMMENT_MODERATE_COMMIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tiktok_comment_moderate_commit",
        "description": "Apply an approved comment moderation action.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent_id": {"type": "string"},
            },
            "required": ["intent_id"],
        },
    },
}

TIKTOK_POST_PREPARE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tiktok_post_prepare",
        "description": "Validate and preview a TikTok Business post (no provider side effect).",
        "parameters": {
            "type": "object",
            "properties": {
                "account_alias": {"type": "string"},
                "video_path": {"type": "string", "description": "Local path to video file"},
                "caption": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "privacy": {"type": "string", "enum": ["PUBLIC", "PRIVATE"]},
                "commercial_content": {
                    "type": "boolean",
                    "description": "Whether this is paid/promotional content",
                },
            },
            "required": ["account_alias", "video_path", "caption"],
        },
    },
}

TIKTOK_POST_COMMIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tiktok_post_commit",
        "description": "Publish an approved TikTok post.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent_id": {"type": "string"},
            },
            "required": ["intent_id"],
        },
    },
}

TIKTOK_PUBLISH_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tiktok_publish_status",
        "description": "Check the publish status of a TikTok post.",
        "parameters": {
            "type": "object",
            "properties": {
                "account_alias": {"type": "string"},
                "publish_id": {"type": "string"},
            },
            "required": ["account_alias", "publish_id"],
        },
    },
}

TIKTOK_CREATOR_CONNECT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tiktok_creator_connect",
        "description": "Start OAuth flow for TikTok creator-authorized posting (Login Kit).",
        "parameters": {
            "type": "object",
            "properties": {
                "account_alias": {"type": "string"},
                "redirect_uri": {"type": "string"},
            },
            "required": ["account_alias", "redirect_uri"],
        },
    },
}

TIKTOK_CREATOR_POST_PREPARE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tiktok_creator_post_prepare",
        "description": "Prepare a creator-authorized TikTok post (no provider side effect).",
        "parameters": {
            "type": "object",
            "properties": {
                "account_alias": {"type": "string"},
                "video_path": {"type": "string"},
                "caption": {"type": "string"},
                "privacy": {"type": "string", "enum": ["PUBLIC", "PRIVATE", "FRIENDS"]},
                "comments": {"type": "boolean"},
                "duet": {"type": "boolean"},
                "stitch": {"type": "boolean"},
                "commercial_content": {"type": "boolean"},
            },
            "required": ["account_alias", "video_path", "caption", "privacy"],
        },
    },
}

TIKTOK_CREATOR_POST_COMMIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tiktok_creator_post_commit",
        "description": "Commit a creator-approved TikTok post.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent_id": {"type": "string"},
            },
            "required": ["intent_id"],
        },
    },
}

DOUYIN_ACCOUNT_GET_SCHEMA = {
    "type": "function",
    "function": {
        "name": "douyin_account_get",
        "description": "Read Douyin account info and granted scopes.",
        "parameters": {
            "type": "object",
            "properties": {
                "account_alias": {"type": "string"},
            },
            "required": ["account_alias"],
        },
    },
}

DOUYIN_SCOPES_GET_SCHEMA = {
    "type": "function",
    "function": {
        "name": "douyin_scopes_get",
        "description": "Inspect granted scopes for a Douyin account.",
        "parameters": {
            "type": "object",
            "properties": {
                "account_alias": {"type": "string"},
            },
            "required": ["account_alias"],
        },
    },
}

DOUYIN_CONTENT_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "douyin_content_list",
        "description": "List authorized Douyin content.",
        "parameters": {
            "type": "object",
            "properties": {
                "account_alias": {"type": "string"},
                "cursor": {"type": "string"},
            },
            "required": ["account_alias"],
        },
    },
}

DOUYIN_MESSAGE_CAPABILITY_GET_SCHEMA = {
    "type": "function",
    "function": {
        "name": "douyin_message_capability_get",
        "description": "Inspect Douyin send-grant eligibility for a conversation.",
        "parameters": {
            "type": "object",
            "properties": {
                "account_alias": {"type": "string"},
                "conversation_short_id": {"type": "string"},
            },
            "required": ["account_alias", "conversation_short_id"],
        },
    },
}

ALL_SCHEMAS: List[Dict[str, Any]] = [
    TIKTOK_ACCOUNT_GET_SCHEMA,
    TIKTOK_POSTS_LIST_SCHEMA,
    TIKTOK_COMMENTS_LIST_SCHEMA,
    TIKTOK_COMMENT_REPLY_PREPARE_SCHEMA,
    TIKTOK_COMMENT_REPLY_COMMIT_SCHEMA,
    TIKTOK_COMMENT_MODERATE_PREPARE_SCHEMA,
    TIKTOK_COMMENT_MODERATE_COMMIT_SCHEMA,
    TIKTOK_POST_PREPARE_SCHEMA,
    TIKTOK_POST_COMMIT_SCHEMA,
    TIKTOK_PUBLISH_STATUS_SCHEMA,
    TIKTOK_CREATOR_CONNECT_SCHEMA,
    TIKTOK_CREATOR_POST_PREPARE_SCHEMA,
    TIKTOK_CREATOR_POST_COMMIT_SCHEMA,
    DOUYIN_ACCOUNT_GET_SCHEMA,
    DOUYIN_SCOPES_GET_SCHEMA,
    DOUYIN_CONTENT_LIST_SCHEMA,
    DOUYIN_MESSAGE_CAPABILITY_GET_SCHEMA,
]

# Classify tools by side effect (§10.1 MCP safety boundary)
READ_ONLY_TOOLS = frozenset({
    "tiktok_account_get",
    "tiktok_posts_list",
    "tiktok_comments_list",
    "tiktok_publish_status",
    "douyin_account_get",
    "douyin_scopes_get",
    "douyin_content_list",
    "douyin_message_capability_get",
})

PREPARE_TOOLS = frozenset({
    "tiktok_comment_reply_prepare",
    "tiktok_comment_moderate_prepare",
    "tiktok_post_prepare",
    "tiktok_creator_post_prepare",
})

COMMIT_TOOLS = frozenset({
    "tiktok_comment_reply_commit",
    "tiktok_comment_moderate_commit",
    "tiktok_post_commit",
    "tiktok_creator_post_commit",
})

MUTATING_TOOLS = frozenset({
    "tiktok_creator_connect",
})
