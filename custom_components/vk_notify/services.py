"""
VK Notify services.py v1.7.0
Voluptuous schemas and entity service registration for VK Notify.
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant.core import SupportsResponse
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import EntityPlatform

INT_STR = vol.Any(cv.positive_int, cv.string)
STR_OR_LIST = vol.Any(cv.string, [cv.string])
PARSE_MODE_VALIDATOR = vol.In(["html", "markdown", "markdownv2", "plain"])

COMMON_FIELDS = {
    vol.Optional("disable_mentions"): cv.boolean,
    vol.Optional("payload"): cv.string,
    vol.Optional("keyboard"): dict,
    vol.Optional("inline_keyboard"): cv.boolean,
    vol.Optional("auto_answer_callback", default=True): cv.boolean,
    vol.Optional("reply_to"): INT_STR,
    vol.Optional("timeout", default=60): cv.positive_int,
    vol.Optional("parse_mode", default="html"): PARSE_MODE_VALIDATOR,
}

SCHEMA_SEND_MESSAGE = {
    vol.Required("message"): cv.string,
    vol.Optional("title"): cv.string,
    vol.Optional("attachment"): cv.string,
    vol.Optional("template"): dict,
    vol.Optional("lat"): cv.string,
    vol.Optional("long"): cv.string,
    **COMMON_FIELDS,
}

SCHEMA_SEND_PHOTO = {
    vol.Optional("url"): STR_OR_LIST,
    vol.Optional("file"): STR_OR_LIST,
    vol.Optional("camera_entity"): cv.entity_ids,
    vol.Optional("message"): cv.string,
    **COMMON_FIELDS,
}

SCHEMA_SEND_FILE = {
    vol.Optional("file"): STR_OR_LIST,
    vol.Optional("url"): STR_OR_LIST,
    vol.Optional("message"): cv.string,
    **COMMON_FIELDS,
}

SCHEMA_SEND_VIDEO = {
    vol.Optional("video_access_token"): cv.string,
    vol.Optional("url"): cv.string,
    vol.Optional("file"): cv.string,
    vol.Optional("message"): cv.string,
    **COMMON_FIELDS,
}

SCHEMA_SEND_VOICE = {
    vol.Required("file"): cv.string,
    vol.Optional("message"): cv.string,
    **COMMON_FIELDS,
}

SCHEMA_EDIT_MESSAGE = {
    vol.Required("message"): cv.string,
    vol.Optional("message_id"): INT_STR,
    vol.Optional("conversation_message_id"): INT_STR,
    vol.Optional("attachment"): cv.string,
    vol.Optional("keyboard"): dict,
    vol.Optional("inline_keyboard"): cv.boolean,
    vol.Optional("auto_answer_callback", default=True): cv.boolean,
    vol.Optional("disable_mentions"): cv.boolean,
    vol.Optional("parse_mode", default="html"): PARSE_MODE_VALIDATOR,
}

SCHEMA_WALL_POST = {
    vol.Optional("message"): cv.string,
    vol.Optional("file"): STR_OR_LIST,
    vol.Optional("url"): STR_OR_LIST,
    vol.Optional("from_group", default=True): cv.boolean,
}

SCHEMA_DELETE_MESSAGE = {
    vol.Optional("message_id"): INT_STR,
    vol.Optional("conversation_message_id"): INT_STR,
}

SCHEMA_SET_ACTIVITY = {
    vol.Required("type"): vol.In(["typing", "audiomsg"]),
}

SCHEMA_SEND_REACTION = {
    vol.Required("conversation_message_id"): INT_STR,
    vol.Required("reaction_id"): INT_STR,
}

SCHEMA_PIN_UNPIN = {
    vol.Optional("message_id"): INT_STR,
    vol.Optional("conversation_message_id"): INT_STR,
}

SCHEMA_SEND_STICKER = {
    vol.Required("sticker_id"): INT_STR,
    vol.Optional("reply_to"): INT_STR,
}

SCHEMA_EDIT_CHAT = {
    vol.Required("title"): cv.string,
}

SCHEMA_GET_USER_INFO = {
    vol.Required("user_id"): INT_STR,
}

SCHEMA_ANSWER_CALLBACK = {
    vol.Required("event_id"): cv.string,
    vol.Required("user_id"): INT_STR,
    vol.Optional("message"): cv.string,
}


def async_register_vk_services(platform: EntityPlatform) -> None:
    """Регистрирует все 16 сервисов сущности VkNotifyEntity."""
    platform.async_register_entity_service(
        "send_message",
        SCHEMA_SEND_MESSAGE,
        "async_send_message",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        "send_photo",
        SCHEMA_SEND_PHOTO,
        "async_send_photo",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        "send_file",
        SCHEMA_SEND_FILE,
        "async_send_file",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        "send_video",
        SCHEMA_SEND_VIDEO,
        "async_send_video",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        "send_voice",
        SCHEMA_SEND_VOICE,
        "async_send_voice",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        "edit_message",
        SCHEMA_EDIT_MESSAGE,
        "async_edit_message",
    )
    platform.async_register_entity_service(
        "wall_post",
        SCHEMA_WALL_POST,
        "async_wall_post",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        "delete_message",
        SCHEMA_DELETE_MESSAGE,
        "async_delete_message",
    )
    platform.async_register_entity_service(
        "set_activity",
        SCHEMA_SET_ACTIVITY,
        "async_set_activity",
    )
    platform.async_register_entity_service(
        "send_reaction",
        SCHEMA_SEND_REACTION,
        "async_send_reaction",
    )
    platform.async_register_entity_service(
        "pin_message",
        SCHEMA_PIN_UNPIN,
        "async_pin_message",
    )
    platform.async_register_entity_service(
        "unpin_message",
        SCHEMA_PIN_UNPIN,
        "async_unpin_message",
    )
    platform.async_register_entity_service(
        "send_sticker",
        SCHEMA_SEND_STICKER,
        "async_send_sticker",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        "edit_chat",
        SCHEMA_EDIT_CHAT,
        "async_edit_chat",
    )
    platform.async_register_entity_service(
        "get_user_info",
        SCHEMA_GET_USER_INFO,
        "async_get_user_info",
        supports_response=SupportsResponse.ONLY,
    )
    platform.async_register_entity_service(
        "answer_callback",
        SCHEMA_ANSWER_CALLBACK,
        "async_answer_callback",
    )
