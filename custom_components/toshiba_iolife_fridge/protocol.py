"""東芝 IoLIFE 冷蔵庫 (T_0008_2500_CA_8) のローカル通信.

Home Assistant に依存しない部分をここにまとめる。
認証と 5A5A パケットの暗号化は midea-local の LocalSecurity / PacketBuilder を使い、
その中身の電文だけ東芝形式 (55 AA CC 33) で組み立てる。

冷蔵庫は接続を保持していると、ドアの開閉 (function 0x22) や状態全体 (0x00) を
自分から送ってくる。FridgeConnection はその通知を受けつつ、定期的に問い合わせも送る。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import socket
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass

from midealocal.exceptions import MideaLocalError
from midealocal.packet_builder import PacketBuilder
from midealocal.security import (
    MSGTYPE_ENCRYPTED_REQUEST,
    MSGTYPE_HANDSHAKE_REQUEST,
    LocalSecurity,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 6444

FRAME_HEADER = b"\x55\xaa\xcc\x33"
FRAME_HEADER_LENGTH = 16
DEVICE_TYPE_REFRIGERATOR = 0xCA
MSG_TYPE_QUERY = 0x0003
FUNCTION_TYPE_BASE = 0x00
# 問い合わせへの応答の data type。自発通知は 0x04
DATA_TYPE_QUERY_RESPONSE = 0x03
FUNCTION_TYPE_DOOR_INFO = 0x22
FUNCTION_TYPE_ECO_INFO = 0x05
BASE_BODY_MIN_LENGTH = 16
DOOR_INFO_BODY_MIN_LENGTH = 2
DOOR_INFO_ALARM_INDEX = 2
ECO_INFO_BODY_MIN_LENGTH = 3

AUTH_RESPONSE_MIN_LENGTH = 72
HANDSHAKE_MAGIC = b"\x83\x70"
HANDSHAKE_HEADER_LENGTH = 8
ENCRYPTED_MESSAGE_MIN_LENGTH = 56
HEARTBEAT_PAYLOAD_TYPES = (0x0001, 0x1001)

ICE_MAKING = {0x00: "normal", 0x01: "quick", 0x02: "off"}
ICE_MAKING_STATUS = {
    0x00: "running",
    0x01: "water_shortage",
    0x02: "ice_full",
    0x03: "stop",
}
POWER_SAVING_MODE = {
    0x00: "normal",
    0x01: "power_saving_auto",
    0x02: "power_saving_auto_plus",
    0x03: "power_saving",
    0x04: "low_power_cooling",
}
COOLING = {
    0x00: "normal",
    0x01: "quick_freezing",
    0x06: "rough_heat_removal",
    0x07: "cool_cooking",
    0x08: "timer",
    0x09: "frozen_rice",
}
# アプリの「運転実績」の色分け (通常運転 / エコ運転 / 予冷運転) に対応
ECO_STATUS = {
    0x00: "normal",
    0x01: "eco",
    0x02: "precool",
    0x03: "auto_saving_off",
}
# 冷蔵室・冷凍室の温度設定 (5段階)。実機で弱=2、中=12、1段強=17、強=22 を確認 (1段弱=7 は5刻みから推定)。
# とってもエコ中や、チルドルームの氷結晶・Deli・解凍モード中は 0x80 (他のモードによる自動制御) になる。
TEMP_SETTING = {
    0x02: "weak",
    0x07: "slightly_weak",
    0x0C: "medium",
    0x11: "slightly_strong",
    0x16: "strong",
    0x80: "auto_controlled",
}
# チルドルーム: chilled_const (下位4bit) と chilled_temp (上位4bit, 2=解凍) を1つにまとめる
CHILLED_MODE = {0x00: "normal", 0x01: "ice_crystal", 0x02: "deli"}
CHILLED_MODE_THAWING = "thawing"
CHILLED_TEMP_THAWING = 0x02


class FridgeError(Exception):
    """通信全般のエラー."""


class FridgeAuthError(FridgeError):
    """token/key での認証に失敗した."""


class FridgeStaleError(FridgeError):
    """接続は生きているが、状態全体が一定時間届いていない."""


# midea-local が不正なデータで送出する例外 (DataSignDoesntMatch など) と、復号時の ValueError
LIBRARY_ERRORS: tuple[type[Exception], ...] = (MideaLocalError, ValueError)


@dataclass(frozen=True)
class FridgeStatus:
    """function type 0x00 (base) の状態."""

    ice_making: str | None
    ice_making_status: str | None
    power_saving_mode: str | None
    cooling: str | None
    refrigerator_setting: str | None
    freezer_setting: str | None
    chilled_mode: str | None
    uv_sterilization: bool
    ice_tray_cleaning: bool
    moisturizing: bool
    defrosting: bool
    door_chilling: bool
    door_freezing: bool
    door_ice: bool
    door_vegetable: bool
    door_freezing_upper: bool
    estimated_power: int
    # 直近24時間の消費電力量 (Wh)。問い合わせ応答でのみ有効で、通知では None
    estimated_daily_energy: int | None
    error_code: int | None
    outside_temperature: float | None
    raw_body: bytes
    # 以下は base には含まれず、ドア情報 (0x22) や節電情報 (0x05) の通知で更新する。
    # 通知をまだ受け取っていない間は None (不明) のままにする
    refrigerator_high_temp: bool | None = None
    freezer_high_temp: bool | None = None
    eco_status: str | None = None
    # かってにエコの通知を一度でも受け取ったか (未知の値で eco_status が None の場合も True)
    eco_status_received: bool = False


@dataclass(frozen=True)
class DoorInfo:
    """function type 0x22 (ドア情報) の通知."""

    door_chilling: bool
    door_vegetable: bool
    door_ice: bool
    door_freezing_upper: bool
    door_freezing: bool
    # Lua の chillingRoomTempThan12 / freezingRoomTempThan10。
    # 警告のバイトがない短い通知では None (前の値を保持する)
    refrigerator_high_temp: bool | None
    freezer_high_temp: bool | None


def crc16_ccitt(data: bytes) -> int:
    """CRC16-CCITT (初期値 0, 多項式 0x1021)."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def build_frame(body: bytes, msg_type: int) -> bytes:
    """Lua の assembleUart と同じ形式の電文を組み立てる."""
    length = FRAME_HEADER_LENGTH + len(body) + 2
    frame = bytearray(length)
    frame[0:4] = FRAME_HEADER
    frame[4:6] = (length - 4).to_bytes(2, "little")
    frame[6] = 0x01
    frame[7] = DEVICE_TYPE_REFRIGERATOR
    frame[14:16] = msg_type.to_bytes(2, "little")
    frame[FRAME_HEADER_LENGTH : FRAME_HEADER_LENGTH + len(body)] = body
    frame[-2:] = crc16_ccitt(bytes(frame[:-2])).to_bytes(2, "little")
    return bytes(frame)


def build_query_frame() -> bytes:
    """状態問い合わせの電文."""
    return build_frame(bytes([FUNCTION_TYPE_BASE]), MSG_TYPE_QUERY)


def extract_body(frame: bytes) -> tuple[int, bytes] | None:
    """応答電文から (data type, 本文) を取り出す. 東芝形式でなければ None."""
    if len(frame) < FRAME_HEADER_LENGTH + 3 or not frame.startswith(FRAME_HEADER):
        return None
    length = int.from_bytes(frame[4:6], "little") + 4
    if length > len(frame):
        return None
    frame = frame[:length]
    if crc16_ccitt(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        return None
    return frame[14], frame[FRAME_HEADER_LENGTH:-2]


def _signed16(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def parse_base_status(body: bytes) -> FridgeStatus:
    """function type 0x00 の本文を解析する.

    消費電力 [12..13] と電力量 [14..15] は Lua の記述 (ビッグエンディアン) と違い、
    実機ではリトルエンディアンで スマートプラグの実測と連動することを確認済み。
    電力量は「直近24時間の消費電力量」で毎時更新され、実測と ±10% 程度で一致する
    (0時リセットの当日値ではない)。消費電力は運転中に実測より大きめに出る。
    """
    if len(body) < BASE_BODY_MIN_LENGTH or body[0] != FUNCTION_TYPE_BASE:
        raise FridgeError(f"unexpected body: {body.hex()}")

    error_code = None
    if len(body) > 17 and (body[16], body[17]) != (0xFF, 0xFF):
        error_code = int.from_bytes(body[16:18], "little")

    outside_temperature = None
    if len(body) > 19:
        outside_temperature = _signed16(int.from_bytes(body[18:20], "little")) / 10

    return FridgeStatus(
        ice_making=ICE_MAKING.get(body[7] & 0x0F),
        ice_making_status=ICE_MAKING_STATUS.get(body[7] >> 4),
        power_saving_mode=POWER_SAVING_MODE.get(body[3]),
        cooling=COOLING.get(body[4]),
        refrigerator_setting=TEMP_SETTING.get(body[5]),
        freezer_setting=TEMP_SETTING.get(body[6]),
        chilled_mode=(
            CHILLED_MODE_THAWING
            if body[1] >> 4 == CHILLED_TEMP_THAWING
            else CHILLED_MODE.get(body[1] & 0x0F)
        ),
        uv_sterilization=bool(body[8] & 0x01),
        ice_tray_cleaning=bool(body[8] & 0x02),
        # アプリの「うるおい運転中」表示に対応
        moisturizing=bool(body[10] & 0x01),
        defrosting=bool(body[10] & 0x04),
        door_chilling=bool(body[11] & 0x01),
        door_freezing=bool(body[11] & 0x02),
        door_ice=bool(body[11] & 0x04),
        door_vegetable=bool(body[11] & 0x08),
        door_freezing_upper=bool(body[11] & 0x10),
        estimated_power=int.from_bytes(body[12:14], "little"),
        estimated_daily_energy=int.from_bytes(body[14:16], "little"),
        error_code=error_code,
        outside_temperature=outside_temperature,
        raw_body=bytes(body),
    )


def parse_door_info(body: bytes) -> DoorInfo:
    """function type 0x22 の本文を解析する.

    base (0x00) とはビットの並びが違う (野菜室が bit1、冷凍室上段が bit3、冷凍室下段が bit4)。
    """
    if len(body) < DOOR_INFO_BODY_MIN_LENGTH or body[0] != FUNCTION_TYPE_DOOR_INFO:
        raise FridgeError(f"unexpected body: {body.hex()}")
    bits = body[1]
    alarms = body[2] if len(body) > DOOR_INFO_ALARM_INDEX else None
    return DoorInfo(
        door_chilling=bool(bits & 0x01),
        door_vegetable=bool(bits & 0x02),
        door_ice=bool(bits & 0x04),
        door_freezing_upper=bool(bits & 0x08),
        door_freezing=bool(bits & 0x10),
        refrigerator_high_temp=None if alarms is None else bool(alarms & 0x01),
        freezer_high_temp=None if alarms is None else bool(alarms & 0x02),
    )


def parse_eco_status(body: bytes) -> str | None:
    """function type 0x05 (かってにエコの運転情報) から現在の運転状態を取り出す."""
    if len(body) < ECO_INFO_BODY_MIN_LENGTH or body[0] != FUNCTION_TYPE_ECO_INFO:
        raise FridgeError(f"unexpected body: {body.hex()}")
    return ECO_STATUS.get(body[2])


def _iter_bodies(security: LocalSecurity, messages: list) -> Iterator[tuple[int, bytes]]:
    """decode_8370 で分けたメッセージから東芝形式の (data type, 本文) を取り出す."""
    for message in messages:
        if message == b"ERROR":
            raise FridgeAuthError("device returned ERROR")
        payload_type = message[2] + (message[3] << 8)
        if payload_type in HEARTBEAT_PAYLOAD_TYPES:
            continue
        if len(message) <= ENCRYPTED_MESSAGE_MIN_LENGTH:
            continue
        frame = bytes(security.aes_decrypt(bytes(message[40:-16])))
        extracted = extract_body(frame)
        if extracted is not None:
            yield extracted


def _handshake_request(security: LocalSecurity, token: str) -> bytes:
    return security.encode_8370(bytes.fromhex(token), MSGTYPE_HANDSHAKE_REQUEST)


def split_handshake_response(buffer: bytes) -> tuple[bytes, bytes] | None:
    """認証応答1つ分がそろっていれば (応答, 後続データ) を返し、足りなければ None.

    応答は 8370 形式 (先頭 83 70、[2..3] がヘッダー8バイトを除いた長さ)。
    成功時は72バイト、拒否時は "ERROR" を含む13バイトで届く。TCP では分割されうる。
    """
    if len(buffer) < HANDSHAKE_HEADER_LENGTH:
        return None
    if buffer[:2] != HANDSHAKE_MAGIC:
        raise FridgeError(f"unexpected handshake response: {buffer[:16].hex()}")
    total = HANDSHAKE_HEADER_LENGTH + int.from_bytes(buffer[2:4], "big")
    if len(buffer) < total:
        return None
    return buffer[:total], buffer[total:]


def _finish_handshake(security: LocalSecurity, response: bytes, key: str) -> None:
    if len(response) < AUTH_RESPONSE_MIN_LENGTH:
        raise FridgeAuthError(f"auth rejected: {response.hex()}")
    try:
        security.tcp_key(response[8:AUTH_RESPONSE_MIN_LENGTH], bytes.fromhex(key))
    except LIBRARY_ERRORS as err:
        # 実機では、鍵が違うと長さは正常な応答が返り、ここで署名不一致になる
        raise FridgeAuthError(f"key rejected by refrigerator: {err!r}") from err


def _encrypted_packet(
    security: LocalSecurity, device_id: int, frame: bytes | None
) -> bytes:
    """frame=None ならハートビート."""
    if frame is None:
        packet = PacketBuilder(device_id, bytearray([0x00])).finalize(msg_type=0)
    else:
        packet = PacketBuilder(device_id, frame).finalize()
    return security.encode_8370(packet, MSGTYPE_ENCRYPTED_REQUEST)


def query_status(
    host: str,
    device_id: int,
    token: str,
    key: str,
    port: int = DEFAULT_PORT,
    timeout: float = 8.0,
) -> FridgeStatus:
    """認証して状態問い合わせを1回送り、応答を解析して返す (ブロッキング).

    timeout は接続から状態全体を受け取るまでの全体の上限。状態以外の通知が
    届き続けても、期限を過ぎたら FridgeError で戻る。
    """
    security = LocalSecurity()
    deadline = time.monotonic() + timeout

    def recv(sock: socket.socket, size: int) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("status query deadline exceeded")
        sock.settimeout(remaining)
        return sock.recv(size)

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(_handshake_request(security, token))
            buffer = b""
            while (split := split_handshake_response(buffer)) is None:
                chunk = recv(sock, 512)
                if not chunk:
                    raise FridgeError("connection closed during handshake")
                buffer += chunk
            response, buffer = split
            _finish_handshake(security, response, key)
            sock.sendall(_encrypted_packet(security, device_id, build_query_frame()))

            while True:
                if buffer:
                    messages, buffer = security.decode_8370(buffer)
                    for _, body in _iter_bodies(security, messages):
                        if body and body[0] == FUNCTION_TYPE_BASE:
                            return parse_base_status(body)
                chunk = recv(sock, 1024)
                if not chunk:
                    raise FridgeError("connection closed before status response")
                buffer += chunk
    except TimeoutError as err:
        raise FridgeError(f"timed out talking to {host}:{port}") from err
    except OSError as err:
        raise FridgeError(f"socket error with {host}:{port}: {err}") from err
    except LIBRARY_ERRORS as err:
        raise FridgeError(f"malformed data from {host}:{port}: {err!r}") from err


class FridgeConnection:
    """冷蔵庫との接続を保持し、通知と定期問い合わせの結果をコールバックで渡す.

    run() は切断されるたびに間隔を空けて繋ぎ直し続ける (キャンセルで終了)。
    """

    CONNECT_TIMEOUT = 10.0
    HEARTBEAT_INTERVAL = 10.0
    QUERY_INTERVAL = 60.0
    # ハートビートの応答も含めて、これだけ何も届かなければ切断とみなす
    RECEIVE_TIMEOUT = 45.0
    # ハートビートに応答していても、状態全体がこれだけ届かなければ古いとみなす
    STATUS_TIMEOUT = 150.0
    RETRY_DELAYS = (5, 10, 30, 60)

    def __init__(
        self,
        host: str,
        port: int,
        device_id: int,
        token: str,
        key: str,
        *,
        on_status: Callable[[FridgeStatus], None],
        on_door_info: Callable[[DoorInfo], None],
        on_eco_status: Callable[[str | None], None],
        on_disconnect: Callable[[Exception, int], None],
    ) -> None:
        self._host = host
        self._port = port
        self._device_id = device_id
        self._token = token
        self._key = key
        self._on_status = on_status
        self._on_door_info = on_door_info
        self._on_eco_status = on_eco_status
        self._on_disconnect = on_disconnect
        self._session_received = False
        self._last_status = 0.0

    async def run(self) -> None:
        failures = 0
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - どんな例外でも接続タスクは止めない
                if not isinstance(err, (FridgeError, OSError, *LIBRARY_ERRORS)):
                    _LOGGER.warning("unexpected error in refrigerator connection", exc_info=True)
                # 一度でも状態を受け取れたセッションなら、失敗回数を数え直す
                failures = 1 if self._session_received else failures + 1
                self._on_disconnect(err, failures)
                delay = self.RETRY_DELAYS[min(failures, len(self.RETRY_DELAYS)) - 1]
                _LOGGER.debug("connection lost (%r), retry in %ss", err, delay)
                await asyncio.sleep(delay)
            else:
                failures = 0

    async def _session(self) -> None:
        self._session_received = False
        security = LocalSecurity()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port), self.CONNECT_TIMEOUT
        )
        try:
            writer.write(_handshake_request(security, self._token))
            await writer.drain()
            response, buffer = await asyncio.wait_for(
                self._read_handshake(reader), self.CONNECT_TIMEOUT
            )
            _finish_handshake(security, response, self._key)

            now = time.monotonic()
            last_query = last_heartbeat = last_receive = self._last_status = now
            await self._send(writer, security, build_query_frame())
            while True:
                if buffer:
                    messages, buffer = security.decode_8370(buffer)
                    for data_type, body in _iter_bodies(security, messages):
                        self._dispatch(data_type, body)
                now = time.monotonic()
                if now - last_query >= self.QUERY_INTERVAL:
                    await self._send(writer, security, build_query_frame())
                    last_query = now
                if now - last_heartbeat >= self.HEARTBEAT_INTERVAL:
                    await self._send(writer, security, None)
                    last_heartbeat = now
                self.check_deadlines(now, last_receive)
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), 1.0)
                except TimeoutError:
                    continue
                if not chunk:
                    raise FridgeError("connection closed by refrigerator")
                last_receive = time.monotonic()
                buffer += chunk
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    def check_deadlines(self, now: float, last_receive: float) -> None:
        """受信の途絶と、状態全体の更新の途絶をそれぞれ判定する."""
        if now - last_receive >= self.RECEIVE_TIMEOUT:
            raise FridgeError("no data from refrigerator")
        if now - self._last_status >= self.STATUS_TIMEOUT:
            raise FridgeStaleError("no status response from refrigerator")

    @staticmethod
    async def _read_handshake(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
        buffer = b""
        while (split := split_handshake_response(buffer)) is None:
            chunk = await reader.read(512)
            if not chunk:
                raise FridgeError("connection closed during handshake")
            buffer += chunk
        return split

    async def _send(
        self, writer: asyncio.StreamWriter, security: LocalSecurity, frame: bytes | None
    ) -> None:
        writer.write(_encrypted_packet(security, self._device_id, frame))
        await writer.drain()

    def _dispatch(self, data_type: int, body: bytes) -> None:
        if not body:
            return
        function_type = body[0]
        if function_type == FUNCTION_TYPE_BASE and len(body) >= BASE_BODY_MIN_LENGTH:
            self._session_received = True
            self._last_status = time.monotonic()
            status = parse_base_status(body)
            if data_type != DATA_TYPE_QUERY_RESPONSE:
                # 自発通知の電力量 [14..15] は何時間〜何日も更新されない古い値で、
                # 問い合わせ応答の値 (直近24時間、毎時更新) と食い違うため使わない
                status = dataclasses.replace(status, estimated_daily_energy=None)
            self._on_status(status)
        elif function_type == FUNCTION_TYPE_DOOR_INFO:
            self._on_door_info(parse_door_info(body))
        elif function_type == FUNCTION_TYPE_ECO_INFO and len(body) >= ECO_INFO_BODY_MIN_LENGTH:
            self._on_eco_status(parse_eco_status(body))
        else:
            _LOGGER.debug(
                "ignored frame data_type=0x%02x function=0x%02x len=%s",
                data_type,
                function_type,
                len(body),
            )
