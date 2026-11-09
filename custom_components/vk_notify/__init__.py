"""
VK Notify __init__.py v1.0.0
Typed runtime_data, seamless migration of legacy single-chat entries to ConfigSubentry,
and Long Poll manager lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .api import VkApiClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_GROUP_ID,
    CONF_MODE,
    CONF_PEER_ID,
    CONF_VERIFY_SSL,
    CONF_VIDEO_ACCESS_TOKEN,
    DOMAIN,
    MODE_LONGPOLL,
)
from .longpoll import VkLongPollManager

PLATFORMS = [Platform.NOTIFY]


@dataclass
class VkNotifyRuntimeData:
    """Runtime data for a VK Notify community config entry."""

    client: VkApiClient
    peer_id: int | None
    group_id: int | None
    mode: str
    verify_ssl: bool
    video_access_token: str | None


VkNotifyConfigEntry = ConfigEntry[VkNotifyRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: VkNotifyConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("lp_managers", {})
    hass.data[DOMAIN][entry.entry_id] = {"data": entry.data}

    # Seamless migration: if this entry was created before subentries, convert its peer_id into a "chat" subentry
    legacy_peer_id = entry.data.get(CONF_PEER_ID)
    chat_subentries = [
        sub for sub in entry.subentries.values() if sub.subentry_type == "chat"
    ]
    if not chat_subentries and legacy_peer_id is not None:
        base_name = entry.data.get("name", "VK Notify")
        chat_title = entry.title or f"{base_name}: {legacy_peer_id}"
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(
                    {
                        CONF_PEER_ID: int(legacy_peer_id),
                        "name": base_name,
                    }
                ),
                subentry_type="chat",
                title=chat_title,
                unique_id=str(legacy_peer_id),
            ),
        )
        chat_subentries = [
            sub for sub in entry.subentries.values() if sub.subentry_type == "chat"
        ]

    # Migrate legacy entity registry entry (unique_id == entry.entry_id) to the corresponding subentry
    # so existing entity_ids (e.g. notify.vk_notify_2000000003) remain 100% unchanged.
    if chat_subentries:
        ent_reg = er.async_get(hass)
        first_sub = chat_subentries[0]
        first_peer = first_sub.data.get(CONF_PEER_ID, legacy_peer_id)
        for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            if ent.unique_id == entry.entry_id:
                ent_reg.async_update_entity(
                    ent.entity_id,
                    new_unique_id=f"{entry.entry_id}_{first_peer}",
                    config_subentry_id=first_sub.subentry_id,
                )
            elif ent.config_subentry_id is None:
                for sub in chat_subentries:
                    sub_peer = sub.data.get(CONF_PEER_ID)
                    if ent.unique_id == f"{entry.entry_id}_{sub_peer}":
                        ent_reg.async_update_entity(
                            ent.entity_id,
                            config_subentry_id=sub.subentry_id,
                        )
                        break

        # Clean up legacy root device if subentries now own their service devices
        dev_reg = dr.async_get(hass)
        if legacy_dev := dev_reg.async_get_device(identifiers={(DOMAIN, entry.entry_id)}):
            dev_reg.async_remove_device(legacy_dev.id)

    verify_ssl = entry.data.get(CONF_VERIFY_SSL, True)
    client = VkApiClient(hass, entry.data[CONF_ACCESS_TOKEN], verify_ssl=verify_ssl)
    group_id: int | None = entry.data.get(CONF_GROUP_ID)

    entry.runtime_data = VkNotifyRuntimeData(
        client=client,
        peer_id=legacy_peer_id,
        group_id=group_id,
        mode=entry.data.get(CONF_MODE, "api"),
        verify_ssl=verify_ssl,
        video_access_token=entry.data.get(CONF_VIDEO_ACCESS_TOKEN),
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if entry.data.get(CONF_MODE) == MODE_LONGPOLL and group_id:
        lp_managers: dict = hass.data[DOMAIN]["lp_managers"]
        if group_id not in lp_managers:
            manager = VkLongPollManager(
                hass,
                entry.data[CONF_ACCESS_TOKEN],
                group_id,
                verify_ssl=verify_ssl,
            )
            lp_managers[group_id] = {"manager": manager, "refcount": 1}
            manager.start()
        else:
            lp_managers[group_id]["refcount"] += 1

    return True


async def async_unload_entry(hass: HomeAssistant, entry: VkNotifyConfigEntry) -> bool:
    group_id: int | None = entry.data.get(CONF_GROUP_ID)
    if entry.data.get(CONF_MODE) == MODE_LONGPOLL and group_id:
        lp_managers: dict = hass.data[DOMAIN]["lp_managers"]
        if group_id in lp_managers:
            lp_managers[group_id]["refcount"] -= 1
            if lp_managers[group_id]["refcount"] <= 0:
                await lp_managers[group_id]["manager"].stop()
                del lp_managers[group_id]

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok

