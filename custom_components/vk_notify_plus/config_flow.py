"""
VK Notify config_flow.py v1.0.0
Added: Optional video_access_token storage (masked password field) and mode switching in reconfigure.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_GROUP_ID,
    CONF_MODE,
    CONF_PEER_ID,
    CONF_VERIFY_SSL,
    CONF_VIDEO_ACCESS_TOKEN,
    DOMAIN,
    MODE_API,
    MODE_LONGPOLL,
    VK_API_CONVERSATIONS,
    VK_API_GROUPS_GET_BY_ID,
    VK_API_LONGPOLL_SERVER,
    VK_API_USERS_GET,
    VK_API_VERSION,
)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCESS_TOKEN): PASSWORD_SELECTOR,
        vol.Optional("name", default="VK Notify Plus"): str,
        vol.Optional(CONF_VIDEO_ACCESS_TOKEN, default=""): PASSWORD_SELECTOR,
        vol.Optional(CONF_VERIFY_SSL, default=True): bool,
    }
)


async def _detect_group_id(
    hass, access_token: str, verify_ssl: bool = True
) -> int | None:
    """Try to auto-detect group_id from a community token via groups.getById."""
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    try:
        async with session.get(
            VK_API_GROUPS_GET_BY_ID,
            params={"access_token": access_token, "v": VK_API_VERSION},
        ) as resp:
            data = await resp.json(content_type=None)
        resp_obj = data.get("response")
        groups = (
            resp_obj.get("groups", []) if isinstance(resp_obj, dict) else (resp_obj or [])
        )
        if groups and isinstance(groups, list):
            return groups[0]["id"]
    except Exception:
        pass
    return None


async def _check_longpoll_access(
    hass, access_token: str, group_id: int, verify_ssl: bool = True
) -> bool:
    """Return True if the token can call groups.getLongPollServer."""
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    try:
        async with session.get(
            VK_API_LONGPOLL_SERVER,
            params={
                "access_token": access_token,
                "group_id": group_id,
                "v": VK_API_VERSION,
            },
        ) as resp:
            data = await resp.json(content_type=None)
        return "error" not in data
    except Exception:
        return False


async def _validate_token(
    hass, access_token: str, verify_ssl: bool = True
) -> bool:
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    try:
        async with session.get(
            VK_API_USERS_GET,
            params={"access_token": access_token, "v": VK_API_VERSION},
        ) as resp:
            data = await resp.json(content_type=None)
            return "error" not in data
    except Exception:
        return False

def _build_conversations(response: dict) -> tuple[dict[str, str], set[str]]:
    profiles = {p["id"]: f"{p['first_name']} {p['last_name']}" for p in response.get("profiles", [])}
    groups = {g["id"]: g["name"] for g in response.get("groups", [])}

    all_options: dict[str, str] = {}
    writable_ids: set[str] = set()

    for item in response.get("items", []):
        conv = item["conversation"]
        peer = conv["peer"]
        peer_id: int = peer["id"]
        peer_type: str = peer["type"]
        can_write: bool = conv.get("can_write", {}).get("allowed", True)

        if peer_type == "chat":
            name = conv.get("chat_settings", {}).get("title", f"Chat {peer_id}")
        elif peer_type == "user":
            name = profiles.get(peer_id, str(peer_id))
        elif peer_type == "group":
            name = groups.get(abs(peer_id), str(peer_id))
        else:
            name = str(peer_id)

        key = str(peer_id)
        label = f"{name} ({peer_id})" if can_write else f"⛔ {name} ({peer_id})"
        all_options[key] = label
        if can_write:
            writable_ids.add(key)

    return all_options, writable_ids

async def _get_conversations(hass, access_token: str, verify_ssl: bool = True) -> tuple[dict[str, str], set[str]]:
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    async with session.get(
        VK_API_CONVERSATIONS,
        params={"access_token": access_token, "extended": 1, "count": 200, "v": VK_API_VERSION},
    ) as resp:
        data = await resp.json(content_type=None)

    if "error" in data:
        return {}, set()

    return _build_conversations(data["response"])

class VkNotifyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._access_token: str = ""
        self._video_access_token: str = ""
        self._name: str = "VK Notify Plus"
        self._mode: str = MODE_API
        self._group_id: int | None = None
        self._verify_ssl: bool = True

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            verify_ssl = user_input.get(CONF_VERIFY_SSL, True)
            try:
                valid = await _validate_token(
                    self.hass, user_input[CONF_ACCESS_TOKEN], verify_ssl=verify_ssl
                )
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                if not valid:
                    errors["base"] = "invalid_token"
                else:
                    self._access_token = user_input[CONF_ACCESS_TOKEN].strip()
                    self._video_access_token = (
                        user_input.get(CONF_VIDEO_ACCESS_TOKEN) or ""
                    ).strip()
                    self._name = user_input.get("name", "VK Notify Plus")
                    self._verify_ssl = verify_ssl
                    self._group_id = await _detect_group_id(
                        self.hass, self._access_token, verify_ssl=verify_ssl
                    )
                    return await self.async_step_select_mode()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={
                "docs_url": "https://github.com/Eugen417/vk_notify_plus/blob/main/docs/SETUP_GUIDE.md"
            },
        )

    async def async_step_select_mode(self, user_input=None):
        errors = {}

        if user_input is not None:
            mode = user_input[CONF_MODE]

            if mode == MODE_LONGPOLL and not self._group_id:
                errors["base"] = "group_id_required"
            elif mode == MODE_LONGPOLL:
                ok = await _check_longpoll_access(
                    self.hass, self._access_token, self._group_id, verify_ssl=self._verify_ssl
                )
                if not ok:
                    errors["base"] = "longpoll_no_access"
                else:
                    self._mode = mode
                    return await self.async_step_select_chat()
            else:
                self._mode = mode
                return await self.async_step_select_chat()

        schema = vol.Schema(
            {
                vol.Required(CONF_MODE, default=MODE_API): SelectSelector(
                    SelectSelectorConfig(
                        options=[MODE_API, MODE_LONGPOLL],
                        mode=SelectSelectorMode.LIST,
                        translation_key="mode",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="select_mode",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_select_chat(self, user_input=None):
        errors = {}
        all_options, writable_ids = await _get_conversations(
            self.hass, self._access_token, verify_ssl=self._verify_ssl
        )

        if user_input is not None:
            peer_id_str = str(user_input[CONF_PEER_ID]).strip()
            if not peer_id_str.lstrip("-").isdigit():
                errors["base"] = "chat_not_writable"
            elif peer_id_str in all_options and peer_id_str not in writable_ids:
                errors["base"] = "chat_not_writable"
            else:
                chat_label = all_options.get(peer_id_str, f"Чат {peer_id_str}")
                card_title = f"{self._name}: {chat_label}"

                entry_data = {
                    CONF_ACCESS_TOKEN: self._access_token,
                    CONF_PEER_ID: int(peer_id_str),
                    CONF_MODE: self._mode,
                    CONF_GROUP_ID: self._group_id,
                    "name": self._name,
                    CONF_VERIFY_SSL: self._verify_ssl,
                }
                if self._video_access_token:
                    entry_data[CONF_VIDEO_ACCESS_TOKEN] = self._video_access_token

                return self.async_create_entry(
                    title=card_title,
                    data=entry_data,
                )

        options = [{"value": str(k), "label": v} for k, v in all_options.items()]

        return self.async_show_form(
            step_id="select_chat",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PEER_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            custom_value=True,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        self._access_token = entry.data[CONF_ACCESS_TOKEN]
        current_verify = entry.data.get(CONF_VERIFY_SSL, True)
        current_mode = entry.data.get(CONF_MODE, MODE_API)
        current_video_token = entry.data.get(CONF_VIDEO_ACCESS_TOKEN, "")

        errors = {}
        all_options, writable_ids = await _get_conversations(
            self.hass, self._access_token, verify_ssl=current_verify
        )

        if user_input is not None:
            peer_id_str = str(user_input[CONF_PEER_ID]).strip()
            new_verify = user_input.get(CONF_VERIFY_SSL, True)
            new_mode = user_input.get(CONF_MODE, current_mode)
            new_video_token = (user_input.get(CONF_VIDEO_ACCESS_TOKEN) or "").strip()
            group_id = entry.data.get(CONF_GROUP_ID) or await _detect_group_id(
                self.hass, self._access_token, verify_ssl=new_verify
            )

            if not peer_id_str.lstrip("-").isdigit():
                errors["base"] = "chat_not_writable"
            elif peer_id_str in all_options and peer_id_str not in writable_ids:
                errors["base"] = "chat_not_writable"
            elif new_mode == MODE_LONGPOLL and not group_id:
                errors["base"] = "group_id_required"
            elif new_mode == MODE_LONGPOLL and not await _check_longpoll_access(
                self.hass, self._access_token, group_id, verify_ssl=new_verify
            ):
                errors["base"] = "longpoll_no_access"
            else:
                chat_label = all_options.get(peer_id_str, f"Чат {peer_id_str}")
                base_name = entry.data.get("name", "VK Notify Plus")
                updated_data = {
                    **entry.data,
                    CONF_PEER_ID: int(peer_id_str),
                    CONF_MODE: new_mode,
                    CONF_GROUP_ID: group_id,
                    CONF_VERIFY_SSL: new_verify,
                }
                if new_video_token:
                    updated_data[CONF_VIDEO_ACCESS_TOKEN] = new_video_token
                else:
                    updated_data.pop(CONF_VIDEO_ACCESS_TOKEN, None)

                return self.async_update_reload_and_abort(
                    entry,
                    title=f"{base_name}: {chat_label}",
                    data=updated_data,
                )

        options = [{"value": str(k), "label": v} for k, v in all_options.items()]

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PEER_ID, default=str(entry.data[CONF_PEER_ID])
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            custom_value=True,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(CONF_MODE, default=current_mode): SelectSelector(
                        SelectSelectorConfig(
                            options=[MODE_API, MODE_LONGPOLL],
                            mode=SelectSelectorMode.LIST,
                            translation_key="mode",
                        )
                    ),
                    vol.Optional(
                        CONF_VIDEO_ACCESS_TOKEN, default=current_video_token
                    ): PASSWORD_SELECTOR,
                    vol.Optional(CONF_VERIFY_SSL, default=current_verify): bool,
                }
            ),
            errors=errors,
        )
