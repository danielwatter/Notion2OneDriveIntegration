"""Small HTTP clients. No Home Assistant dependency, credentials never enter errors."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID

import aiohttp

from .const import CHUNK_SIZE, GRAPH_API, MS_AUTH, MS_SCOPE, NOTION_API, NOTION_VERSION


class BackupError(Exception):
    """An error whose message is safe to show in HA."""


class AuthenticationError(BackupError):
    """Credentials are expired, invalid or revoked."""


class ApiError(BackupError):
    """HTTP failure with status, without response payload or signed URLs."""

    def __init__(self, service: str, status: int, code: str | None = None):
        self.status = status
        self.code = code
        suffix = f" ({code})" if code else ""
        super().__init__(f"{service}: HTTP {status}{suffix}")


def notion_id(value: str) -> str:
    """Accept a Notion ID or a normal page/database URL (ignoring a view query)."""
    import re

    value = value.strip()
    if value.startswith(("https://", "http://")):
        value = urlsplit(value).path.rstrip("/").split("/")[-1]
    match = re.search(
        r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$", value
    )
    if not match:
        raise ValueError("Invalid Notion ID")
    return str(UUID(match[1]))


def parse_roots(value: str) -> list[str]:
    roots = list(dict.fromkeys(notion_id(line) for line in value.splitlines() if line.strip()))
    if not roots:
        raise ValueError("At least one root is required")
    return roots


def folder_parts(value: str) -> list[str]:
    """OneDrive path components, with traversal and illegal names rejected."""
    parts = value.strip().strip("/").split("/")
    if (
        any(
            not p
            or p in {".", ".."}
            or any(c in p for c in '\\:*?"<>|')
            or p != p.strip()
            or p.endswith(".")
            or len(p) > 100
            for p in parts
        )
        or len(parts) > 8
    ):
        raise ValueError("Invalid OneDrive folder")
    return parts


async def json_request(session, method, url, *, service, **kwargs):
    """Retry rate limits and temporary errors, with bounded individual waits."""
    for attempt in range(5):
        try:
            async with session.request(
                method, url, timeout=aiohttp.ClientTimeout(total=90), **kwargs
            ) as response:
                if response.status in (401, 403):
                    raise AuthenticationError(f"{service}: access denied (HTTP {response.status})")
                if response.status == 429 or response.status >= 500:
                    if attempt < 4:
                        try:
                            delay = float(response.headers.get("Retry-After", 2**attempt))
                        except ValueError:
                            delay = 2**attempt
                        await asyncio.sleep(min(max(delay, 1), 60))
                        continue
                if response.status >= 400:
                    code = None
                    try:
                        payload = await response.json()
                        candidate = payload.get("error", {}).get("code")
                        if isinstance(candidate, str) and re.fullmatch(
                            r"[A-Za-z0-9_]{1,80}", candidate
                        ):
                            code = candidate
                    except (aiohttp.ClientError, ValueError, TypeError, AttributeError):
                        pass
                    raise ApiError(service, response.status, code)
                return await response.json()
        except (aiohttp.ClientError, TimeoutError):
            if attempt == 4:
                raise BackupError(f"{service}: connection failed") from None
            await asyncio.sleep(2**attempt)
    raise BackupError(f"{service}: retry limit exceeded")


class NotionClient:
    def __init__(self, session, token):
        self.session = session
        self.headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION}
        self._last_request = 0.0

    async def request(self, method, path, **kwargs):
        # Stay below the standard 3 requests/second budget.
        await asyncio.sleep(max(0, 0.36 - (time.monotonic() - self._last_request)))
        self._last_request = time.monotonic()
        return await json_request(
            self.session,
            method,
            f"{NOTION_API}/{path}",
            service="Notion",
            headers=self.headers,
            **kwargs,
        )

    async def identity(self):
        return await self.request("GET", "users/me")

    async def paginate(self, method, path):
        results, cursor, seen = [], None, set()
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = await self.request(
                method, path, **{"json" if method == "POST" else "params": params}
            )
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                return results
            cursor = data.get("next_cursor")
            if not cursor or cursor in seen:
                raise BackupError("Notion: invalid pagination cursor")
            seen.add(cursor)

    async def root(self, object_id):
        for kind, endpoint in (
            ("page", "pages"),
            ("database", "databases"),
            ("data_source", "data_sources"),
        ):
            try:
                return kind, await self.request("GET", f"{endpoint}/{object_id}")
            except ApiError as err:
                if err.status != 404:
                    raise
        raise BackupError(f"Notion: root {object_id} is missing or not shared")


class MicrosoftAuth:
    def __init__(self, session, client_id):
        self.session, self.client_id = session, client_id

    async def device_code(self):
        return await json_request(
            self.session,
            "POST",
            f"{MS_AUTH}/devicecode",
            service="Microsoft login",
            data={"client_id": self.client_id, "scope": MS_SCOPE},
        )

    async def exchange(self, **data):
        try:
            async with self.session.post(
                f"{MS_AUTH}/token",
                data={"client_id": self.client_id, **data},
                timeout=aiohttp.ClientTimeout(total=45),
            ) as response:
                payload = await response.json()
                if response.status >= 500 or response.status == 429:
                    raise BackupError("Microsoft login temporarily unavailable")
                if "error" in payload:
                    code = payload["error"]
                    if code in {"authorization_pending", "slow_down"}:
                        return {"pending": code}
                    raise AuthenticationError("Microsoft login expired, denied or invalid")
                if response.status >= 400 or not payload.get("access_token"):
                    raise AuthenticationError("Microsoft login failed")
                return payload
        except (aiohttp.ClientError, TimeoutError):
            raise BackupError("Microsoft login connection failed") from None


class OneDriveClient:
    def __init__(
        self,
        session,
        client_id,
        refresh_token,
        save_refresh: Callable[[str], Awaitable[None]],
        *,
        token_provider: Callable[[], Awaitable[str]] | None = None,
        use_me: bool = False,
    ):
        self.session = session
        self.auth = MicrosoftAuth(session, client_id)
        self.refresh_token = refresh_token
        self.save_refresh = save_refresh
        self.access_token = ""
        self.expires = 0.0
        self.token_provider = token_provider
        self.use_me = use_me

    async def token(self):
        if self.token_provider is not None:
            # HA owns authentication and token rotation for a linked OneDrive entry.
            return await self.token_provider()
        if time.monotonic() >= self.expires:
            data = await self.auth.exchange(
                grant_type="refresh_token",
                refresh_token=self.refresh_token,
                scope=MS_SCOPE,
            )
            if not data.get("access_token"):
                raise AuthenticationError("Microsoft: please sign in again")
            if new_refresh := data.get("refresh_token"):
                # Persist rotation before any subsequent upload can fail.
                await self.save_refresh(new_refresh)
                self.refresh_token = new_refresh
            self.access_token = data["access_token"]
            self.expires = time.monotonic() + max(0, data.get("expires_in", 3600) - 120)
        return self.access_token

    async def request(self, method, path, *, operation=None, **kwargs):
        return await json_request(
            self.session,
            method,
            f"{GRAPH_API}/{path}",
            service=f"OneDrive {operation}" if operation else "OneDrive",
            headers={"Authorization": f"Bearer {await self.token()}"},
            **kwargs,
        )

    def drive_base(self, drive_id):
        # App-folder-scoped personal accounts use the same route as HA's SDK.
        return "me/drive" if self.use_me else f"drives/{quote(drive_id, safe='')}"

    async def folder(self, drive_id, parts, *, root_id=None):
        parent = f"items/{quote(root_id, safe='')}" if root_id else "root"
        for part in parts:
            base = f"{self.drive_base(drive_id)}/{parent}"
            item_path = f"{base}:/{quote(part, safe='')}:"
            try:
                item = await self.request("GET", item_path, operation="folder lookup")
            except ApiError as err:
                if err.status != 404:
                    raise
                try:
                    item = await self.request(
                        "POST",
                        f"{base}/children",
                        operation="folder creation",
                        json={
                            "name": part,
                            "folder": {},
                            "@microsoft.graph.conflictBehavior": "fail",
                        },
                    )
                except ApiError as conflict:
                    if conflict.status != 409:
                        raise
                    item = await self.request("GET", item_path, operation="folder lookup")
            if "folder" not in item:
                raise BackupError("OneDrive: backup path contains a file")
            parent = f"items/{quote(item['id'], safe='')}"
        return parent.split("/")[-1]

    async def upload(self, drive_id: str, folder_id: str, path: Path):
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        endpoint = (
            f"{self.drive_base(drive_id)}/items/{quote(folder_id, safe='')}:"
            f"/{quote(path.name, safe='')}:/createUploadSession"
        )
        session = await self.request(
            "POST",
            endpoint,
            operation="upload session creation",
            json={
                "item": {"@microsoft.graph.conflictBehavior": "fail", "name": path.name},
            },
        )
        upload_url = session["uploadUrl"]
        if urlsplit(upload_url).scheme != "https":
            raise BackupError("OneDrive: invalid upload URL")
        handle = await asyncio.to_thread(path.open, "rb")
        try:
            offset, result = 0, None
            while offset < size:
                chunk = await asyncio.to_thread(handle.read, CHUNK_SIZE)
                if not chunk:
                    raise BackupError("Local ZIP changed during upload")
                # uploadUrl is preauthenticated: do not send the Graph bearer token.
                result = await json_request(
                    self.session,
                    "PUT",
                    upload_url,
                    service="OneDrive upload",
                    data=chunk,
                    headers={"Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}"},
                )
                offset += len(chunk)
                if offset < size:
                    ranges = result.get("nextExpectedRanges", [])
                    if not ranges or int(ranges[0].split("-")[0]) != offset:
                        raise BackupError("OneDrive: upload offset mismatch; retry backup")
            if not result or not result.get("id") or result.get("size") != size:
                raise BackupError("OneDrive: uploaded ZIP size could not be verified")
            # Check the committed item, rather than treating chunk acceptance as completion.
            item = await self.request(
                "GET",
                f"{self.drive_base(drive_id)}/items/{quote(result['id'], safe='')}",
                operation="upload verification",
            )
            if item.get("size") != size:
                raise BackupError("OneDrive: committed ZIP size mismatch")
            return item
        finally:
            await asyncio.to_thread(handle.close)


def validate_download_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise BackupError("Attachment requires a public HTTPS URL")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise BackupError("Attachment points to a non-public address")


class PublicResolver(aiohttp.resolver.DefaultResolver):
    """Prevent a page attachment from accessing HA/LAN/metadata services."""

    async def resolve(self, host, port=0, family=0):
        records = await super().resolve(host, port, family)
        if not records or any(not ipaddress.ip_address(r["host"]).is_global for r in records):
            raise OSError("Non-public attachment address")
        return records
