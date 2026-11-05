"""Constants for Notion Backup."""

DOMAIN = "notion_backup"
VERSION = "0.1.2"
NOTION_VERSION = "2025-09-03"
NOTION_API = "https://api.notion.com/v1"
GRAPH_API = "https://graph.microsoft.com/v1.0"
MS_AUTH = "https://login.microsoftonline.com/common/oauth2/v2.0"
MS_SCOPE = "https://graph.microsoft.com/Files.ReadWrite offline_access"
CHUNK_SIZE = 10 * 320 * 1024  # Graph requires multiples of 320 KiB.
MAX_ATTACHMENT_BYTES = 1024 * 1024 * 1024
DEFAULT_FOLDER = "Notion Backups"
