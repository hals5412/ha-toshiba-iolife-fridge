"""センサー."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import FridgeConfigEntry
from .entity import FridgeEntity
from .protocol import (
    CHILLED_MODE,
    CHILLED_MODE_THAWING,
    COOLING,
    ECO_STATUS,
    ICE_MAKING,
    ICE_MAKING_STATUS,
    POWER_SAVING_MODE,
    TEMP_SETTING,
    FridgeStatus,
)


@dataclass(frozen=True, kw_only=True)
class FridgeSensorDescription(SensorEntityDescription):
    value_fn: Callable[[FridgeStatus], str | int | float | None]


SENSORS: tuple[FridgeSensorDescription, ...] = (
    FridgeSensorDescription(
        key="ice_making_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(ICE_MAKING_STATUS.values()),
        value_fn=lambda s: s.ice_making_status,
    ),
    FridgeSensorDescription(
        key="ice_making",
        device_class=SensorDeviceClass.ENUM,
        options=list(ICE_MAKING.values()),
        value_fn=lambda s: s.ice_making,
    ),
    FridgeSensorDescription(
        key="power_saving_mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(POWER_SAVING_MODE.values()),
        value_fn=lambda s: s.power_saving_mode,
    ),
    FridgeSensorDescription(
        key="cooling",
        device_class=SensorDeviceClass.ENUM,
        options=list(COOLING.values()),
        value_fn=lambda s: s.cooling,
    ),
    FridgeSensorDescription(
        key="eco_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(ECO_STATUS.values()),
        value_fn=lambda s: s.eco_status,
    ),
    FridgeSensorDescription(
        key="refrigerator_setting",
        device_class=SensorDeviceClass.ENUM,
        options=list(TEMP_SETTING.values()),
        value_fn=lambda s: s.refrigerator_setting,
    ),
    FridgeSensorDescription(
        key="freezer_setting",
        device_class=SensorDeviceClass.ENUM,
        options=list(TEMP_SETTING.values()),
        value_fn=lambda s: s.freezer_setting,
    ),
    FridgeSensorDescription(
        key="chilled_mode",
        device_class=SensorDeviceClass.ENUM,
        options=[*CHILLED_MODE.values(), CHILLED_MODE_THAWING],
        value_fn=lambda s: s.chilled_mode,
    ),
    FridgeSensorDescription(
        key="outside_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda s: s.outside_temperature,
    ),
    # 冷蔵庫自身の推定値。実測 (スマートプラグ) より大きめに出るので既定では無効
    FridgeSensorDescription(
        key="estimated_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.estimated_power,
    ),
    FridgeSensorDescription(
        key="estimated_daily_energy",
        device_class=SensorDeviceClass.ENERGY,
        # 直近24時間の値で増減するので、積算 (TOTAL_INCREASING) の長期統計にはしない
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.estimated_daily_energy,
    ),
    FridgeSensorDescription(
        key="error_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.error_code,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        (FridgeRestoreSensor if desc.key in RESTORED_KEYS else FridgeSensor)(
            coordinator, desc
        )
        for desc in SENSORS
    )


class FridgeSensor(FridgeEntity, SensorEntity):
    entity_description: FridgeSensorDescription

    def __init__(self, coordinator, description: FridgeSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | int | float | None:
        return self.entity_description.value_fn(self.coordinator.data)


# 問い合わせでは取れず、冷蔵庫からの通知でしか届かない値
RESTORED_KEYS = {"eco_status"}


class FridgeRestoreSensor(FridgeSensor, RestoreEntity):
    """通知が届くまでは、再起動前の最後の値を表示する."""

    _restored: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in (self.entity_description.options or []):
            self._restored = last.state

    @property
    def native_value(self) -> str | int | float | None:
        value = super().native_value
        return self._restored if value is None else value
