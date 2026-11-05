"""UI setup: one Notion connection and OneDrive destination per config entry."""

from __future__ import annotations

import time
from uuid import UUID

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    AuthenticationError,
    BackupError,
    MicrosoftAuth,
    NotionClient,
    OneDriveClient,
    folder_parts,
    parse_roots,
)
from .const import DEFAULT_FOLDER, DOMAIN


def settings_schema(data):
    return {
        vol.Required("root_pages", default=data.get("root_pages", "")): TextSelector(
            TextSelectorConfig(multiline=True)
        ),
        vol.Required("backup_folder", default=data.get("backup_folder", DEFAULT_FOLDER)): str,
        vol.Required("hour", default=data.get("hour", 2)): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=23)
        ),
        vol.Required("minute", default=data.get("minute", 0)): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=59)
        ),
    }


async def validate_roots(client, data):
    folder_parts(data["backup_folder"])
    roots = parse_roots(data["root_pages"])
    for root_id in roots:
        await client.root(root_id)
    return roots


class NotionBackupConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self.values = {}
        self.device = None
        self.deadline = 0.0
        self.next_poll = 0.0

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                client = NotionClient(
                    async_get_clientsession(self.hass), user_input["notion_token"].strip()
                )
                identity = await client.identity()
                await validate_roots(client, user_input)
                await self.async_set_unique_id(identity["id"])
                self._abort_if_unique_id_configured()
            except ValueError:
                errors["base"] = "invalid_settings"
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except BackupError:
                errors["base"] = "cannot_connect"
            else:
                self.values = {
                    **user_input,
                    "notion_token": user_input["notion_token"].strip(),
                    "notion_bot_id": identity["id"],
                    "workspace_name": identity.get("bot", {}).get("workspace_name", "Notion"),
                }
                return await self.async_step_onedrive()
        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=(user_input or {}).get("name", "Notion")): str,
                    vol.Required("notion_token"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    **settings_schema(user_input or {}),
                }
            ),
        )

    async def async_step_onedrive(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                client_id = str(UUID(user_input["client_id"].strip()))
                auth = MicrosoftAuth(async_get_clientsession(self.hass), client_id)
                self.device = await auth.device_code()
                self.values["client_id"] = client_id
                self.deadline = time.monotonic() + self.device["expires_in"]
                self.next_poll = time.monotonic() + self.device.get("interval", 5)
                return await self.async_step_authorize()
            except ValueError:
                errors["base"] = "invalid_client"
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except BackupError:
                errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="onedrive",
            errors=errors,
            description_placeholders={"workspace": self.values.get("workspace_name", "Notion")},
            data_schema=vol.Schema(
                {vol.Required("client_id", default=self.values.get("client_id", "")): str}
            ),
        )

    async def async_step_authorize(self, user_input=None):
        if not self.device or time.monotonic() >= self.deadline:
            return self.async_abort(reason="login_expired")
        errors = {}
        if user_input is not None:
            if time.monotonic() < self.next_poll:
                errors["base"] = "authorization_pending"
            else:
                try:
                    auth = MicrosoftAuth(
                        async_get_clientsession(self.hass), self.values["client_id"]
                    )
                    token = await auth.exchange(
                        grant_type="urn:ietf:params:oauth:grant-type:device_code",
                        device_code=self.device["device_code"],
                    )
                    if token.get("pending"):
                        interval = self.device.get("interval", 5)
                        if token["pending"] == "slow_down":
                            interval += 5
                            self.device["interval"] = interval
                        self.next_poll = time.monotonic() + interval
                        errors["base"] = "authorization_pending"
                    elif not token.get("refresh_token"):
                        errors["base"] = "invalid_auth"
                    else:

                        async def save_refresh(value):
                            self.values["refresh_token"] = value

                        self.values["refresh_token"] = token["refresh_token"]
                        drive = OneDriveClient(
                            async_get_clientsession(self.hass),
                            self.values["client_id"],
                            token["refresh_token"],
                            save_refresh,
                        )
                        drive.access_token = token["access_token"]
                        drive.expires = time.monotonic() + token.get("expires_in", 3600) - 120
                        info = await drive.request("GET", "me/drive")
                        self.values["drive_id"] = info["id"]
                        self.values["drive_name"] = (
                            info.get("owner", {})
                            .get("user", {})
                            .get("displayName", info.get("name", "OneDrive"))
                        )
                        if self.source == config_entries.SOURCE_REAUTH:
                            entry = self._get_reauth_entry()
                            if entry.data["drive_id"] != info["id"]:
                                return self.async_abort(reason="wrong_drive")
                            return self.async_update_reload_and_abort(entry, data=self.values)
                        return self.async_create_entry(title=self.values["name"], data=self.values)
                except AuthenticationError:
                    errors["base"] = "invalid_auth"
                except BackupError:
                    errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="authorize",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "url": self.device["verification_uri"],
                "code": self.device["user_code"],
            },
        )

    async def async_step_reauth(self, entry_data):
        self.values = dict(entry_data)
        if self.context.get("notion_auth"):
            return await self.async_step_notion_reauth()
        return await self.async_step_onedrive()

    async def async_step_notion_reauth(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                client = NotionClient(
                    async_get_clientsession(self.hass), user_input["notion_token"].strip()
                )
                identity = await client.identity()
                if identity["id"] != self.values["notion_bot_id"]:
                    return self.async_abort(reason="wrong_notion")
                await validate_roots(client, {**self.values, **self._get_reauth_entry().options})
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except BackupError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={"notion_token": user_input["notion_token"].strip()},
                )
        return self.async_show_form(
            step_id="notion_reauth",
            data_schema=vol.Schema(
                {
                    vol.Required("notion_token"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return NotionBackupOptionsFlow()


class NotionBackupOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        current = {**self.config_entry.data, **self.config_entry.options}
        errors = {}
        if user_input is not None:
            try:
                await validate_roots(
                    NotionClient(async_get_clientsession(self.hass), current["notion_token"]),
                    user_input,
                )
            except ValueError:
                errors["base"] = "invalid_settings"
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except BackupError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(settings_schema(user_input or current)),
            errors=errors,
        )
