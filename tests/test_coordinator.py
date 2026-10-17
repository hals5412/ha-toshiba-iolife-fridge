"""FridgeCoordinator の通知の扱いのテスト (コードレビューの指摘への対応).

Home Assistant 本体は使わず、coordinator が使う HA の部品だけを最小限の代替で差し替える。
実行: python -m pytest tests  (midea-local が必要)
"""

import asyncio
import dataclasses
import importlib
import sys
import types
from pathlib import Path
from typing import Generic, TypeVar

_T = TypeVar("_T")


class _StubCoordinator(Generic[_T]):
    """DataUpdateCoordinator のうち、統合が使う振る舞いだけを再現する."""

    def __init__(self, hass, logger, *, config_entry, name) -> None:
        self.hass = hass
        self.config_entry = config_entry
        self.data = None
        self.last_update_success = True
        self.published = []

    def async_set_updated_data(self, data) -> None:
        self.data = data
        self.last_update_success = True
        self.published.append(data)

    def async_set_update_error(self, err) -> None:
        self.last_update_success = False


class _StubCoordinatorEntity(Generic[_T]):
    """CoordinatorEntity の代わり (属性の定義だけ使う)."""


@dataclasses.dataclass(frozen=True, kw_only=True)
class _StubEntityDescription:
    key: str
    device_class: object = None
    entity_category: object = None


def _install_ha_stubs() -> None:
    modules = {
        "homeassistant.components": {},
        "homeassistant.components.binary_sensor": {
            "BinarySensorDeviceClass": types.SimpleNamespace(PROBLEM="problem", DOOR="door", RUNNING="running"),
            "BinarySensorEntity": type("BinarySensorEntity", (), {}),
            "BinarySensorEntityDescription": _StubEntityDescription,
        },
        "homeassistant.helpers.entity_platform": {"AddConfigEntryEntitiesCallback": object},
        "homeassistant.helpers.device_registry": {"DeviceInfo": dict},
        "homeassistant": {},
        "homeassistant.config_entries": {"ConfigEntry": type("ConfigEntry", (), {})},
        "homeassistant.const": {
            "CONF_HOST": "host",
            "CONF_PORT": "port",
            "EntityCategory": types.SimpleNamespace(DIAGNOSTIC="diagnostic"),
        },
        "homeassistant.core": {"HomeAssistant": object, "callback": lambda func: func},
        "homeassistant.exceptions": {"ConfigEntryNotReady": type("ConfigEntryNotReady", (Exception,), {})},
        "homeassistant.helpers": {},
        "homeassistant.helpers.update_coordinator": {
            "DataUpdateCoordinator": _StubCoordinator,
            "CoordinatorEntity": _StubCoordinatorEntity,
            "UpdateFailed": type("UpdateFailed", (Exception,), {}),
        },
    }
    for name, attrs in modules.items():
        module = sys.modules.get(name) or types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module


_install_ha_stubs()
_PACKAGE = "fridge_pkg"
_package = types.ModuleType(_PACKAGE)
_package.__path__ = [str(Path(__file__).parents[1] / "custom_components" / "toshiba_iolife_fridge")]
sys.modules[_PACKAGE] = _package  # __init__.py (HA 依存) は読み込まない
coordinator_module = importlib.import_module(f"{_PACKAGE}.coordinator")
binary_sensor_module = importlib.import_module(f"{_PACKAGE}.binary_sensor")
protocol = importlib.import_module(f"{_PACKAGE}.protocol")


def _coordinator():
    entry = types.SimpleNamespace(
        data={"host": "127.0.0.1", "port": 6444, "device_id": 1, "token": "00", "key": "00"}
    )
    return coordinator_module.FridgeCoordinator(object(), entry)


def _base(door_chilling: bool = False) -> "protocol.FridgeStatus":
    body = bytearray(63)
    body[11] = 0x01 if door_chilling else 0x00
    return protocol.parse_base_status(bytes(body))


def _door_info(door_chilling: bool, high: bool) -> "protocol.DoorInfo":
    return protocol.parse_door_info(bytes([0x22, 0x01 if door_chilling else 0x00, 0x03 if high else 0x00]))


def test_notifications_before_first_status_are_kept() -> None:
    async def scenario():
        coordinator = _coordinator()
        coordinator._handle_door_info(_door_info(door_chilling=True, high=True))
        coordinator._handle_eco_status("eco")
        assert coordinator.published == []
        coordinator._handle_status(_base(door_chilling=False))
        return coordinator

    coordinator = asyncio.run(scenario())
    data = coordinator.data
    assert data.refrigerator_high_temp is True
    assert data.freezer_high_temp is True
    assert data.eco_status == "eco"
    # ドアは状態全体にも含まれるので、後から届いた状態全体を優先する
    assert data.door_chilling is False


def test_later_status_carries_notification_only_values() -> None:
    async def scenario():
        coordinator = _coordinator()
        coordinator._handle_status(_base())
        coordinator._handle_door_info(_door_info(door_chilling=True, high=False))
        coordinator._handle_eco_status("precool")
        coordinator._handle_status(_base(door_chilling=True))
        return coordinator

    data = asyncio.run(scenario()).data
    assert data.refrigerator_high_temp is False
    assert data.eco_status == "precool"
    assert data.door_chilling is True


def test_partial_update_does_not_restore_availability() -> None:
    async def scenario():
        coordinator = _coordinator()
        coordinator._handle_status(_base())
        coordinator._handle_disconnect(protocol.FridgeStaleError("stale"), 1)
        assert coordinator.last_update_success is False
        published = len(coordinator.published)
        coordinator._handle_door_info(_door_info(door_chilling=False, high=True))
        assert coordinator.last_update_success is False
        assert len(coordinator.published) == published
        coordinator._handle_status(_base())
        return coordinator

    coordinator = asyncio.run(scenario())
    assert coordinator.last_update_success is True
    assert coordinator.data.refrigerator_high_temp is True


def test_status_fields_cover_non_base_fields() -> None:
    names = {field.name for field in dataclasses.fields(protocol.FridgeStatus)}
    assert set(coordinator_module.NON_BASE_FIELDS) <= names


def test_short_door_notification_keeps_alarms() -> None:
    """A door notification without the alarm byte must not clear the alarms."""

    async def scenario():
        coordinator = _coordinator()
        coordinator._handle_status(_base())
        coordinator._handle_door_info(_door_info(door_chilling=False, high=True))
        coordinator._handle_door_info(protocol.parse_door_info(bytes([0x22, 0x01])))
        return coordinator

    data = asyncio.run(scenario()).data
    assert data.door_chilling is True
    assert data.refrigerator_high_temp is True
    assert data.freezer_high_temp is True


def test_unknown_ice_status_is_unknown_in_binary_sensors() -> None:
    """An unknown ice status must not read as "no water shortage"."""
    body = bytearray(63)
    body[7] = 0xF0  # ice status 0x0F is not defined
    status = protocol.parse_base_status(bytes(body))
    assert status.ice_making_status is None
    sensors = {d.key: d for d in binary_sensor_module.BINARY_SENSORS}
    assert sensors["water_shortage"].value_fn(status) is None
    assert sensors["ice_full"].value_fn(status) is None

    body[7] = 0x10  # water shortage
    status = protocol.parse_base_status(bytes(body))
    assert sensors["water_shortage"].value_fn(status) is True
    assert sensors["ice_full"].value_fn(status) is False


def test_energy_only_updates_from_query_responses() -> None:
    """A pushed status keeps the energy from the last query response."""

    async def scenario():
        coordinator = _coordinator()
        query = dataclasses.replace(_base(), estimated_daily_energy=1403)
        coordinator._handle_status(query)
        pushed = dataclasses.replace(_base(door_chilling=True), estimated_daily_energy=None)
        coordinator._handle_status(pushed)
        return coordinator

    data = asyncio.run(scenario()).data
    assert data.estimated_daily_energy == 1403
    assert data.door_chilling is True


def test_unknown_eco_notification_is_marked_as_received() -> None:
    async def scenario():
        coordinator = _coordinator()
        coordinator._handle_status(_base())
        assert coordinator.data.eco_status_received is False
        coordinator._handle_eco_status("precool")
        coordinator._handle_eco_status(None)
        coordinator._handle_status(_base())
        return coordinator

    data = asyncio.run(scenario()).data
    assert data.eco_status is None
    assert data.eco_status_received is True
