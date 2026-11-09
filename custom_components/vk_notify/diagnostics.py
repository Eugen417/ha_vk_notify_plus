"""Diagnostics support for VK Notify (Home Assistant 2026.11+)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_GROUP_ID,
    CONF_MODE,
    CONF_PEER_ID,
    CONF_VERIFY_SSL,
    DOMAIN,
    VK_API_VERSION,
)

TO_REDACT = {
    CONF_ACCESS_TOKEN,
    "video_access_token",
    "token",
    "key",
    "hash",
    "secret",
}


def _mask_id(value: int | str | None) -> str | None:
    """Partially masks numeric VK IDs (peer_id, group_id) for safe sharing."""
    if value is None:
        return None
    s = str(value).strip()
    if len(s) <= 4:
        return "***"
    if s.startswith("2000"):
        return f"2000***{s[-2:]}"
    return f"{s[:2]}***{s[-2:]}"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry with all sensitive data redacted."""
    redacted_data = dict(async_redact_data(entry.data, TO_REDACT))
    peer_id = entry.data.get(CONF_PEER_ID)
    group_id = entry.data.get(CONF_GROUP_ID)

    if CONF_PEER_ID in redacted_data:
        redacted_data[CONF_PEER_ID] = _mask_id(peer_id)
    if CONF_GROUP_ID in redacted_data:
        redacted_data[CONF_GROUP_ID] = _mask_id(group_id)

    is_group_chat = isinstance(peer_id, int) and peer_id >= 2000000000

    lp_managers: dict = hass.data.get(DOMAIN, {}).get("lp_managers", {})
    lp_info = lp_managers.get(group_id) if group_id else None
    lp_manager = lp_info.get("manager") if isinstance(lp_info, dict) else None
    lp_task = getattr(lp_manager, "_task", None) if lp_manager else None

    ent_reg = er.async_get(hass)
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    entity_states = []
    for ent in entities:
        state = hass.states.get(ent.entity_id)
        entity_states.append(
            {
                "entity_id": ent.entity_id,
                "disabled": ent.disabled,
                "state": state.state if state else None,
                "has_last_message_id": bool(
                    state and state.attributes.get("last_message_id")
                ),
                "has_last_cmid": bool(state and state.attributes.get("last_cmid")),
            }
        )

    subentries_diag = [
        {
            "subentry_id": sub.subentry_id,
            "subentry_type": sub.subentry_type,
            "title": sub.title,
            "peer_id": _mask_id(sub.data.get(CONF_PEER_ID)),
        }
        for sub in entry.subentries.values()
    ]

    return {
        "integration": {
            "domain": DOMAIN,
            "vk_api_version": VK_API_VERSION,
            "mode": entry.data.get(CONF_MODE),
            "verify_ssl": entry.data.get(CONF_VERIFY_SSL, True),
            "chat_type": "group_chat" if is_group_chat else "direct_or_community",
            "chats_count": len(subentries_diag) or (1 if peer_id else 0),
        },
        "config_entry": {
            "entry_id": entry.entry_id,
            "version": entry.version,
            "data": redacted_data,
            "options": async_redact_data(entry.options, TO_REDACT),
            "subentries": subentries_diag,
        },
        "longpoll": {
            "configured": entry.data.get(CONF_MODE) == "longpoll",
            "manager_registered": bool(lp_info),
            "refcount": lp_info.get("refcount", 0) if isinstance(lp_info, dict) else 0,
            "task_running": bool(lp_task and not lp_task.done()),
        },
        "entities": entity_states,
    }
