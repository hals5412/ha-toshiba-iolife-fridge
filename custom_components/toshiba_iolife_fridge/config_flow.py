"""設定フロー.

IoLIFE アカウントでログインして冷蔵庫の token/key を取得し、LAN で問い合わせが通ることを
確認してから登録する。パスワードは保存しない (token/key だけを保存する)。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from midealocal.cloud import get_midea_cloud
from midealocal.exceptions import CloudLoginError, MideaCloudError

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CLOUD_NAME,
    CONF_ACCOUNT,
    CONF_DEVICE_ID,
    CONF_KEY,
    CONF_MODEL,
    CONF_TOKEN,
    DEVICE_TYPE_REFRIGERATOR,
    DOMAIN,
)
from .protocol import DEFAULT_PORT, FridgeError, query_status

_LOGGER = logging.getLogger(__name__)


class CannotLogin(Exception):
    """IoLIFE へのログイン失敗."""


class NoRefrigerator(Exception):
    """アカウントに冷蔵庫が登録されていない."""


class NoWorkingKey(Exception):
    """取得した token/key のどれでも LAN 問い合わせが通らない."""


class CannotGetKey(Exception):
    """IoLIFE からローカル通信用の鍵を取得できない."""


class ToshibaIolifeFridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """東芝 IoLIFE 冷蔵庫の設定フロー."""

    VERSION = 1

    def __init__(self) -> None:
        self._account = ""
        self._password = ""
        self._host = ""
        self._cloud = None
        self._appliances: dict[int, dict[str, Any]] = {}
        # 冷蔵庫の選択画面から鍵の確認に失敗したとき、最初の画面に出すエラー
        self._pending_error: str | None = None

    async def _async_login(self) -> None:
        cloud = get_midea_cloud(
            CLOUD_NAME,
            async_get_clientsession(self.hass),
            self._account,
            self._password,
        )
        try:
            logged_in = await cloud.login()
        except CloudLoginError as err:
            raise CannotLogin from err
        if not logged_in:
            raise CannotLogin
        self._cloud = cloud
        appliances = await cloud.list_appliances() or {}
        self._appliances = {
            device_id: info
            for device_id, info in appliances.items()
            if info.get("type") == DEVICE_TYPE_REFRIGERATOR
        }
        if not self._appliances:
            raise NoRefrigerator
        await self._async_fill_model_names()

    async def _async_fill_model_names(self) -> None:
        """機種名 (GR-Y540XFS など) を補う.

        midea-local の list_appliances は modelName を返さないので、同じ一覧 API を
        もう一度呼んで拾う。取れなくても登録には影響しない。
        """
        try:
            data = self._cloud._make_general_data()  # noqa: SLF001
            data["appVersion"] = self._cloud._app_version  # noqa: SLF001
            page_list = await self._cloud._api_request(  # noqa: SLF001
                endpoint="/v1/appliance/user/home/page/list/info",
                data=data,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("could not fetch model names", exc_info=True)
            return
        for item in page_list if isinstance(page_list, list) else []:
            try:
                device_id = int(item["id"])
            except (KeyError, TypeError, ValueError):
                continue
            model_name = str(item.get("modelName") or "")
            if device_id in self._appliances and model_name:
                # "GR-Y540XFS(TW)" の (TW) は色の違いなので落とす
                self._appliances[device_id]["model_name"] = model_name.split("(")[0]

    async def _async_fetch_working_key(self, device_id: int) -> dict[str, str]:
        keys = await self._cloud.get_cloud_keys(device_id)
        for method, pair in keys.items():
            try:
                await self.hass.async_add_executor_job(
                    query_status,
                    self._host,
                    device_id,
                    pair["token"],
                    pair["key"],
                    DEFAULT_PORT,
                )
            except FridgeError as err:
                _LOGGER.debug("key method %s did not work: %s", method, err)
                continue
            return pair
        raise NoWorkingKey

    def _schema(self, host_default: str = "") -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_ACCOUNT, default=self._account): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_HOST, default=host_default): str,
            }
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._account = user_input[CONF_ACCOUNT]
            self._password = user_input[CONF_PASSWORD]
            self._host = user_input[CONF_HOST].strip()
            try:
                await self._async_login()
            except CannotLogin:
                errors["base"] = "invalid_auth"
            except NoRefrigerator:
                errors["base"] = "no_refrigerator"
            except MideaCloudError:
                _LOGGER.exception("IoLIFE cloud error")
                errors["base"] = "cannot_connect"
            else:
                if len(self._appliances) > 1:
                    return await self.async_step_select()
                device_id = next(iter(self._appliances))
                await self.async_set_unique_id(str(device_id))
                self._abort_if_unique_id_configured()
                try:
                    return await self._async_finish(device_id)
                except NoWorkingKey:
                    errors["base"] = "cannot_connect_device"
                except CannotGetKey:
                    errors["base"] = "cannot_get_key"
        elif self._pending_error:
            errors["base"] = self._pending_error
            self._pending_error = None
        return self.async_show_form(
            step_id="user",
            data_schema=self._schema(self._host),
            errors=errors,
        )

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            device_id = int(user_input[CONF_DEVICE_ID])
            await self.async_set_unique_id(str(device_id))
            self._abort_if_unique_id_configured()
            try:
                return await self._async_finish(device_id)
            except NoWorkingKey:
                self._pending_error = "cannot_connect_device"
            except CannotGetKey:
                self._pending_error = "cannot_get_key"
            # IP アドレスを直せるように、最初の画面に戻る
            return await self.async_step_user()
        choices = {
            str(device_id): f"{info.get('name') or device_id} ({device_id})"
            for device_id, info in self._appliances.items()
        }
        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE_ID): vol.In(choices)}),
        )

    async def _async_finish(self, device_id: int) -> ConfigFlowResult:
        """鍵を取得して確かめ、登録 (再認証なら更新) する.

        冷蔵庫が応答しなければ NoWorkingKey、鍵を取得できなければ CannotGetKey を送出し、
        呼び出し側の画面でエラーを表示して入力し直せるようにする。
        """
        try:
            pair = await self._async_fetch_working_key(device_id)
        except MideaCloudError as err:
            _LOGGER.exception("IoLIFE cloud error while fetching keys")
            raise CannotGetKey from err

        info = self._appliances[device_id]
        data = {
            CONF_ACCOUNT: self._account,
            CONF_HOST: self._host,
            CONF_PORT: DEFAULT_PORT,
            CONF_DEVICE_ID: device_id,
            CONF_TOKEN: pair["token"],
            CONF_KEY: pair["key"],
            CONF_MODEL: info.get("model_name"),
        }
        if self.source == "reauth":
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=data
            )
        return self.async_create_entry(
            title=info.get("name") or f"Toshiba refrigerator {device_id}",
            data=data,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        self._account = entry_data[CONF_ACCOUNT]
        self._host = entry_data[CONF_HOST]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._account = user_input[CONF_ACCOUNT]
            self._password = user_input[CONF_PASSWORD]
            self._host = user_input[CONF_HOST].strip()
            device_id = self._get_reauth_entry().data[CONF_DEVICE_ID]
            try:
                await self._async_login()
            except CannotLogin:
                errors["base"] = "invalid_auth"
            except NoRefrigerator:
                errors["base"] = "no_refrigerator"
            except MideaCloudError:
                _LOGGER.exception("IoLIFE cloud error")
                errors["base"] = "cannot_connect"
            else:
                if device_id not in self._appliances:
                    errors["base"] = "no_refrigerator"
                else:
                    try:
                        return await self._async_finish(device_id)
                    except NoWorkingKey:
                        errors["base"] = "cannot_connect_device"
                    except CannotGetKey:
                        errors["base"] = "cannot_get_key"
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._schema(self._host),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """登録済みの冷蔵庫の IP アドレスだけを変える (鍵はそのまま使う)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                await self.hass.async_add_executor_job(
                    query_status,
                    host,
                    entry.data[CONF_DEVICE_ID],
                    entry.data[CONF_TOKEN],
                    entry.data[CONF_KEY],
                    entry.data.get(CONF_PORT, DEFAULT_PORT),
                )
            except FridgeError as err:
                _LOGGER.debug("refrigerator did not answer at %s: %s", host, err)
                errors["base"] = "cannot_connect_device"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_HOST: host}
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str}
            ),
            errors=errors,
        )
