"""共通エンティティ."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_ID, CONF_MODEL, DOMAIN, MANUFACTURER
from .coordinator import FridgeCoordinator


class FridgeEntity(CoordinatorEntity[FridgeCoordinator]):
    """冷蔵庫1台ぶんのデバイスにぶら下がるエンティティ."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FridgeCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        device_id = entry.data[CONF_DEVICE_ID]
        self._attr_translation_key = key
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(device_id))},
            manufacturer=MANUFACTURER,
            model=entry.data.get(CONF_MODEL),
            name=entry.title,
        )
