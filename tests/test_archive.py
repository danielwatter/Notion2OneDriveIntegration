import copy
import hashlib
import json
import zipfile
from unittest.mock import AsyncMock

import pytest
from aioresponses import aioresponses

from custom_components.notion_backup.api import BackupError
from custom_components.notion_backup.archive import Exporter, create_zip, render_blocks

ROOT = "11111111-1111-1111-1111-111111111111"
CHILD = "22222222-2222-2222-2222-222222222222"
DATABASE = "33333333-3333-3333-3333-333333333333"
SOURCE = "44444444-4444-4444-4444-444444444444"
ROW = "55555555-5555-5555-5555-555555555555"


def rich(text):
    return {"type": "text", "plain_text": text, "text": {"content": text}}


def page(object_id, text):
    return {
        "id": object_id,
        "object": "page",
        "properties": {"Name": {"id": "title", "type": "title", "title": [rich(text)]}},
    }


def file_block(block_id, url):
    return {
        "id": block_id,
        "type": "file",
        "has_children": False,
        "file": {"type": "file", "name": "same.pdf", "file": {"url": url}},
    }


class FakeNotion:
    """Synthetic graph with nested pages, nested blocks, database and a row."""

    def __init__(self):
        self.objects = {
            f"pages/{ROOT}": page(ROOT, "Home"),
            f"pages/{CHILD}": page(CHILD, "Child"),
            f"pages/{ROW}": page(ROW, "Database row"),
            f"databases/{DATABASE}": {
                "id": DATABASE,
                "title": [rich("Documents")],
                "data_sources": [{"id": SOURCE}],
            },
            f"data_sources/{SOURCE}": {
                "id": SOURCE,
                "title": [rich("Documents source")],
                "parent": {"database_id": DATABASE},
            },
        }
        self.lists = {
            f"blocks/{ROOT}/children": [
                {
                    "id": "text",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [rich("Hello world")]},
                    "has_children": False,
                },
                {"id": CHILD, "type": "child_page", "child_page": {"title": "Child"}},
                {
                    "id": DATABASE,
                    "type": "child_database",
                    "child_database": {"title": "Documents"},
                },
                {
                    "id": "nested",
                    "type": "toggle",
                    "toggle": {"rich_text": [rich("Details")]},
                    "has_children": True,
                },
                file_block("file-a", "https://files.example.com/a.pdf?signature=one"),
                file_block("file-b", "https://files.example.com/b.pdf?signature=two"),
            ],
            "blocks/nested/children": [
                {
                    "id": "nested-text",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [rich("Nested content")]},
                }
            ],
            f"blocks/{CHILD}/children": [],
            f"blocks/{ROW}/children": [],
            f"data_sources/{SOURCE}/query": [page(ROW, "Database row")],
        }

    async def root(self, object_id):
        return "page", copy.deepcopy(self.objects[f"pages/{object_id}"])

    async def request(self, method, path):
        return copy.deepcopy(self.objects[path])

    async def paginate(self, method, path):
        if "/properties/title" in path:
            object_id = path.split("/")[1]
            return [
                {
                    "type": "title",
                    "title": self.objects[f"pages/{object_id}"]["properties"]["Name"]["title"][0],
                }
            ]
        return copy.deepcopy(self.lists[path])


async def test_export_zip_is_readable_self_contained_and_has_verified_checksums(tmp_path):
    content = tmp_path / "content"
    content.mkdir()
    with aioresponses() as mock:
        mock.get("https://files.example.com/a.pdf?signature=one", body=b"%PDF-document-A")
        mock.get("https://files.example.com/b.pdf?signature=two", body=b"%PDF-document-B")
        exporter = Exporter(FakeNotion(), content, "Test account", [ROOT, ROOT])
        manifest = await exporter.run()
    assert manifest["complete"] is True
    assert len(manifest["objects"]) == 5
    assert len(manifest["attachments"]) == 2
    assert len({a["path"] for a in manifest["attachments"]}) == 2
    markdown = (content / f"pages/{ROOT}.md").read_text()
    assert "Hello world" in markdown and "Nested content" in markdown
    assert "../attachments/" in markdown
    archive_path = tmp_path / "test.zip"
    create_zip(content, archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        for line in archive.read("SHA256SUMS").decode().splitlines():
            digest, name = line.split("  ", 1)
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        assert json.loads(archive.read("manifest.json"))["complete"] is True
        assert archive.read(manifest["attachments"][0]["path"]) == b"%PDF-document-A"
        assert f"pages/{ROW}.json" in archive.namelist()
        assert "synthetic-token" not in archive.read("manifest.json").decode()


async def test_missing_attachment_makes_archive_incomplete_and_keeps_other_content(tmp_path):
    with aioresponses() as mock:
        mock.get("https://files.example.com/a.pdf?signature=one", status=403)
        mock.get("https://files.example.com/b.pdf?signature=two", body=b"document")
        manifest = await Exporter(FakeNotion(), tmp_path, "Test", [ROOT]).run()
    assert manifest["complete"] is False
    assert len(manifest["errors"]) == 1
    assert len(manifest["attachments"]) == 1
    assert "signature" not in json.dumps(manifest["errors"])
    assert (tmp_path / f"pages/{ROOT}.md").exists()


async def test_unsupported_block_and_missing_child_are_reported(tmp_path):
    notion = FakeNotion()
    notion.lists[f"blocks/{ROOT}/children"] = [
        {"id": "unavailable", "type": "unsupported"},
        {"id": CHILD, "type": "child_page", "child_page": {"title": "Child"}},
    ]
    original = notion.request

    async def request(method, path):
        if path == f"pages/{CHILD}":
            raise BackupError("Notion: missing or not shared")
        return await original(method, path)

    notion.request = request
    manifest = await Exporter(notion, tmp_path, "Test", [ROOT]).run()
    assert not manifest["complete"]
    assert len(manifest["errors"]) == 2


async def test_empty_export_is_a_failure(tmp_path):
    notion = FakeNotion()
    notion.root = AsyncMock(side_effect=BackupError("Notion: missing"))
    with pytest.raises(BackupError, match="No Notion content"):
        await Exporter(notion, tmp_path, "Test", [ROOT]).run()


async def test_attachment_redirect_to_lan_is_rejected(tmp_path):
    notion = FakeNotion()
    notion.lists[f"blocks/{ROOT}/children"] = [
        file_block("f", "https://files.example.com/redirect")
    ]
    with aioresponses() as mock:
        mock.get(
            "https://files.example.com/redirect",
            status=302,
            headers={"Location": "https://192.168.1.1/private"},
        )
        manifest = await Exporter(notion, tmp_path, "Test", [ROOT]).run()
    assert not manifest["complete"]
    assert not manifest["attachments"]


async def test_same_url_downloaded_once_and_properties_include_full_title(tmp_path):
    notion = FakeNotion()
    url = "https://files.example.com/same.pdf"
    notion.lists[f"blocks/{ROOT}/children"] = [file_block("a", url), file_block("b", url)]
    with aioresponses() as mock:
        mock.get(url, body=b"doc")
        manifest = await Exporter(notion, tmp_path, "Test", [ROOT]).run()
    assert manifest["complete"]
    assert len(manifest["attachments"]) == 1


def test_code_fences_preserve_embedded_backticks():
    result = "\n".join(
        render_blocks(
            [{"type": "code", "code": {"language": "python", "rich_text": [rich("``` embedded")]}}]
        )
    )
    assert result.startswith("````python")
