"""Use a selected HA OneDrive connection without copying its credentials."""

from __future__ import annotations

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AuthenticationError, BackupError, OneDriveClient


def onedrive_entries(hass):
    """Include temporarily unavailable accounts so setup can explain the failure."""
    return [
        entry
        for entry in hass.config_entries.async_entries("onedrive")
        if entry.disabled_by is None
    ]


async def linked_drive(hass, entry_id, expected_drive_id=None):
    """Validate the exact selected account and obtain its application folder."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != "onedrive" or entry.disabled_by is not None:
        raise BackupError("Selected OneDrive connection is missing or disabled")
    if entry.state is not ConfigEntryState.LOADED or not hasattr(entry, "runtime_data"):
        raise BackupError("Selected OneDrive connection is not ready; check OneDrive in HA")
    runtime = entry.runtime_data
    if not callable(getattr(runtime, "token_function", None)):
        raise BackupError("Selected OneDrive connection has an unsupported runtime")

    async def token():
        try:
            return await runtime.token_function()
        except ConfigEntryAuthFailed:
            entry.async_start_reauth(hass)
            raise AuthenticationError("OneDrive: renew the existing HA connection") from None
        except aiohttp.ClientResponseError as err:
            if err.status in (400, 401, 403):
                entry.async_start_reauth(hass)
                raise AuthenticationError("OneDrive: renew the existing HA connection") from None
            raise BackupError("OneDrive authentication service unavailable") from None
        except (aiohttp.ClientError, TimeoutError):
            raise BackupError("OneDrive authentication connection failed") from None

    # OAuth state is read only through the core integration's refresh-aware callback.
    # No access/refresh token is persisted in our config entry or copied from storage.
    drive = OneDriveClient(
        async_get_clientsession(hass),
        "",
        "",
        None,
        token_provider=token,
        use_me=True,
    )
    info = await drive.request("GET", "me/drive")
    if expected_drive_id is not None and info["id"] != expected_drive_id:
        raise AuthenticationError("OneDrive destination identity changed")
    try:
        approot = await runtime.client.get_approot()
    except Exception:
        # SDK exception text may contain HTTP details. Keep the user-facing text safe.
        raise BackupError("OneDrive application folder is unavailable") from None
    if not approot.id:
        raise BackupError("OneDrive application folder has no ID")
    return drive, info, approot.id, approot.name or "Home Assistant"
