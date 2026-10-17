"""protocol.py の単体テスト. 実機 (GR-Y540XFS) の応答電文をそのまま使う.

実行: python -m pytest tests  (midea-local が必要。Home Assistant は不要)
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "toshiba_iolife_fridge"
    / "protocol.py"
)
_spec = importlib.util.spec_from_file_location("fridge_protocol", _PATH)
protocol = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = protocol
_spec.loader.exec_module(protocol)

# 2026-09-30 00:24 コンプレッサー運転中 (スマートプラグ実測 89.8W)
FRAME_RUNNING = bytes.fromhex(
    "55aacc334d0001ca00000000000003800000000200"
    "0c0c0000000000810000000000e000000e64646464"
    "0c190c0c001600063c63633263630000070000000000"
    "000000000000000000ffffffffffff98c3"
)
# 2026-09-30 02:15 給水不足の通知直後 (スマートプラグ実測 10.5W)
FRAME_WATER_SHORTAGE = bytes.fromhex(
    "55aacc334d0001ca00000000000003800000000200"
    "0c0c10000000000b00ee000000e400000e64646464"
    "0c190c0c001600063c63633263630000070000000000"
    "000000000000000000ffffffffffff4106"
)


def test_query_frame_matches_lua_output() -> None:
    assert protocol.build_query_frame().hex(" ") == (
        "55 aa cc 33 0f 00 01 ca 00 00 00 00 00 00 03 00 00 35 7c"
    )


def test_parse_running() -> None:
    data_type, body = protocol.extract_body(FRAME_RUNNING)
    assert data_type == 0x03
    status = protocol.parse_base_status(body)
    assert status.ice_making == "normal"
    assert status.ice_making_status == "running"
    assert status.power_saving_mode == "power_saving_auto_plus"
    assert status.refrigerator_setting == "medium"
    assert status.freezer_setting == "medium"
    assert status.cooling == "normal"
    assert not any(
        (
            status.door_chilling,
            status.door_freezing,
            status.door_ice,
            status.door_vegetable,
            status.door_freezing_upper,
        )
    )
    assert status.estimated_power == 129
    assert status.estimated_daily_energy == 0
    assert status.error_code == 0
    assert status.outside_temperature == pytest.approx(22.4)


def test_parse_chilled_and_flags() -> None:
    _, body = protocol.extract_body(FRAME_RUNNING)
    status = protocol.parse_base_status(body)
    assert status.chilled_mode == "normal"
    assert not status.uv_sterilization
    assert not status.ice_tray_cleaning
    assert not status.moisturizing

    edited = bytearray(body)
    edited[1] = 0x20  # chilled_temp=2 (解凍) は chilled_const より優先
    edited[8] = 0x03  # UV除菌 + 製氷皿お手入れ
    edited[10] = 0x01  # うるおい (霜取り状態 bit0)
    status = protocol.parse_base_status(bytes(edited))
    assert status.chilled_mode == "thawing"
    assert status.uv_sterilization
    assert status.ice_tray_cleaning
    assert status.moisturizing
    assert not status.defrosting

    edited[1] = 0x01
    assert protocol.parse_base_status(bytes(edited)).chilled_mode == "ice_crystal"


def test_settings_seen_on_device() -> None:
    # 2026-09-30 04:54〜04:56 にアプリで設定を変えたときの実機の値
    _, body = protocol.extract_body(FRAME_RUNNING)
    edited = bytearray(body)
    edited[5] = 0x11  # 冷蔵室を1段強く
    assert protocol.parse_base_status(bytes(edited)).refrigerator_setting == "slightly_strong"
    edited[3], edited[5], edited[6] = 0x04, 0x80, 0x80  # とってもエコ
    status = protocol.parse_base_status(bytes(edited))
    assert status.power_saving_mode == "low_power_cooling"
    assert status.refrigerator_setting == "auto_controlled"
    assert status.freezer_setting == "auto_controlled"
    edited[3] = 0x00  # 節電オフ
    assert protocol.parse_base_status(bytes(edited)).power_saving_mode == "normal"


def test_app_modes_seen_on_device() -> None:
    # 2026-09-30 16:09〜16:19 にアプリで各モードを切り替えたときの実機の値
    _, body = protocol.extract_body(FRAME_RUNNING)

    def with_bytes(**changes):
        edited = bytearray(body)
        for index, value in changes.items():
            edited[int(index[1:])] = value
        return protocol.parse_base_status(bytes(edited))

    assert with_bytes(b5=0x02).refrigerator_setting == "weak"
    assert with_bytes(b5=0x16).refrigerator_setting == "strong"
    assert with_bytes(b6=0x02).freezer_setting == "weak"
    assert with_bytes(b6=0x16).freezer_setting == "strong"
    assert with_bytes(b4=0x01).cooling == "quick_freezing"
    assert with_bytes(b4=0x09).cooling == "frozen_rice"
    assert with_bytes(b4=0x06).cooling == "rough_heat_removal"
    assert with_bytes(b4=0x08).cooling == "timer"
    ice_crystal = with_bytes(b1=0x01, b5=0x80)
    assert ice_crystal.chilled_mode == "ice_crystal"
    assert ice_crystal.refrigerator_setting == "auto_controlled"
    assert with_bytes(b1=0x02, b5=0x80).chilled_mode == "deli"
    assert with_bytes(b1=0x22, b5=0x80).chilled_mode == "thawing"
    assert with_bytes(b7=0x01).ice_making == "quick"
    assert with_bytes(b7=0x02).ice_making == "off"
    assert with_bytes(b8=0x01).uv_sterilization


def test_parse_water_shortage() -> None:
    _, body = protocol.extract_body(FRAME_WATER_SHORTAGE)
    status = protocol.parse_base_status(body)
    assert status.ice_making_status == "water_shortage"
    assert status.estimated_power == 11
    assert status.estimated_daily_energy == 238
    assert status.outside_temperature == pytest.approx(22.8)


def test_parse_door_info_push() -> None:
    # 2026-09-30 03:58 冷蔵室ドアを開けた瞬間に冷蔵庫から届いた通知 (data type 0x04)
    frame = protocol.build_frame(bytes.fromhex("220100000000000000"), 0x0004)
    data_type, body = protocol.extract_body(frame)
    assert data_type == 0x04
    doors = protocol.parse_door_info(body)
    assert doors.door_chilling
    assert not any(
        (doors.door_vegetable, doors.door_ice, doors.door_freezing_upper, doors.door_freezing)
    )
    closed = protocol.parse_door_info(bytes.fromhex("220000000000000000"))
    assert not closed.door_chilling


def test_door_info_bit_order_differs_from_base() -> None:
    # 0x22 は bit1 が野菜室、bit4 が冷凍室 (base は bit1 が冷凍室、bit3 が野菜室)
    doors = protocol.parse_door_info(bytes([0x22, 0x12]))
    assert doors.door_vegetable
    assert doors.door_freezing
    assert not doors.door_freezing_upper


def test_door_info_temperature_alarms() -> None:
    doors = protocol.parse_door_info(bytes([0x22, 0x00, 0x03]))
    assert doors.refrigerator_high_temp
    assert doors.freezer_high_temp
    quiet = protocol.parse_door_info(bytes.fromhex("220100000000000000"))
    assert not quiet.refrigerator_high_temp
    assert not quiet.freezer_high_temp


def test_parse_eco_status_push() -> None:
    # 2026-09-30 04:01 に冷蔵庫から届いたかってにエコの運転情報 (data type 0x04)
    body = bytes.fromhex("050300010 1e9fff7fff2fffbff0400ebff00".replace(" ", ""))
    assert protocol.parse_eco_status(body) == "normal"
    assert protocol.parse_eco_status(bytes([0x05, 0x01, 0x01])) == "eco"
    assert protocol.parse_eco_status(bytes([0x05, 0x01, 0x02])) == "precool"


def test_bad_crc_is_rejected() -> None:
    broken = FRAME_RUNNING[:-1] + bytes([FRAME_RUNNING[-1] ^ 0xFF])
    assert protocol.extract_body(broken) is None


def test_non_toshiba_frame_is_ignored() -> None:
    assert protocol.extract_body(bytes.fromhex("aa0bca00000000000003") + b"\x00" * 10) is None
