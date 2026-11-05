import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries, loader
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from custom_components.notion_backup import BackupRuntime, _reload
from custom_components.notion_backup.api import AuthenticationError, BackupError
from custom_components.notion_backup.button import BackupButton
from custom_components.notion_backup.config_flow import (
    NotionBackupConfigFlow,
    NotionBackupOptionsFlow,
)
from custom_components.notion_backup.const import DOMAIN
from custom_components.notion_backup.sensor import LastSuccessSensor, StatusSensor

ROOT = "11111111-1111-1111-1111-111111111111"
BOT = "22222222-2222-2222-2222-222222222222"
INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "custom_components/notion_backup"
DATA = {
    "name": "Test account",
    "notion_token": "synthetic-notion-token",
    "root_pages": ROOT,
    "backup_folder": "Notion Backups",
    "hour": 2,
    "minute": 0,
    "client_id": "33333333-3333-3333-3333-333333333333",
    "refresh_token": "synthetic-refresh",
    "notion_bot_id": BOT,
    "workspace_name": "Test workspace",
    "drive_id": "test-drive",
}


@pytest.fixture
async def hass(tmp_path):
    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = config_entries.ConfigEntries(instance, {})
    loader.async_setup(instance)
    yield instance
    await instance.async_stop(force=True)


def make_entry(hass, data=None):
    entry = config_entries.ConfigEntry(
        data=copy.deepcopy(data or DATA),
        options={},
        domain=DOMAIN,
        title="Test account",
        source="user",
        unique_id=BOT,
        version=1,
        minor_version=1,
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
    )
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


@pytest.fixture
def api_mocks():
    with (
        patch(
            "custom_components.notion_backup.config_flow.async_get_clientsession",
            return_value=object(),
        ),
        patch("custom_components.notion_backup.config_flow.NotionClient") as notion,
        patch("custom_components.notion_backup.config_flow.MicrosoftAuth") as auth,
        patch("custom_components.notion_backup.config_flow.OneDriveClient") as drive,
    ):
        notion.return_value.identity = AsyncMock(
            return_value={"id": BOT, "bot": {"workspace_name": "Test workspace"}}
        )
        notion.return_value.root = AsyncMock(return_value=("page", {"id": ROOT}))
        auth.return_value.device_code = AsyncMock(
            return_value={
                "device_code": "synthetic-device",
                "user_code": "ABCD-1234",
                "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 900,
                "interval": 5,
            }
        )
        auth.return_value.exchange = AsyncMock(
            return_value={
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "expires_in": 3600,
            }
        )
        drive.return_value.request = AsyncMock(
            return_value={"id": "test-drive", "owner": {"user": {"displayName": "Test owner"}}}
        )
        yield notion.return_value, auth.return_value, drive.return_value


async def test_ui_flow_validates_roots_before_ms_login_and_creates_entry(hass, api_mocks):
    flow = NotionBackupConfigFlow()
    flow.hass, flow.context = hass, {"source": "user"}
    initial = await flow.async_step_user()
    assert initial["type"] == FlowResultType.FORM
    assert initial["step_id"] == "user"
    user = {
        k: DATA[k]
        for k in ("name", "notion_token", "root_pages", "backup_folder", "hour", "minute")
    }
    assert (await flow.async_step_user(user))["step_id"] == "onedrive"
    result = await flow.async_step_onedrive({"client_id": DATA["client_id"]})
    assert result["step_id"] == "authorize"
    assert result["description_placeholders"]["code"] == "ABCD-1234"
    flow.next_poll = 0
    result = await flow.async_step_authorize({})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["drive_id"] == "test-drive"
    assert result["data"]["notion_bot_id"] == BOT


async def test_ui_flow_invalid_roots_and_credentials_do_not_start_ms_login(hass, api_mocks):
    notion, auth, _ = api_mocks
    flow = NotionBackupConfigFlow()
    flow.hass, flow.context = hass, {"source": "user"}
    user = {
        k: DATA[k]
        for k in ("name", "notion_token", "root_pages", "backup_folder", "hour", "minute")
    }
    user["root_pages"] = "invalid"
    result = await flow.async_step_user(user)
    assert result["errors"]["base"] == "invalid_settings"
    auth.device_code.assert_not_called()
    user["root_pages"] = ROOT
    notion.identity.side_effect = AuthenticationError("Notion: denied")
    assert (await flow.async_step_user(user))["errors"]["base"] == "invalid_auth"


async def test_pending_auth_cannot_create_entry(hass, api_mocks):
    _, auth, _ = api_mocks
    flow = NotionBackupConfigFlow()
    flow.hass, flow.context = hass, {"source": "user"}
    flow.values = copy.deepcopy(DATA)
    await flow.async_step_onedrive({"client_id": DATA["client_id"]})
    assert (await flow.async_step_authorize({}))["errors"]["base"] == "authorization_pending"
    auth.exchange.assert_not_called()
    flow.next_poll = 0
    auth.exchange.return_value = {"pending": "slow_down"}
    assert (await flow.async_step_authorize({}))["errors"]["base"] == "authorization_pending"
    assert flow.device["interval"] == 10


async def test_reauthentication_cannot_switch_to_another_onedrive(hass, api_mocks):
    _, _, drive = api_mocks
    entry = make_entry(hass)
    flow = NotionBackupConfigFlow()
    flow.hass = hass
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    await flow.async_step_reauth(entry.data)
    await flow.async_step_onedrive({"client_id": DATA["client_id"]})
    flow.next_poll = 0
    drive.request.return_value = {"id": "other-drive"}
    result = await flow.async_step_authorize({})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "wrong_drive"


@pytest.fixture
def runtime_mocks():
    with (
        patch("custom_components.notion_backup.async_get_clientsession", return_value=object()),
        patch("custom_components.notion_backup.NotionClient") as notion,
        patch("custom_components.notion_backup.OneDriveClient") as drive,
        patch("custom_components.notion_backup.Exporter") as exporter,
        patch("custom_components.notion_backup.create_zip") as zipper,
    ):
        notion.return_value.identity = AsyncMock(return_value={"id": BOT})
        drive.return_value.request = AsyncMock(return_value={"id": "test-drive"})
        drive.return_value.folder = AsyncMock(return_value="folder")
        drive.return_value.upload = AsyncMock(return_value={"id": "uploaded", "size": 123})
        exporter.return_value.run = AsyncMock(
            return_value={
                "complete": True,
                "objects": [{"type": "page"}],
                "attachments": [{"path": "attachments/a.pdf"}],
                "errors": [],
            }
        )
        yield notion.return_value, drive.return_value, exporter.return_value, zipper


async def test_runtime_success_updates_last_success_and_entities(hass, runtime_mocks):
    entry = make_entry(hass)
    runtime = BackupRuntime(hass, entry)
    entry.runtime_data = runtime
    runtime.store = MagicMock(async_save=AsyncMock())
    await runtime.run()
    assert runtime.state["status"] == "completed"
    assert runtime.state["last_success"] is not None
    assert runtime.state["attachments"] == 1
    assert StatusSensor(entry).native_value == "completed"
    assert LastSuccessSensor(entry).native_value.tzinfo is not None
    assert BackupButton(entry).unique_id.endswith("backup_now")
    filename = runtime.state["filename"]
    assert filename.endswith(".zip") and "INCOMPLETE" not in filename
    path = runtime_mocks[1].upload.call_args.args[-1]
    assert not path.parent.exists()  # temporary data removed after verified upload


async def test_incomplete_zip_is_uploaded_but_never_overwrites_last_success(hass, runtime_mocks):
    _, drive, exporter, _ = runtime_mocks
    exporter.run.return_value.update(complete=False, errors=[{"reason": "missing PDF"}])
    entry = make_entry(hass)
    runtime = BackupRuntime(hass, entry)
    runtime.store = MagicMock(async_save=AsyncMock())
    previous = "2026-10-01T02:00:00+00:00"
    runtime.state["last_success"] = previous
    await runtime.run()
    assert runtime.state["status"] == "incomplete"
    assert runtime.state["last_success"] == previous
    assert runtime.state["filename"].endswith("_INCOMPLETE.zip")
    drive.upload.assert_awaited_once()


async def test_failed_upload_preserves_last_success(hass, runtime_mocks):
    runtime_mocks[1].upload.side_effect = BackupError("OneDrive upload: HTTP 503")
    runtime = BackupRuntime(hass, make_entry(hass))
    runtime.store = MagicMock(async_save=AsyncMock())
    await runtime.run()
    assert runtime.state["status"] == "failed"
    assert runtime.state["last_success"] is None
    assert runtime.state.get("last_upload") is None


async def test_wrong_notion_identity_prevents_export_and_upload(hass, runtime_mocks):
    notion, drive, exporter, _ = runtime_mocks
    notion.identity.return_value = {"id": "other-bot"}
    entry = make_entry(hass)
    runtime = BackupRuntime(hass, entry)
    runtime.store = MagicMock(async_save=AsyncMock())
    with patch.object(type(entry), "async_start_reauth") as reauth:
        await runtime.run()
    assert runtime.state["status"] == "failed"
    exporter.run.assert_not_called()
    drive.upload.assert_not_called()
    assert reauth.call_args.kwargs["context"]["notion_auth"]


async def test_schedule_and_restart_catchup(hass):
    runtime = BackupRuntime(hass, make_entry(hass))
    runtime.launch = MagicMock()
    fixed = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)
    with (
        patch("custom_components.notion_backup.dt_util.now", return_value=fixed),
        patch("custom_components.notion_backup.async_track_time_change") as track,
    ):
        runtime.start()
        assert track.call_args.kwargs == {"hour": 2, "minute": 0, "second": 0}
        runtime.launch.assert_called_once()
        runtime.launch.reset_mock()
        runtime.state["last_attempt"] = (fixed - timedelta(hours=1)).isoformat()
        runtime.start()
        runtime.launch.assert_not_called()


async def test_manual_backup_does_not_overlap_and_cancel_cleans_up(hass, runtime_mocks):
    gate = asyncio.Event()

    async def export():
        await gate.wait()

    runtime_mocks[2].run.side_effect = export
    runtime = BackupRuntime(hass, make_entry(hass))
    runtime.store = MagicMock(async_save=AsyncMock())
    first = runtime.launch()
    await asyncio.sleep(0.02)
    assert runtime.launch() is first
    with pytest.raises(HomeAssistantError, match="already running"):
        await runtime.manual()
    await runtime.stop()
    assert first.cancelled()
    assert runtime.state["status"] == "interrupted"
    runtime_mocks[1].upload.assert_not_called()


async def test_options_validate_scope_and_change_schedule_without_ms_login(hass, api_mocks):
    entry = make_entry(hass)
    flow = NotionBackupOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_init()
    assert result["type"] == FlowResultType.FORM
    result = await flow.async_step_init(
        {"root_pages": ROOT, "backup_folder": "New Folder", "hour": 4, "minute": 30}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["hour"] == 4
    api_mocks[1].device_code.assert_not_called()


async def test_refresh_rotation_does_not_reload_running_integration(hass):
    entry = make_entry(hass)
    entry.runtime_data = BackupRuntime(hass, entry)
    with patch.object(type(hass.config_entries), "async_reload", new_callable=AsyncMock) as reload:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "refresh_token": "rotated"}
        )
        await _reload(hass, entry)
        reload.assert_not_called()
        hass.config_entries.async_update_entry(entry, options={"hour": 5})
        await _reload(hass, entry)
        reload.assert_awaited_once_with(entry.entry_id)


async def test_real_ha_entry_setup_loads_entities_and_unloads_cleanly(hass):
    """Exercise actual loader/platform APIs, not just entity constructors."""
    from homeassistant.helpers import area_registry, device_registry, entity_registry
    from homeassistant.setup import async_setup_component

    base = INTEGRATION_DIR
    integration = loader.Integration(
        hass,
        "custom_components.notion_backup",
        base,
        json.loads((base / "manifest.json").read_text()),
        {p.name for p in base.iterdir()},
    )
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = integration
    await hass.config_entries.async_initialize()
    await area_registry.async_load(hass, load_empty=True)
    device_registry.async_setup(hass)
    await device_registry.async_load(hass, load_empty=True)
    await entity_registry.async_load(hass, load_empty=True)
    entry = make_entry(hass)
    with (
        patch.object(BackupRuntime, "start"),
        patch.object(BackupRuntime, "load", new_callable=AsyncMock),
    ):
        assert await asyncio.wait_for(async_setup_component(hass, DOMAIN, {}), 5)
        await hass.async_block_till_done()
        assert entry.state == config_entries.ConfigEntryState.LOADED
        entity_ids = [s.entity_id for s in hass.states.async_all()]
        assert sum(e.startswith("sensor.") for e in entity_ids) == 2
        assert sum(e.startswith("button.") for e in entity_ids) == 1
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_automatic_schedule_does_not_repeat_on_same_local_day(hass):
    from homeassistant.util import dt as dt_util

    runtime = BackupRuntime(hass, make_entry(hass))
    runtime.launch = MagicMock()
    now = dt_util.now()
    runtime.state["last_attempt"] = now.isoformat()
    runtime.scheduled(now)
    runtime.launch.assert_not_called()
    runtime.state["last_attempt"] = (now - timedelta(days=1)).isoformat()
    runtime.scheduled(now)
    runtime.launch.assert_called_once()


def test_translation_keys_match_and_json_files_are_valid():
    base = Path(__file__).resolve().parents[1] / "custom_components/notion_backup"
    en = json.loads((base / "strings.json").read_text())
    de = json.loads((base / "translations/de.json").read_text())

    def keys(value):
        return {key: keys(item) if isinstance(item, dict) else None for key, item in value.items()}

    assert keys(en) == keys(de)
    assert en == json.loads((base / "translations/en.json").read_text())
    assert json.loads((base / "manifest.json").read_text())["config_flow"] is True
