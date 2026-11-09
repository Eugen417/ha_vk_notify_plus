"""
VK Notify __init__.py v1.7.0
Typed runtime_data and Long Poll manager lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

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
    """Runtime data for a VK Notify config entry."""

    client: VkApiClient
    peer_id: int
    group_id: int | None
    mode: str
    verify_ssl: bool
    video_access_token: str | None


type VkNotifyConfigEntry = ConfigEntry[VkNotifyRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: VkNotifyConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("lp_managers", {})
    hass.data[DOMAIN][entry.entry_id] = {"data": entry.data}

    verify_ssl = entry.data.get(CONF_VERIFY_SSL, True)
    client = VkApiClient(hass, entry.data[CONF_ACCESS_TOKEN], verify_ssl=verify_ssl)
    group_id: int | None = entry.data.get(CONF_GROUP_ID)

    entry.runtime_data = VkNotifyRuntimeData(
        client=client,
        peer_id=entry.data[CONF_PEER_ID],
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

