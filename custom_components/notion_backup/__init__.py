"""Notion content backups uploaded as ZIP archives to OneDrive."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, MAJOR_VERSION, MINOR_VERSION, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import (
    AuthenticationError,
    BackupError,
    NotionClient,
    OneDriveClient,
    folder_parts,
    parse_roots,
)
from .archive import Exporter, create_zip, safe_name
from .const import DOMAIN
from .linked_onedrive import linked_drive

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    if source_id := entry.data.get("onedrive_entry_id"):
        source = hass.config_entries.async_get_entry(source_id)
        if (
            source is not None
            and source.disabled_by is None
            and source.state is not ConfigEntryState.LOADED
        ):
            # Retry through HA rather than assuming startup order between integrations.
            raise ConfigEntryNotReady("Waiting for the selected OneDrive connection")
    runtime = BackupRuntime(hass, entry)
    entry.runtime_data = runtime
    await runtime.load()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.start()
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, runtime.stop_event))
    entry.async_on_unload(entry.add_update_listener(_reload))
    # HA 2026.10 requires bool; 2026.11 changed the contract to None.
    return True if (MAJOR_VERSION, MINOR_VERSION) < (2026, 11) else None


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    await entry.runtime_data.stop()
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        raise HomeAssistantError("Notion Backup platforms could not be unloaded")
    return True if (MAJOR_VERSION, MINOR_VERSION) < (2026, 11) else None


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry):
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()


async def _reload(hass, entry):
    # Token rotation updates data without changing options; it must not cancel the upload.
    if entry.runtime_data.settings != {**entry.data, **entry.options}:
        # Ignore refresh-token-only updates.
        new = {**entry.data, **entry.options}
        old = entry.runtime_data.settings
        if {k: v for k, v in new.items() if k != "refresh_token"} != {
            k: v for k, v in old.items() if k != "refresh_token"
        }:
            await hass.config_entries.async_reload(entry.entry_id)


class BackupRuntime:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.settings = {**entry.data, **entry.options}
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.state = {"status": "idle", "last_success": None, "last_attempt": None, "error": None}
        self.task = None
        self.unsubscribe = None
        self.signal = f"{DOMAIN}_{entry.entry_id}"

    async def load(self):
        self.state.update(await self.store.async_load() or {})
        if self.state["status"] == "running":
            self.state.update(status="interrupted", error="Previous backup was interrupted")

    def start(self):
        self.unsubscribe = async_track_time_change(
            self.hass,
            self.scheduled,
            hour=self.settings["hour"],
            minute=self.settings["minute"],
            second=0,
        )
        now = dt_util.now()
        slot = now.replace(
            hour=self.settings["hour"], minute=self.settings["minute"], second=0, microsecond=0
        )
        if slot > now:
            slot -= timedelta(days=1)
        last = (
            dt_util.parse_datetime(self.state["last_attempt"])
            if self.state["last_attempt"]
            else None
        )
        # First backup starts after setup; missed daily runs are caught up after restart.
        if last is None or last < slot:
            self.launch()

    @callback
    def scheduled(self, now):
        last = self.state.get("last_attempt")
        if last and dt_util.as_local(dt_util.parse_datetime(last)).date() == now.date():
            # Also prevents a duplicate automatic run during the autumn DST overlap.
            return
        self.launch()

    @callback
    def launch(self):
        if self.task and not self.task.done():
            return self.task
        self.task = self.entry.async_create_background_task(
            self.hass, self.run(), f"Notion backup {self.entry.entry_id}"
        )
        return self.task

    async def stop_event(self, event):
        await self.stop()

    async def stop(self):
        if self.unsubscribe:
            self.unsubscribe()
            self.unsubscribe = None
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def save(self):
        await self.store.async_save(self.state)
        async_dispatcher_send(self.hass, self.signal)

    async def manual(self):
        if self.task and not self.task.done():
            raise HomeAssistantError("A Notion backup is already running")
        await self.launch()
        if self.state["status"] != "completed":
            raise HomeAssistantError(self.state.get("error") or "Backup was not completed")

    async def run(self):
        temporary = None
        self.state.update(
            status="running",
            error=None,
            phase="checking_destination",
            last_attempt=dt_util.utcnow().isoformat(),
        )
        await self.save()
        try:
            session = async_get_clientsession(self.hass)
            notion = NotionClient(session, self.settings["notion_token"])
            identity = await notion.identity()
            if identity.get("id") != self.settings["notion_bot_id"]:
                raise AuthenticationError("Notion connection identity changed")

            async def save_refresh(token):
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    data={**self.entry.data, "refresh_token": token},
                )
                self.settings["refresh_token"] = token

            app_root_id = None
            if linked_entry_id := self.settings.get("onedrive_entry_id"):
                drive, info, app_root_id, app_folder_name = await linked_drive(
                    self.hass,
                    linked_entry_id,
                    self.settings["drive_id"],
                )
                self.state["destination"] = (
                    f"Apps/{app_folder_name}/{self.settings['backup_folder']}"
                )
            else:
                drive = OneDriveClient(
                    session,
                    self.settings["client_id"],
                    self.entry.data["refresh_token"],
                    save_refresh,
                )
                info = await drive.request("GET", "me/drive")
                if info["id"] != self.settings["drive_id"]:
                    raise AuthenticationError("OneDrive destination identity changed")
            parts = folder_parts(self.settings["backup_folder"]) + [
                f"{safe_name(self.entry.title)}_{self.settings['notion_bot_id']}",
            ]
            if app_root_id is not None:
                folder_id = await drive.folder(info["id"], parts, root_id=app_root_id)
            else:
                folder_id = await drive.folder(info["id"], parts)
            self.state.update(phase="exporting", last_activity=dt_util.utcnow().isoformat())
            await self.save()
            temporary = Path(
                await self.hass.async_add_executor_job(tempfile.mkdtemp, "", "notion_backup_")
            )
            content = temporary / "content"
            await self.hass.async_add_executor_job(content.mkdir)
            exporter = Exporter(
                notion, content, self.entry.title, parse_roots(self.settings["root_pages"])
            )
            manifest = await exporter.run()
            self.state.update(
                phase="creating_zip",
                last_activity=dt_util.utcnow().isoformat(),
                pages=sum(o["type"] == "page" for o in manifest["objects"]),
                attachments=len(manifest["attachments"]),
                errors=len(manifest["errors"]),
            )
            await self.save()
            stamp = dt_util.utcnow().strftime("%Y-%m-%d_%H%M%S")
            suffix = "" if manifest["complete"] else "_INCOMPLETE"
            filename = f"Notion_{safe_name(self.entry.title)}_{stamp}_{uuid4().hex[:8]}{suffix}.zip"
            archive_path = temporary / filename
            # Wait for the executor on cancellation, so cleanup cannot race ZIP writing.
            worker = asyncio.ensure_future(
                self.hass.async_add_executor_job(create_zip, content, archive_path)
            )
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                await worker
                raise
            self.state.update(phase="uploading", last_activity=dt_util.utcnow().isoformat())
            await self.save()
            item = await drive.upload(info["id"], folder_id, archive_path)
            self.state.update(
                status="completed" if manifest["complete"] else "incomplete",
                last_upload=dt_util.utcnow().isoformat(),
                filename=filename,
                bytes=item["size"],
                pages=sum(o["type"] == "page" for o in manifest["objects"]),
                attachments=len(manifest["attachments"]),
                errors=len(manifest["errors"]),
                phase="finished",
                last_activity=dt_util.utcnow().isoformat(),
            )
            if manifest["complete"]:
                self.state["last_success"] = dt_util.utcnow().isoformat()
            else:
                self.state["error"] = (
                    f"Incomplete ZIP uploaded: {len(manifest['errors'])} "
                    "content/attachment errors; inspect manifest.json"
                )
        except asyncio.CancelledError:
            self.state.update(
                status="interrupted", error="Backup interrupted by reload or shutdown"
            )
            raise
        except AuthenticationError as err:
            self.state.update(status="failed", error=str(err))
            if not str(err).startswith("Notion") and self.settings.get("onedrive_entry_id"):
                linked_entry = self.hass.config_entries.async_get_entry(
                    self.settings["onedrive_entry_id"]
                )
                if linked_entry is not None:
                    linked_entry.async_start_reauth(self.hass)
            else:
                self.entry.async_start_reauth(
                    self.hass, context={"notion_auth": str(err).startswith("Notion")}
                )
        except BackupError as err:
            self.state.update(status="failed", error=str(err))
        except Exception:
            # Never log arbitrary exception text: HTTP exceptions can contain signed URLs.
            self.state.update(
                status="failed",
                error="Unexpected backup error; check connectivity and available storage",
            )
            _LOGGER.error("Unexpected backup error for entry %s", self.entry.entry_id)
        finally:
            if temporary:
                await self.hass.async_add_executor_job(shutil.rmtree, temporary, True)
            await self.save()
            if self.state["status"] in {"failed", "incomplete", "interrupted"}:
                from homeassistant.components import persistent_notification

                persistent_notification.async_create(
                    self.hass,
                    self.state["error"],
                    title=f"Notion Backup: {self.entry.title}",
                    notification_id=f"{DOMAIN}_{self.entry.entry_id}",
                )
            else:
                from homeassistant.components import persistent_notification

                persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{self.entry.entry_id}")
