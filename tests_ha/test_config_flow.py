"""Config flow: manual, discovery, reauth, reconfigure."""
from __future__ import annotations

from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pclink.api import PclinkConnectionError
from custom_components.pclink.const import DOMAIN

try:
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # HA < 2024.11
    from homeassistant.components.zeroconf import ZeroconfServiceInfo
from ipaddress import ip_address


async def test_user_flow_creates_entry(hass: HomeAssistant, fake_client, entry_data) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == data_entry_flow.FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], entry_data)
    await hass.async_block_till_done()
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "TestPC"
    assert result["data"] == entry_data
    assert result["result"].unique_id == "testpc"


async def test_user_flow_errors(hass: HomeAssistant, fake_client, entry_data) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    # wrong token
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**entry_data, "token": "wrong"})
    assert result["type"] == data_entry_flow.FlowResultType.FORM and result["errors"] == {"base": "invalid_auth"}
    # unreachable
    fake_client.instances.clear()
    orig_init = fake_client.__init__

    def failing_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        self.fail = PclinkConnectionError("nope")

    fake_client.__init__ = failing_init
    try:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], entry_data)
    finally:
        fake_client.__init__ = orig_init
    assert result["errors"] == {"base": "cannot_connect"}
    # old service without the catalog

    def outdated_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        self.catalog = None

    fake_client.__init__ = outdated_init
    try:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], entry_data)
    finally:
        fake_client.__init__ = orig_init
    assert result["errors"] == {"base": "outdated"}
    # finally a good one
    result = await hass.config_entries.flow.async_configure(result["flow_id"], entry_data)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY


async def test_duplicate_pc_is_rejected_and_address_updated(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data={**entry_data, "host": "10.0.0.9"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], entry_data)
    assert result["type"] == data_entry_flow.FlowResultType.ABORT and result["reason"] == "already_configured"
    assert entry.data["host"] == "192.168.1.50"


async def test_zeroconf_flow(hass: HomeAssistant, fake_client) -> None:
    info = ZeroconfServiceInfo(
        ip_address=ip_address("192.168.1.50"), ip_addresses=[ip_address("192.168.1.50")], hostname="family-pc.local.",
        name="TestPC._pclink._tcp.local.", port=47392, type="_pclink._tcp.local.",
        properties={"id": "testpc", "name": "TestPC", "version": "0.3.0", "path": "/api"},
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info)
    assert result["type"] == data_entry_flow.FlowResultType.FORM and result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"]["host"] == "192.168.1.50:47392"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "wrong"})
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "secret"})
    await hass.async_block_till_done()
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"] == {"host": "192.168.1.50", "port": 47392, "token": "secret"}
    assert result["result"].unique_id == "testpc"
    # a second announcement for the same PC is ignored
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info)
    assert result["type"] == data_entry_flow.FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_reauth_updates_token(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data={**entry_data, "token": "old"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id, "unique_id": entry.unique_id},
        data=entry.data)
    assert result["type"] == data_entry_flow.FlowResultType.FORM and result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "still-wrong"})
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "secret"})
    await hass.async_block_till_done()
    assert result["type"] == data_entry_flow.FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data["token"] == "secret"


async def test_reconfigure_changes_address(hass: HomeAssistant, fake_client, entry_data) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="testpc", title="TestPC", data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id})
    assert result["type"] == data_entry_flow.FlowResultType.FORM and result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**entry_data, "host": "10.0.0.5"})
    await hass.async_block_till_done()
    assert result["type"] == data_entry_flow.FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == "10.0.0.5"
    assert fake_client.instances[-1].host == "10.0.0.5"  # reloaded with the new address
    assert hass.states.get("sensor.testpc_cpu_usage").state == "12.5"
