"""設定フロー (登録・入力し直し・再認証・IP アドレスの変更) のテスト."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from midealocal.exceptions import MideaCloudError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.toshiba_iolife_fridge.const import (
    CONF_ACCOUNT,
    CONF_DEVICE_ID,
    CONF_KEY,
    CONF_MODEL,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.toshiba_iolife_fridge.protocol import FridgeError

FLOW = "custom_components.toshiba_iolife_fridge.config_flow"
DEVICE_ID = 1234
GOOD_HOST = "192.0.2.10"
BAD_HOST = "192.0.2.99"
USER_INPUT = {
    CONF_ACCOUNT: "user@example.com",
    CONF_PASSWORD: "secret",
    CONF_HOST: GOOD_HOST,
}


def _query_status(host, *_args, **_kwargs):
    if host != GOOD_HOST:
        raise FridgeError("no answer")


@pytest.fixture
def cloud():
    cloud = MagicMock()
    cloud.login = AsyncMock(return_value=True)
    cloud.list_appliances = AsyncMock(
        return_value={DEVICE_ID: {"type": 0xCA, "name": "Fridge"}}
    )
    cloud.get_cloud_keys = AsyncMock(return_value={1: {"token": "tok", "key": "key"}})
    # 機種名の取得は失敗しても登録できる
    cloud._api_request = AsyncMock(side_effect=RuntimeError("no model"))
    with (
        patch(f"{FLOW}.get_midea_cloud", return_value=cloud),
        patch(f"{FLOW}.query_status", side_effect=_query_status),
        patch(
            "custom_components.toshiba_iolife_fridge.async_setup_entry",
            return_value=True,
        ),
    ):
        yield cloud


def _entry(hass: HomeAssistant, host: str = GOOD_HOST) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(DEVICE_ID),
        data={
            CONF_ACCOUNT: "user@example.com",
            CONF_HOST: host,
            CONF_PORT: 6444,
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_TOKEN: "old-tok",
            CONF_KEY: "old-key",
            CONF_MODEL: None,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _start_user_flow(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return result


async def test_user_flow_creates_entry(hass: HomeAssistant, cloud) -> None:
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Fridge"
    assert result["data"][CONF_HOST] == GOOD_HOST
    assert result["data"][CONF_TOKEN] == "tok"
    assert CONF_PASSWORD not in result["data"]


async def test_user_flow_wrong_host_can_be_corrected(hass: HomeAssistant, cloud) -> None:
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: BAD_HOST}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect_device"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == GOOD_HOST


async def test_user_flow_key_error_is_shown_on_form(hass: HomeAssistant, cloud) -> None:
    cloud.get_cloud_keys.side_effect = MideaCloudError(-1, "boom")
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_get_key"}


async def test_user_flow_invalid_auth(hass: HomeAssistant, cloud) -> None:
    cloud.login.return_value = False
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_select_flow_returns_to_user_form_on_lan_error(
    hass: HomeAssistant, cloud
) -> None:
    cloud.list_appliances.return_value = {
        DEVICE_ID: {"type": 0xCA, "name": "Fridge"},
        5678: {"type": 0xCA, "name": "Second"},
    }
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: BAD_HOST}
    )
    assert result["step_id"] == "select"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE_ID: str(DEVICE_ID)}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect_device"}


async def test_already_configured(hass: HomeAssistant, cloud) -> None:
    _entry(hass)
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_shows_error_then_updates_keys(hass: HomeAssistant, cloud) -> None:
    entry = _entry(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: BAD_HOST}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect_device"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "tok"
    assert entry.data[CONF_KEY] == "key"


async def test_reconfigure_changes_only_host(hass: HomeAssistant, cloud) -> None:
    entry = _entry(hass, host=BAD_HOST)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.50"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect_device"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: GOOD_HOST}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == GOOD_HOST
    assert entry.data[CONF_TOKEN] == "old-tok"
    cloud.login.assert_not_called()
