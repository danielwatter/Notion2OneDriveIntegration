"""Backup status and last verified successful upload."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.util import dt as dt_util

from .entity import BackupEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([StatusSensor(entry), LastSuccessSensor(entry)])


class StatusSensor(BackupEntity, SensorEntity):
    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "running", "completed", "incomplete", "failed", "interrupted"]
    _attr_icon = "mdi:archive-check"

    def __init__(self, entry):
        super().__init__(entry, "status")

    @property
    def native_value(self):
        return self.runtime.state["status"]

    @property
    def extra_state_attributes(self):
        return {key: value for key, value in self.runtime.state.items() if key != "status"}


class LastSuccessSensor(BackupEntity, SensorEntity):
    _attr_translation_key = "last_success"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry):
        super().__init__(entry, "last_success")

    @property
    def native_value(self):
        value = self.runtime.state.get("last_success")
        return dt_util.parse_datetime(value) if value else None
