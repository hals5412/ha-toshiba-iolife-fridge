"""冷蔵庫との常時接続と状態の保持."""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_DEVICE_ID, CONF_KEY, CONF_TOKEN, DOMAIN
from .protocol import (
    DoorInfo,
    FridgeAuthError,
    FridgeConnection,
    FridgeStaleError,
    FridgeStatus,
)

_LOGGER = logging.getLogger(__name__)

type FridgeConfigEntry = ConfigEntry[FridgeCoordinator]

# 連続でこの回数つながらなければ、エンティティを利用不可にする
UNAVAILABLE_AFTER_FAILURES = 3
# 認証エラーがこの回数続いたら再認証を求める
AUTH_FAILURE_THRESHOLD = 3
FIRST_DATA_TIMEOUT = 30
# 状態全体 (base) には含まれず、通知でしか届かない値
NON_BASE_FIELDS = ("refrigerator_high_temp", "freezer_high_temp", "eco_status")


class FridgeCoordinator(DataUpdateCoordinator[FridgeStatus]):
    """冷蔵庫からの通知 (ドア開閉など) と60秒ごとの問い合わせ結果を受け取る."""

    config_entry: FridgeConfigEntry

    def __init__(self, hass: HomeAssistant, entry: FridgeConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN)
        data = entry.data
        self._first_data = asyncio.Event()
        self._auth_failures = 0
        self._before_first_status: dict[str, object] = {}
        self._connection = FridgeConnection(
            data[CONF_HOST],
            data[CONF_PORT],
            data[CONF_DEVICE_ID],
            data[CONF_TOKEN],
            data[CONF_KEY],
            on_status=self._handle_status,
            on_door_info=self._handle_door_info,
            on_eco_status=self._handle_eco_status,
            on_disconnect=self._handle_disconnect,
        )

    async def async_start(self) -> None:
        """接続タスクを始め、最初の状態が届くまで待つ."""
        self.config_entry.async_create_background_task(
            self.hass, self._connection.run(), f"{DOMAIN}_connection"
        )
        try:
            await asyncio.wait_for(self._first_data.wait(), FIRST_DATA_TIMEOUT)
        except TimeoutError as err:
            raise ConfigEntryNotReady("refrigerator did not answer") from err

    @callback
    def _handle_status(self, status: FridgeStatus) -> None:
        self._auth_failures = 0
        self._first_data.set()
        # base に含まれない値は、直前の状態 (初回は初回前に届いた通知) から引き継ぐ。
        # ドアのように base にも含まれる値は、新しい base を優先する。
        if self.data is not None:
            carried = {name: getattr(self.data, name) for name in NON_BASE_FIELDS}
            # 電力量は問い合わせ応答でだけ更新する (自発通知では None)
            if status.estimated_daily_energy is None:
                carried["estimated_daily_energy"] = self.data.estimated_daily_energy
        else:
            carried = self._before_first_status
            self._before_first_status = {}
        self.async_set_updated_data(dataclasses.replace(status, **carried))

    @callback
    def _handle_door_info(self, doors: DoorInfo) -> None:
        # 警告のバイトがない短い通知 (None) では、警告の値を変えない
        self._apply_partial(
            **{k: v for k, v in dataclasses.asdict(doors).items() if v is not None}
        )

    @callback
    def _handle_eco_status(self, eco_status: str | None) -> None:
        self._apply_partial(eco_status=eco_status)

    @callback
    def _apply_partial(self, **changes: object) -> None:
        """ドア情報などの部分的な通知を反映する.

        利用不可にしている間は、部分通知だけで古い状態全体を正常扱いに戻さない。
        値は保持しておき、次に状態全体が届いたときに一緒に反映する。
        """
        if self.data is None:
            # 初回の base より先に届いた通知は、base に含まれない値だけ取っておく
            self._before_first_status.update(
                {name: value for name, value in changes.items() if name in NON_BASE_FIELDS}
            )
            return
        updated = dataclasses.replace(self.data, **changes)
        if self.last_update_success:
            self.async_set_updated_data(updated)
        else:
            self.data = updated

    @callback
    def _handle_disconnect(self, err: Exception, failures: int) -> None:
        if isinstance(err, FridgeAuthError):
            self._auth_failures += 1
            if self._auth_failures == AUTH_FAILURE_THRESHOLD:
                self.config_entry.async_start_reauth(self.hass)
        # 状態全体が途絶えた場合は、再接続の回数を待たずに利用不可にする
        stale = isinstance(err, FridgeStaleError)
        if (stale or failures >= UNAVAILABLE_AFTER_FAILURES) and self.last_update_success:
            self.async_set_update_error(UpdateFailed(str(err)))
