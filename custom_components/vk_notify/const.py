"""
VK Notify const.py v1.0.0
Single source of truth for constants and VK API endpoints.
"""
from __future__ import annotations

DOMAIN = "vk_notify"

CONF_ACCESS_TOKEN = "access_token"
CONF_VIDEO_ACCESS_TOKEN = "video_access_token"
CONF_PEER_ID = "peer_id"
CONF_MODE = "mode"
CONF_GROUP_ID = "group_id"
CONF_VERIFY_SSL = "verify_ssl"

MODE_API = "api"
MODE_LONGPOLL = "longpoll"

VK_API_VERSION = "5.199"
VK_API_BASE = "https://api.vk.com/method"

VK_API_URL = f"{VK_API_BASE}/messages.send"
VK_API_SEND = VK_API_URL
VK_API_EDIT = f"{VK_API_BASE}/messages.edit"
VK_API_DELETE = f"{VK_API_BASE}/messages.delete"
VK_API_ACTIVITY = f"{VK_API_BASE}/messages.setActivity"
VK_API_REACTION = f"{VK_API_BASE}/messages.sendReaction"
VK_API_PIN = f"{VK_API_BASE}/messages.pin"
VK_API_UNPIN = f"{VK_API_BASE}/messages.unpin"
VK_API_EVENT_ANSWER = f"{VK_API_BASE}/messages.sendMessageEventAnswer"
VK_API_MESSAGES_EDIT_CHAT = f"{VK_API_BASE}/messages.editChat"
VK_API_MARK_AS_READ = f"{VK_API_BASE}/messages.markAsRead"
VK_API_CONVERSATIONS = f"{VK_API_BASE}/messages.getConversations"

VK_API_PHOTO_UPLOAD_SERVER = f"{VK_API_BASE}/photos.getMessagesUploadServer"
VK_API_PHOTO_SAVE = f"{VK_API_BASE}/photos.saveMessagesPhoto"
VK_API_DOC_UPLOAD_SERVER = f"{VK_API_BASE}/docs.getMessagesUploadServer"
VK_API_DOC_SAVE = f"{VK_API_BASE}/docs.save"
VK_API_VIDEO_SAVE = f"{VK_API_BASE}/video.save"
VK_API_WALL_POST = f"{VK_API_BASE}/wall.post"
VK_API_WALL = VK_API_WALL_POST
VK_API_LONGPOLL_SERVER = f"{VK_API_BASE}/groups.getLongPollServer"
VK_API_GROUPS_GET_BY_ID = f"{VK_API_BASE}/groups.getById"
VK_API_USERS_GET = f"{VK_API_BASE}/users.get"

# Transient VK API error codes eligible for automatic retry:
# 6 = Too many requests per second, 9 = Flood control, 10 = Internal server error
VK_RETRYABLE_ERROR_CODES = {6, 9, 10}

