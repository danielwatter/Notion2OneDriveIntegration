"""Manual backup action."""

from homeassistant.components.button import ButtonEntity

from .entity import BackupEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([BackupButton(entry)])


class BackupButton(BackupEntity, ButtonEntity):
    _attr_translation_key = "backup_now"
    _attr_icon = "mdi:cloud-upload"

    def __init__(self, entry):
        super().__init__(entry, "backup_now")

    async def async_press(self):
        await self.runtime.manual()
