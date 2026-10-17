"""東芝 IoLIFE 冷蔵庫 (ローカル) 統合."""

from __future__ import annotations

import re

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_MODEL
from .coordinator import FridgeConfigEntry, FridgeCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: FridgeConfigEntry) -> bool:
    # 0.1.0 は機種名の代わりに SN 由来の内部コード ("0000000D" など) を保存していた
    model = entry.data.get(CONF_MODEL)
    if model and re.fullmatch(r"[0-9A-Fa-f]{8}", model):
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_MODEL: None})

    coordinator = FridgeCoordinator(hass, entry)
    await coordinator.async_start()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FridgeConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
