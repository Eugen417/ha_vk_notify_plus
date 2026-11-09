"""
VK Notify notify.py v1.0.0
Entity implementation using VkApiClient, modular services/formatting/media,
DeviceInfo(SERVICE), camera_entity support, and stored video_access_token fallback.
"""
from __future__ import annotations

import json
import random
from typing import Any

from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, ServiceResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import VkApiClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_GROUP_ID,
    CONF_PEER_ID,
    CONF_VERIFY_SSL,
    CONF_VIDEO_ACCESS_TOKEN,
    DOMAIN,
    VK_API_ACTIVITY,
    VK_API_DELETE,
    VK_API_EDIT,
    VK_API_EVENT_ANSWER,
    VK_API_MESSAGES_EDIT_CHAT,
    VK_API_PIN,
    VK_API_REACTION,
    VK_API_SEND,
    VK_API_UNPIN,
    VK_API_USERS_GET,
    VK_API_VERSION,
    VK_API_WALL,
)
from .formatting import build_vk_keyboard, parse_vk_formatting, redact_vk_payload
from .media import (
    async_upload_file,
    async_upload_files,
    async_upload_photo,
    async_upload_photos,
    async_upload_video,
)
from .services import async_register_vk_services


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    chat_subentries = [
        sub for sub in entry.subentries.values() if sub.subentry_type == "chat"
    ]
    if chat_subentries:
        for subentry in chat_subentries:
            async_add_entities(
                [VkNotifyEntity(hass, entry, subentry)],
                config_subentry_id=subentry.subentry_id,
            )
    elif CONF_PEER_ID in entry.data:
        async_add_entities([VkNotifyEntity(hass, entry, None)])

    platform = entity_platform.async_get_current_platform()
    async_register_vk_services(platform)


class VkNotifyEntity(NotifyEntity):
    _attr_supported_features = NotifyEntityFeature.TITLE

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        subentry: ConfigSubentry | None = None,
    ) -> None:
        self.hass, self._entry, self._subentry = hass, entry, subentry
        self._access_token: str = entry.data[CONF_ACCESS_TOKEN]
        self._peer_id: int = int(
            subentry.data[CONF_PEER_ID] if subentry else entry.data[CONF_PEER_ID]
        )
        self._verify_ssl: bool = entry.data.get(CONF_VERIFY_SSL, True)
        runtime = getattr(entry, "runtime_data", None)
        self._client: VkApiClient = (
            runtime.client
            if runtime and hasattr(runtime, "client")
            else VkApiClient(hass, self._access_token, verify_ssl=self._verify_ssl)
        )
        self._last_message_id: int | None = None
        self._last_cmid: int | None = None
        self._attr_unique_id = (
            f"{entry.entry_id}_{self._peer_id}" if subentry else entry.entry_id
        )
        base_name = (
            subentry.data.get("name") if subentry else None
        ) or entry.data.get("name", "VK Notify")
        self._attr_name = f"{base_name} {self._peer_id}"
        dev_id = subentry.subentry_id if subentry else entry.entry_id
        dev_name = subentry.title if subentry else (entry.title or self._attr_name)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dev_id)},
            name=dev_name,
            manufacturer="VK",
            model=f"VK Chat ({self._peer_id})",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "peer_id": self._peer_id,
            "last_message_id": self._last_message_id,
            "last_cmid": self._last_cmid,
        }

    async def _internal_send(
        self,
        endpoint: str,
        params: dict[str, Any],
        timeout: int = 60,
        token_override: str | None = None,
    ) -> ServiceResponse:
        if endpoint == VK_API_SEND:
            params.update(
                {"peer_ids": str(self._peer_id), "random_id": random.randint(0, 2**31 - 1)}
            )
            params.pop("peer_id", None)
        elif endpoint != VK_API_WALL:
            params.setdefault("peer_id", self._peer_id)

        response = await self._client.async_request(
            endpoint, params, token_override=token_override, timeout=timeout
        )
        if endpoint == VK_API_SEND and response is not None:
            msg = response[0] if isinstance(response, list) and response else response
            if isinstance(msg, dict):
                if "error" in msg and msg["error"]:
                    raise HomeAssistantError(
                        f"VK API messages.send error: {redact_vk_payload(msg['error'])}"
                    )
                self._last_message_id = msg.get("message_id") or msg.get("id")
                self._last_cmid = msg.get("conversation_message_id")
            else:
                self._last_message_id = msg
                self._last_cmid = None
            self.async_write_ha_state()
            return {
                "message_id": self._last_message_id,
                "conversation_message_id": self._last_cmid,
            }
        return response

    def _prepare_reply(self, params: dict[str, Any], reply_to: Any | None) -> None:
        if not reply_to:
            return
        reply_to_int = int(reply_to)
        if self._peer_id >= 2000000000:
            params["forward"] = json.dumps(
                {
                    "peer_id": self._peer_id,
                    "conversation_message_ids": [reply_to_int],
                    "is_reply": 1,
                },
                ensure_ascii=False,
            )
        else:
            params["reply_to"] = reply_to_int

    def _apply_common_params(self, params: dict[str, Any], kwargs: dict[str, Any]) -> None:
        if kwargs.get("disable_mentions"):
            params["disable_mentions"] = 1
        if "payload" in kwargs:
            params["payload"] = kwargs["payload"]
        if "keyboard" in kwargs:
            params["keyboard"] = build_vk_keyboard(
                kwargs["keyboard"],
                inline_keyboard=kwargs.get("inline_keyboard"),
                auto_answer_callback=bool(kwargs.get("auto_answer_callback", False)),
            )

    async def async_send_message(self, message: str, title: str | None = None, **kwargs) -> ServiceResponse:
        if title:
            message = f"{title}\n\n{message}"
        clean_msg, fmt_data = parse_vk_formatting(message, kwargs.get("parse_mode", "html"))
        params = {"message": clean_msg}
        if fmt_data:
            params["format_data"] = fmt_data
        if "attachment" in kwargs:
            params["attachment"] = kwargs["attachment"]
        if "template" in kwargs:
            params["template"] = json.dumps(kwargs["template"], ensure_ascii=False)
        if "lat" in kwargs:
            params["lat"], params["long"] = kwargs["lat"], kwargs.get("long", "")
        self._apply_common_params(params, kwargs)
        self._prepare_reply(params, kwargs.get("reply_to"))
        timeout = kwargs.get("timeout", 60)
        return await self._internal_send(VK_API_SEND, params, timeout=timeout)

    async def async_send_photo(self, **kwargs: Any) -> ServiceResponse:
        clean_msg, fmt_data = parse_vk_formatting(
            kwargs.get("message", ""), kwargs.get("parse_mode", "html")
        )
        params: dict[str, Any] = {"message": clean_msg}
        timeout = kwargs.get("timeout", 60)
        params["attachment"] = await async_upload_photos(
            self.hass,
            self._access_token,
            self._peer_id,
            url=kwargs.get("url"),
            filepath=kwargs.get("file"),
            camera_entity=kwargs.get("camera_entity"),
            verify_ssl=self._verify_ssl,
            timeout=timeout,
        )
        if fmt_data:
            params["format_data"] = fmt_data
        self._apply_common_params(params, kwargs)
        self._prepare_reply(params, kwargs.get("reply_to"))
        return await self._internal_send(VK_API_SEND, params, timeout=timeout)

    async def async_send_file(self, **kwargs: Any) -> ServiceResponse:
        clean_msg, fmt_data = parse_vk_formatting(
            kwargs.get("message", ""), kwargs.get("parse_mode", "html")
        )
        timeout = kwargs.get("timeout", 60)
        params: dict[str, Any] = {
            "message": clean_msg,
            "attachment": await async_upload_files(
                self.hass,
                self._access_token,
                self._peer_id,
                filepath=kwargs.get("file"),
                url=kwargs.get("url"),
                verify_ssl=self._verify_ssl,
                timeout=timeout,
            ),
        }
        if fmt_data:
            params["format_data"] = fmt_data
        self._apply_common_params(params, kwargs)
        self._prepare_reply(params, kwargs.get("reply_to"))
        return await self._internal_send(VK_API_SEND, params, timeout=timeout)

    async def async_send_video(self, **kwargs: Any) -> ServiceResponse:
        video_token = (
            kwargs.get("video_access_token")
            or self._entry.data.get(CONF_VIDEO_ACCESS_TOKEN)
            or ""
        ).strip()
        if not video_token:
            raise HomeAssistantError(
                "Для отправки видео укажите video_access_token в вызове службы или сохраните его в настройках интеграции VK Notify."
            )
        clean_msg, fmt_data = parse_vk_formatting(
            kwargs.get("message", ""), kwargs.get("parse_mode", "html")
        )
        params: dict[str, Any] = {"message": clean_msg}
        timeout = kwargs.get("timeout", 60)
        params["attachment"] = await async_upload_video(
            self.hass,
            self._access_token,
            self._peer_id,
            video_access_token=video_token,
            url=kwargs.get("url"),
            filepath=kwargs.get("file"),
            caption=clean_msg,
            verify_ssl=self._verify_ssl,
            timeout=timeout,
        )
        if fmt_data:
            params["format_data"] = fmt_data
        self._apply_common_params(params, kwargs)
        self._prepare_reply(params, kwargs.get("reply_to"))
        return await self._internal_send(VK_API_SEND, params, timeout=timeout)

    async def async_send_voice(self, **kwargs: Any) -> ServiceResponse:
        clean_msg, fmt_data = parse_vk_formatting(
            kwargs.get("message", ""), kwargs.get("parse_mode", "html")
        )
        timeout = kwargs.get("timeout", 60)
        params: dict[str, Any] = {
            "message": clean_msg,
            "attachment": await async_upload_file(
                self.hass,
                self._access_token,
                self._peer_id,
                filepath=kwargs.get("file"),
                verify_ssl=self._verify_ssl,
                timeout=timeout,
                force_voice=True,
            ),
        }
        if fmt_data:
            params["format_data"] = fmt_data
        self._apply_common_params(params, kwargs)
        self._prepare_reply(params, kwargs.get("reply_to"))
        return await self._internal_send(VK_API_SEND, params, timeout=timeout)

    async def async_edit_message(self, message: str, **kwargs: Any) -> None:
        clean_msg, fmt_data = parse_vk_formatting(message, kwargs.get("parse_mode", "html"))
        params: dict[str, Any] = {
            "message": clean_msg,
            "keep_forward_messages": 1,
            "keep_snippets": 1,
        }
        if fmt_data:
            params["format_data"] = fmt_data
        if "attachment" in kwargs:
            params["attachment"] = kwargs["attachment"]
        if "keyboard" in kwargs:
            params["keyboard"] = build_vk_keyboard(
                kwargs["keyboard"],
                inline_keyboard=kwargs.get("inline_keyboard"),
                auto_answer_callback=bool(kwargs.get("auto_answer_callback", False)),
            )
        if kwargs.get("message_id"):
            params["message_id"] = int(kwargs["message_id"])
        elif kwargs.get("conversation_message_id"):
            params["conversation_message_id"] = int(kwargs["conversation_message_id"])
        elif self._last_cmid:
            params["conversation_message_id"] = int(self._last_cmid)
        elif self._last_message_id:
            params["message_id"] = int(self._last_message_id)
        else:
            raise HomeAssistantError(
                "Для редактирования сообщения укажите conversation_message_id или message_id."
            )
        if kwargs.get("disable_mentions"):
            params["disable_mentions"] = 1
        await self._internal_send(VK_API_EDIT, params)

    async def async_wall_post(self, **kwargs: Any) -> ServiceResponse:
        group_id = (
            self._entry.data.get(CONF_GROUP_ID)
            or self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("data", {})
            .get(CONF_GROUP_ID)
        )
        if not group_id:
            raise HomeAssistantError(
                "Community group_id is not configured or could not be detected for wall.post"
            )
        clean_msg, _ = parse_vk_formatting(
            kwargs.get("message", ""), kwargs.get("parse_mode", "html")
        )
        params: dict[str, Any] = {
            "owner_id": f"-{group_id}",
            "from_group": 1 if kwargs.get("from_group", True) else 0,
            "message": clean_msg,
        }
        if kwargs.get("file") or kwargs.get("url"):
            params["attachments"] = await async_upload_files(
                self.hass,
                self._access_token,
                self._peer_id,
                filepath=kwargs.get("file"),
                url=kwargs.get("url"),
                verify_ssl=self._verify_ssl,
            )
        user_token = (self._entry.data.get(CONF_VIDEO_ACCESS_TOKEN) or "").strip() or None
        return await self._internal_send(
            VK_API_WALL, params, token_override=user_token
        )

    async def async_delete_message(self, **kwargs: Any) -> None:
        delete_for_all = 1 if kwargs.get("delete_for_all", True) else 0
        params: dict[str, Any] = {"delete_for_all": delete_for_all}
        if kwargs.get("message_id"):
            params["message_ids"] = int(kwargs["message_id"])
        elif kwargs.get("conversation_message_id"):
            params["cmids"] = int(kwargs["conversation_message_id"])
        elif self._last_cmid:
            params["cmids"] = int(self._last_cmid)
        elif self._last_message_id:
            params["message_ids"] = int(self._last_message_id)
        else:
            raise HomeAssistantError(
                "Для удаления сообщения укажите conversation_message_id или message_id."
            )
        await self._internal_send(VK_API_DELETE, params)

    async def async_send_reaction(
        self, conversation_message_id: Any, reaction_id: Any, **kwargs: Any
    ) -> None:
        cmid = int(conversation_message_id) if conversation_message_id else 0
        if cmid == 0:
            raise HomeAssistantError(
                "Cannot send reaction: conversation_message_id is missing or 0"
            )
        await self._internal_send(
            VK_API_REACTION, {"cmid": cmid, "reaction_id": int(reaction_id)}
        )

    async def async_pin_message(self, **kwargs: Any) -> None:
        params: dict[str, Any] = {}
        if kwargs.get("message_id"):
            params["message_id"] = int(kwargs["message_id"])
        elif kwargs.get("conversation_message_id"):
            params["conversation_message_id"] = int(kwargs["conversation_message_id"])
        elif self._last_cmid:
            params["conversation_message_id"] = int(self._last_cmid)
        elif self._last_message_id:
            params["message_id"] = int(self._last_message_id)
        await self._internal_send(VK_API_PIN, params)

    async def async_unpin_message(self, **kwargs) -> None:
        params = {}
        if kwargs.get("message_id"): params["message_id"] = int(kwargs["message_id"])
        elif kwargs.get("conversation_message_id"): params["conversation_message_id"] = int(kwargs["conversation_message_id"])
        await self._internal_send(VK_API_UNPIN, params)

    async def async_answer_callback(
        self, event_id: str, user_id: Any, message: str | None = None, **kwargs: Any
    ) -> None:
        params: dict[str, Any] = {"event_id": event_id, "user_id": int(user_id)}
        if message:
            params["event_data"] = json.dumps(
                {"type": "show_snackbar", "text": message[:90]}, ensure_ascii=False
            )
        try:
            await self._internal_send(VK_API_EVENT_ANSWER, params)
        except HomeAssistantError as err:
            if "invalid event_id" in str(err):
                return
            raise

    async def async_get_user_info(self, user_id: Any, **kwargs: Any) -> ServiceResponse:
        uid = str(user_id).replace("[VK ID: ", "").replace("]", "").strip()
        uid_int = int(uid) if uid.lstrip("-").isdigit() else 0
        if not uid_int or uid_int >= 2000000000:
            return {"full_name": "Система", "is_online": False}
        try:
            res = await self._client.async_request(
                VK_API_USERS_GET,
                {
                    "user_ids": str(uid_int),
                    "fields": "online,last_seen",
                    "lang": "ru",
                },
            )
            if isinstance(res, list) and res:
                u = res[0]
                return {
                    "full_name": f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(),
                    "is_online": bool(u.get("online", 0)),
                    "last_seen": u.get("last_seen", {}).get("time", 0),
                }
        except Exception:
            pass
        return {"full_name": "Неизвестно", "is_online": False, "last_seen": 0}

    async def async_send_sticker(self, sticker_id: Any, **kwargs) -> ServiceResponse:
        params = {"sticker_id": int(sticker_id)}
        self._prepare_reply(params, kwargs.get("reply_to"))
        return await self._internal_send(VK_API_SEND, params)

    async def async_set_activity(self, type: str, **kwargs) -> None:
        await self._internal_send(VK_API_ACTIVITY, {"type": type})

    async def async_edit_chat(self, title: str, **kwargs) -> None:
        if self._peer_id < 2000000000: raise HomeAssistantError("Only for group chats")
        await self._internal_send(VK_API_MESSAGES_EDIT_CHAT, {"chat_id": self._peer_id - 2000000000, "title": title})
