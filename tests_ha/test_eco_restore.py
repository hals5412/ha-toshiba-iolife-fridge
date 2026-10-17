"""かってにエコの運転状態の復元値のテスト."""

from unittest.mock import MagicMock

from custom_components.toshiba_iolife_fridge.const import CONF_DEVICE_ID
from custom_components.toshiba_iolife_fridge.protocol import FridgeStatus
from custom_components.toshiba_iolife_fridge.sensor import SENSORS, FridgeRestoreSensor

ECO = next(desc for desc in SENSORS if desc.key == "eco_status")
_EMPTY = {name: None for name in FridgeStatus.__dataclass_fields__}


def _sensor(**changes) -> FridgeRestoreSensor:
    coordinator = MagicMock()
    coordinator.data = FridgeStatus(**{**_EMPTY, **changes})
    coordinator.config_entry.data = {CONF_DEVICE_ID: 1234}
    coordinator.config_entry.title = "Fridge"
    sensor = FridgeRestoreSensor(coordinator, ECO)
    sensor._restored = "eco"  # 再起動前の値
    return sensor


def test_restored_value_is_used_until_first_notification() -> None:
    assert _sensor(eco_status_received=False).native_value == "eco"


def test_notification_replaces_restored_value() -> None:
    sensor = _sensor(eco_status="precool", eco_status_received=True)
    assert sensor.native_value == "precool"


def test_unknown_value_after_notification_does_not_bring_back_restored_value() -> None:
    sensor = _sensor(eco_status=None, eco_status_received=True)
    assert sensor.native_value is None
