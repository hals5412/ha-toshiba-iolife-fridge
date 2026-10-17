"""バイナリセンサー."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import FridgeConfigEntry
from .entity import FridgeEntity
from .protocol import FridgeStatus


@dataclass(frozen=True, kw_only=True)
class FridgeBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[FridgeStatus], bool | None]


def _ice_status_is(status: FridgeStatus, value: str) -> bool | None:
    """製氷状態が未知の値 (None) なら、警告の有無も不明として返す."""
    if status.ice_making_status is None:
        return None
    return status.ice_making_status == value


BINARY_SENSORS: tuple[FridgeBinarySensorDescription, ...] = (
    FridgeBinarySensorDescription(
        key="water_shortage",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda s: _ice_status_is(s, "water_shortage"),
    ),
    FridgeBinarySensorDescription(
        key="ice_full",
        value_fn=lambda s: _ice_status_is(s, "ice_full"),
    ),
    FridgeBinarySensorDescription(
        key="door_chilling",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda s: s.door_chilling,
    ),
    FridgeBinarySensorDescription(
        key="door_freezing",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda s: s.door_freezing,
    ),
    FridgeBinarySensorDescription(
        key="door_ice",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda s: s.door_ice,
    ),
    FridgeBinarySensorDescription(
        key="door_vegetable",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda s: s.door_vegetable,
    ),
    FridgeBinarySensorDescription(
        key="door_freezing_upper",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda s: s.door_freezing_upper,
    ),
    # ドア情報の通知に含まれる警告。実際に高温になったときの動きは未確認
    FridgeBinarySensorDescription(
        key="refrigerator_high_temp",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda s: s.refrigerator_high_temp,
    ),
    FridgeBinarySensorDescription(
        key="freezer_high_temp",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda s: s.freezer_high_temp,
    ),
    FridgeBinarySensorDescription(
        key="moisturizing",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda s: s.moisturizing,
    ),
    FridgeBinarySensorDescription(
        key="uv_sterilization",
        value_fn=lambda s: s.uv_sterilization,
    ),
    FridgeBinarySensorDescription(
        key="ice_tray_cleaning",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda s: s.ice_tray_cleaning,
    ),
    FridgeBinarySensorDescription(
        key="defrosting",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.defrosting,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(FridgeBinarySensor(coordinator, desc) for desc in BINARY_SENSORS)


class FridgeBinarySensor(FridgeEntity, BinarySensorEntity):
    entity_description: FridgeBinarySensorDescription

    def __init__(self, coordinator, description: FridgeBinarySensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data)
