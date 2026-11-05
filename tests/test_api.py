import asyncio
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.notion_backup.api import (
    ApiError,
    AuthenticationError,
    BackupError,
    MicrosoftAuth,
    NotionClient,
    OneDriveClient,
    PublicResolver,
    folder_parts,
    json_request,
    parse_roots,
    validate_download_url,
)
from custom_components.notion_backup.const import CHUNK_SIZE, GRAPH_API, MS_AUTH, NOTION_API

ROOT = "11111111-1111-1111-1111-111111111111"


def test_root_urls_ignore_database_view_and_deduplicate():
    url = "https://www.notion.so/Title-" + ROOT.replace("-", "") + "?v=" + "2" * 32
    assert parse_roots(f"{url}\n{ROOT}\n") == [ROOT]


@pytest.mark.parametrize("value", ["", "not-a-page", "https://notion.so/"])
def test_invalid_roots(value):
    with pytest.raises(ValueError):
        parse_roots(value)


@pytest.mark.parametrize("value", ["../x", "a//b", "x/..", "a:b", "x\\y", "x/ a", "x."])
def test_invalid_folders(value):
    with pytest.raises(ValueError):
        folder_parts(value)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a",
        "https://127.0.0.1/a",
        "https://[::1]/a",
        "https://169.254.169.254/a",
        "https://user:password@example.com/a",
    ],
)
def test_attachment_urls_reject_non_public_or_credentialed_targets(url):
    with pytest.raises(BackupError):
        validate_download_url(url)


async def test_dns_resolver_rejects_private_addresses():
    resolver = PublicResolver()
    try:
        with patch(
            "aiohttp.resolver.DefaultResolver.resolve",
            AsyncMock(return_value=[{"host": "192.168.1.1"}]),
        ):
            with pytest.raises(OSError):
                await resolver.resolve("public-looking.example")
    finally:
        await resolver.close()


async def test_notion_pagination_includes_every_page_and_stops_cycles():
    url = f"{NOTION_API}/blocks/{ROOT}/children"
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mock:
            mock.get(
                url + "?page_size=100",
                payload={"results": [{"id": "a"}], "has_more": True, "next_cursor": "next"},
            )
            mock.get(
                url + "?page_size=100&start_cursor=next",
                payload={"results": [{"id": "b"}], "has_more": False},
            )
            assert await NotionClient(session, "synthetic-token").paginate(
                "GET", f"blocks/{ROOT}/children"
            ) == [{"id": "a"}, {"id": "b"}]
        with aioresponses() as mock:
            mock.get(url + "?page_size=100", payload={"has_more": True, "next_cursor": "loop"})
            mock.get(
                url + "?page_size=100&start_cursor=loop",
                payload={"has_more": True, "next_cursor": "loop"},
            )
            with pytest.raises(BackupError, match="cursor"):
                await NotionClient(session, "synthetic-token").paginate(
                    "GET", f"blocks/{ROOT}/children"
                )


async def test_retry_after_is_honored_and_errors_never_expose_credentials():
    async with aiohttp.ClientSession() as session:
        with (
            aioresponses() as mock,
            patch(
                "custom_components.notion_backup.api.asyncio.sleep", new_callable=AsyncMock
            ) as sleep,
        ):
            mock.get("https://example.com/api", status=429, headers={"Retry-After": "7"})
            mock.get("https://example.com/api", payload={"ok": True})
            assert await json_request(
                session, "GET", "https://example.com/api", service="Test"
            ) == {"ok": True}
            sleep.assert_awaited_once_with(7)
        with aioresponses() as mock:
            mock.get(
                "https://example.com/api?secret=value",
                status=403,
                payload={"error": "secret-token"},
            )
            with pytest.raises(AuthenticationError) as error:
                await json_request(
                    session, "GET", "https://example.com/api?secret=value", service="Test"
                )
            assert "secret" not in str(error.value)


async def test_device_login_pending_and_denied():
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mock:
            mock.post(f"{MS_AUTH}/token", status=400, payload={"error": "authorization_pending"})
            assert await MicrosoftAuth(session, "client").exchange(device_code="synthetic") == {
                "pending": "authorization_pending"
            }
            mock.post(
                f"{MS_AUTH}/token",
                status=400,
                payload={"error": "invalid_grant", "error_description": "sensitive"},
            )
            with pytest.raises(AuthenticationError, match="expired"):
                await MicrosoftAuth(session, "client").exchange(refresh_token="synthetic")


async def test_refresh_rotation_is_saved_before_any_graph_request():
    events = []

    async def save(token):
        events.append(token)

    async with aiohttp.ClientSession() as session:
        drive = OneDriveClient(session, "client", "old-refresh", save)
        with aioresponses() as mock:
            mock.post(
                f"{MS_AUTH}/token",
                payload={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                },
            )
            mock.get(f"{GRAPH_API}/me/drive", status=503, repeat=True)
            with patch("custom_components.notion_backup.api.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(ApiError):
                    await drive.request("GET", "me/drive")
        assert events == ["new-refresh"]
        assert drive.refresh_token == "new-refresh"


async def test_folder_creation_and_reuse():
    async with aiohttp.ClientSession() as session:
        drive = OneDriveClient(session, "client", "refresh", AsyncMock())
        drive.token = AsyncMock(return_value="access")
        with aioresponses() as mock:
            mock.get(f"{GRAPH_API}/drives/drive/root:/Notion%20Backups:", status=404)
            mock.post(
                f"{GRAPH_API}/drives/drive/root/children", payload={"id": "base", "folder": {}}
            )
            mock.get(
                f"{GRAPH_API}/drives/drive/items/base:/account:",
                payload={"id": "account-id", "folder": {}},
            )
            assert await drive.folder("drive", ["Notion Backups", "account"]) == "account-id"


async def test_chunked_upload_ranges_no_bearer_on_upload_and_final_size_verification(tmp_path):
    data = b"x" * (CHUNK_SIZE + 17)
    path = tmp_path / "backup.zip"
    path.write_bytes(data)
    upload_url = "https://upload.example.com/signed"
    async with aiohttp.ClientSession() as session:
        drive = OneDriveClient(session, "client", "refresh", AsyncMock())
        drive.token = AsyncMock(return_value="synthetic-bearer")
        with aioresponses() as mock:
            mock.post(
                f"{GRAPH_API}/drives/drive/items/folder:/backup.zip:/createUploadSession",
                payload={"uploadUrl": upload_url},
            )
            mock.put(upload_url, status=202, payload={"nextExpectedRanges": [f"{CHUNK_SIZE}-"]})
            mock.put(upload_url, status=201, payload={"id": "uploaded", "size": len(data)})
            mock.get(
                f"{GRAPH_API}/drives/drive/items/uploaded",
                payload={"id": "uploaded", "size": len(data)},
            )
            assert (await drive.upload("drive", "folder", path))["size"] == len(data)
            calls = [
                call
                for (method, _), values in mock.requests.items()
                if method == "PUT"
                for call in values
            ]
            assert (
                calls[0].kwargs["headers"]["Content-Range"]
                == f"bytes 0-{CHUNK_SIZE - 1}/{len(data)}"
            )
            assert (
                calls[1].kwargs["headers"]["Content-Range"]
                == f"bytes {CHUNK_SIZE}-{len(data) - 1}/{len(data)}"
            )
            assert all("Authorization" not in c.kwargs["headers"] for c in calls)


async def test_upload_rejects_committed_size_mismatch(tmp_path):
    path = tmp_path / "backup.zip"
    path.write_bytes(b"zip")
    async with aiohttp.ClientSession() as session:
        drive = OneDriveClient(session, "client", "refresh", AsyncMock())
        drive.token = AsyncMock(return_value="access")
        with aioresponses() as mock:
            mock.post(
                f"{GRAPH_API}/drives/drive/items/folder:/backup.zip:/createUploadSession",
                payload={"uploadUrl": "https://upload.example.com/session"},
            )
            mock.put("https://upload.example.com/session", payload={"id": "item", "size": 3})
            mock.get(f"{GRAPH_API}/drives/drive/items/item", payload={"id": "item", "size": 2})
            with pytest.raises(BackupError, match="mismatch"):
                await drive.upload("drive", "folder", path)


async def test_cancellation_is_not_retried():
    async with aiohttp.ClientSession() as session:
        with aioresponses() as mock:
            mock.get("https://example.com/api", exception=asyncio.CancelledError())
            with pytest.raises(asyncio.CancelledError):
                await json_request(session, "GET", "https://example.com/api", service="Test")


async def test_linked_token_provider_owns_refresh_and_is_used_for_each_request():
    provider = AsyncMock(side_effect=["first-token", "renewed-token"])
    async with aiohttp.ClientSession() as session:
        drive = OneDriveClient(session, "", "", None, token_provider=provider)
        with (
            aioresponses() as mock,
            patch.object(drive.auth, "exchange", new_callable=AsyncMock) as exchange,
        ):
            mock.get(f"{GRAPH_API}/me/drive", payload={"id": "drive"}, repeat=True)
            await drive.request("GET", "me/drive")
            await drive.request("GET", "me/drive")
            calls = next(iter(mock.requests.values()))
            assert calls[0].kwargs["headers"]["Authorization"] == "Bearer first-token"
            assert calls[1].kwargs["headers"]["Authorization"] == "Bearer renewed-token"
            exchange.assert_not_called()
    assert provider.await_count == 2


async def test_linked_folder_is_created_inside_app_root_not_drive_root():
    async with aiohttp.ClientSession() as session:
        drive = OneDriveClient(
            session, "", "", None, token_provider=AsyncMock(return_value="access")
        )
        with aioresponses() as mock:
            mock.get(f"{GRAPH_API}/drives/drive/items/app-root:/Notion%20Backups:", status=404)
            mock.post(
                f"{GRAPH_API}/drives/drive/items/app-root/children",
                payload={"id": "notion-folder", "folder": {}},
            )
            assert (
                await drive.folder("drive", ["Notion Backups"], root_id="app-root")
                == "notion-folder"
            )
            assert all("/root" not in str(url) for _, url in mock.requests)


async def test_personal_app_folder_uses_me_route_and_closed_path_address():
    async with aiohttp.ClientSession() as session:
        drive = OneDriveClient(
            session,
            "",
            "",
            None,
            token_provider=AsyncMock(return_value="access"),
            use_me=True,
        )
        with aioresponses() as mock:
            mock.get(f"{GRAPH_API}/me/drive/items/app-root:/DHW%20Holding:", status=404)
            mock.post(
                f"{GRAPH_API}/me/drive/items/app-root/children",
                payload={"id": "folder", "folder": {}},
            )
            assert (
                await drive.folder("validated-drive-id", ["DHW Holding"], root_id="app-root")
                == "folder"
            )
            assert all("/drives/" not in str(url) for _, url in mock.requests)


async def test_graph_error_reports_operation_and_safe_code_only():
    async with aiohttp.ClientSession() as session:
        drive = OneDriveClient(
            session, "", "", None, token_provider=AsyncMock(return_value="secret")
        )
        with aioresponses() as mock:
            mock.get(
                f"{GRAPH_API}/probe",
                status=400,
                payload={
                    "error": {"code": "invalidRequest", "message": "secret-token and private-url"}
                },
            )
            with pytest.raises(ApiError) as error:
                await drive.request("GET", "probe", operation="folder lookup")
            assert str(error.value) == "OneDrive folder lookup: HTTP 400 (invalidRequest)"
            assert "secret" not in str(error.value)


async def test_personal_upload_session_payload_matches_ha_sdk_and_probe_is_cancelled():
    async with aiohttp.ClientSession() as session:
        drive = OneDriveClient(
            session,
            "",
            "",
            None,
            token_provider=AsyncMock(return_value="access"),
            use_me=True,
        )
        url = f"{GRAPH_API}/me/drive/items/folder:/connection-check.zip:/createUploadSession"
        upload_url = "https://upload.example.com/signed-session"
        with aioresponses() as mock:
            mock.post(url, payload={"uploadUrl": upload_url})
            mock.delete(upload_url, status=204)
            await drive.check_upload("validated-drive", "folder", "connection-check.zip")
            post_call = next(
                c for (method, _), calls in mock.requests.items() if method == "POST" for c in calls
            )
            assert post_call.kwargs["json"] == {
                "item": {"@microsoft.graph.conflictBehavior": "rename"},
                "deferCommit": False,
                "name": "connection-check.zip",
            }
            delete_call = next(
                c
                for (method, _), calls in mock.requests.items()
                if method == "DELETE"
                for c in calls
            )
            assert "Authorization" not in delete_call.kwargs.get("headers", {})
            assert all(method != "PUT" for method, _ in mock.requests)


async def test_personal_folder_id_is_not_double_encoded_for_upload():
    async with aiohttp.ClientSession() as session:
        drive = OneDriveClient(
            session,
            "",
            "",
            None,
            token_provider=AsyncMock(return_value="access"),
            use_me=True,
        )
        with aioresponses() as mock:
            mock.get(f"{GRAPH_API}/me/drive/items/DRIVE%21app:/Backups:", status=404)
            mock.post(
                f"{GRAPH_API}/me/drive/items/DRIVE%21app/children",
                payload={"id": "DRIVE!123", "folder": {}},
            )
            folder_id = await drive.folder("drive", ["Backups"], root_id="DRIVE!app")
            assert folder_id == "DRIVE!123"
            mock.post(
                f"{GRAPH_API}/me/drive/items/DRIVE%21123:/backup.zip:/createUploadSession",
                payload={"uploadUrl": "https://upload.example.com/session"},
            )
            await drive.prepare_upload("drive", folder_id, "backup.zip")
            assert all("%2521" not in str(url) for _, url in mock.requests)
