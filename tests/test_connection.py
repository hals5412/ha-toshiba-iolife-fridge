"""接続まわりの異常系テスト (コードレビューの指摘への対応).

実行: python -m pytest tests  (midea-local が必要。Home Assistant は不要)
"""

import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest
from midealocal.exceptions import DataSignDoesntMatch

_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "toshiba_iolife_fridge"
    / "protocol.py"
)
_spec = importlib.util.spec_from_file_location("fridge_protocol_conn", _PATH)
protocol = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = protocol
_spec.loader.exec_module(protocol)

# 実機の成功応答と同じ形 (83 70 + 長さ0x40 + 64バイト) と、鍵の不一致で返る拒否応答
AUTH_OK = bytes.fromhex("83700040" "2001") + bytes(2) + os.urandom(64)
AUTH_REJECTED = bytes.fromhex("83700005200f20004552524f52")


def test_handshake_split_is_reassembled() -> None:
    assert protocol.split_handshake_response(AUTH_OK[:36]) is None
    assert protocol.split_handshake_response(AUTH_OK[:5]) is None
    response, rest = protocol.split_handshake_response(AUTH_OK + b"next")
    assert response == AUTH_OK
    assert rest == b"next"


def test_short_rejection_is_still_auth_error() -> None:
    response, rest = protocol.split_handshake_response(AUTH_REJECTED)
    assert rest == b""
    with pytest.raises(protocol.FridgeAuthError):
        protocol._finish_handshake(protocol.LocalSecurity(), response, "00" * 32)


def test_non_8370_handshake_is_rejected() -> None:
    with pytest.raises(protocol.FridgeError):
        protocol.split_handshake_response(b"\x5a\x5a" + bytes(10))


class _FakeReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def test_async_handshake_reads_until_complete() -> None:
    reader = _FakeReader([AUTH_OK[:36], AUTH_OK[36:] + b"tail"])
    response, rest = asyncio.run(protocol.FridgeConnection._read_handshake(reader))
    assert response == AUTH_OK
    assert rest == b"tail"


def test_async_handshake_detects_close() -> None:
    reader = _FakeReader([AUTH_OK[:36]])
    with pytest.raises(protocol.FridgeError):
        asyncio.run(protocol.FridgeConnection._read_handshake(reader))


def _connection(**overrides) -> "protocol.FridgeConnection":
    callbacks = {
        "on_status": lambda _s: None,
        "on_door_info": lambda _d: None,
        "on_eco_status": lambda _e: None,
        "on_disconnect": lambda _e, _n: None,
    }
    callbacks.update(overrides)
    return protocol.FridgeConnection("127.0.0.1", 6444, 1, "00", "00", **callbacks)


def test_library_error_does_not_stop_reconnect_loop() -> None:
    disconnects: list[tuple[Exception, int]] = []
    conn = _connection(on_disconnect=lambda e, n: disconnects.append((e, n)))
    conn.RETRY_DELAYS = (0,)
    calls = 0

    async def fake_session() -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise DataSignDoesntMatch
        raise asyncio.CancelledError

    conn._session = fake_session
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(conn.run())
    assert calls == 3
    assert [n for _, n in disconnects] == [1, 2]
    assert all(isinstance(e, DataSignDoesntMatch) for e, _ in disconnects)


def test_unexpected_error_does_not_stop_reconnect_loop() -> None:
    disconnects = []
    conn = _connection(on_disconnect=lambda e, n: disconnects.append(n))
    conn.RETRY_DELAYS = (0,)
    calls = 0

    async def fake_session() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyError("boom")
        raise asyncio.CancelledError

    conn._session = fake_session
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(conn.run())
    assert disconnects == [1]


def test_heartbeat_only_session_becomes_stale() -> None:
    conn = _connection()
    conn._last_status = 1000.0
    # ハートビートの応答は届いている (last_receive が新しい) が、状態全体が途絶えている
    conn.check_deadlines(1000.0 + conn.STATUS_TIMEOUT - 1, last_receive=1000.0 + conn.STATUS_TIMEOUT - 2)
    with pytest.raises(protocol.FridgeStaleError):
        conn.check_deadlines(1000.0 + conn.STATUS_TIMEOUT, last_receive=1000.0 + conn.STATUS_TIMEOUT - 1)


def test_receive_timeout_is_plain_disconnect() -> None:
    conn = _connection()
    conn._last_status = 1000.0
    with pytest.raises(protocol.FridgeError) as info:
        conn.check_deadlines(1000.0 + conn.RECEIVE_TIMEOUT, last_receive=1000.0)
    assert not isinstance(info.value, protocol.FridgeStaleError)


def test_status_resets_stale_deadline() -> None:
    conn = _connection()
    conn._last_status = 0.0
    body = bytes([0x00]) + bytes(62)
    conn._dispatch(0x03, body)
    conn.check_deadlines(conn._last_status + 1, last_receive=conn._last_status + 1)


class _FakeSocket:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def sendall(self, _data: bytes) -> None:
        return None

    def recv(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def settimeout(self, _value: float) -> None:
        return None


class _ChattySocket(_FakeSocket):
    """認証後、状態全体を返さずに別のデータを送り続ける相手."""

    def __init__(self) -> None:
        super().__init__([AUTH_OK])
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def recv(self, n: int) -> bytes:
        if self._chunks:
            return super().recv(n)
        time.sleep(0.005)
        return b"x"


def test_query_status_has_overall_deadline(monkeypatch) -> None:
    sock = _ChattySocket()
    monkeypatch.setattr(protocol.socket, "create_connection", lambda *_a, **_k: sock)
    monkeypatch.setattr(protocol.LocalSecurity, "tcp_key", lambda *_a: b"")
    monkeypatch.setattr(protocol.LocalSecurity, "encode_8370", lambda _self, data, _t: bytes(data))
    monkeypatch.setattr(protocol.LocalSecurity, "decode_8370", lambda _self, data: ([], data))
    started = time.monotonic()
    with pytest.raises(protocol.FridgeError):
        protocol.query_status("127.0.0.1", 1, "00" * 64, "00" * 32, timeout=0.1)
    assert time.monotonic() - started < 1.0
    # 受信ごとの待ち時間は、全体の残り時間に合わせて短くなっていく
    # 時計の分解能による誤差 (Windows) は許容する
    assert sock.timeouts[-1] < sock.timeouts[0] <= 0.1 + 1e-3


def test_query_status_wraps_library_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        protocol.socket, "create_connection", lambda *_a, **_k: _FakeSocket([AUTH_OK[:30], AUTH_OK[30:]])
    )

    def broken_tcp_key(self, response, key):
        raise DataSignDoesntMatch

    monkeypatch.setattr(protocol.LocalSecurity, "tcp_key", broken_tcp_key)
    with pytest.raises(protocol.FridgeAuthError) as info:
        protocol.query_status("127.0.0.1", 1, "00" * 64, "00" * 32)
    assert isinstance(info.value.__cause__, DataSignDoesntMatch)


def test_library_error_after_handshake_is_not_auth_error(monkeypatch) -> None:
    monkeypatch.setattr(
        protocol.socket, "create_connection", lambda *_a, **_k: _FakeSocket([AUTH_OK, b"garbage"])
    )
    monkeypatch.setattr(protocol.LocalSecurity, "tcp_key", lambda *_a: b"")
    monkeypatch.setattr(protocol.LocalSecurity, "encode_8370", lambda _self, data, _t: bytes(data))

    def broken_decode(self, data):
        raise DataSignDoesntMatch

    monkeypatch.setattr(protocol.LocalSecurity, "decode_8370", broken_decode)
    with pytest.raises(protocol.FridgeError) as info:
        protocol.query_status("127.0.0.1", 1, "00" * 64, "00" * 32)
    assert not isinstance(info.value, protocol.FridgeAuthError)


def test_high_temp_alarms_are_unknown_until_notified() -> None:
    status = protocol.parse_base_status(bytes([0x00]) + bytes(62))
    assert status.refrigerator_high_temp is None
    assert status.freezer_high_temp is None


def test_pushed_status_drops_stale_energy() -> None:
    """Pushed status frames (data type 0x04) carry a stale energy value."""
    statuses = []
    conn = _connection(on_status=statuses.append)
    body = bytearray(63)
    body[14:16] = (1210).to_bytes(2, "little")
    conn._dispatch(0x04, bytes(body))
    body[14:16] = (1403).to_bytes(2, "little")
    conn._dispatch(protocol.DATA_TYPE_QUERY_RESPONSE, bytes(body))
    assert [s.estimated_daily_energy for s in statuses] == [None, 1403]
