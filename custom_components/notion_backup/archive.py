"""Export an explicit set of Notion roots and create a self-contained ZIP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import zipfile
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

import aiohttp

from .api import BackupError, PublicResolver, validate_download_url
from .const import MAX_ATTACHMENT_BYTES, NOTION_VERSION, VERSION


def safe_name(value: str, fallback="untitled") -> str:
    value = re.sub(r"[^\w. -]", "_", value, flags=re.UNICODE).strip(" .")
    return value[:100] or fallback


def plain(items) -> str:
    return "".join(
        item.get("plain_text", item.get("text", {}).get("content", "")) for item in items
    )


def title(obj) -> str:
    if isinstance(obj.get("title"), list):
        return plain(obj["title"]) or "Untitled"
    for prop in obj.get("properties", {}).values():
        if prop.get("type") == "title":
            return plain(prop.get("title", [])) or "Untitled"
    return "Untitled"


def render_blocks(blocks, depth=0):
    """Readable Markdown; the accompanying JSON remains the authoritative copy."""
    lines = []
    for block in blocks:
        kind = block.get("type", "unsupported")
        body = block.get(kind, {})
        text = plain(body.get("rich_text", []))
        if kind.startswith("heading_"):
            text = "#" * int(kind[-1]) + " " + text
        elif kind == "bulleted_list_item":
            text = "- " + text
        elif kind == "numbered_list_item":
            text = "1. " + text
        elif kind == "to_do":
            text = f"- [{'x' if body.get('checked') else ' '}] {text}"
        elif kind in {"quote", "callout"}:
            text = "> " + text.replace("\n", "\n> ")
        elif kind == "code":
            fence = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", text)), default=3))
            text = f"{fence}{body.get('language', '')}\n{text}\n{fence}"
        elif kind == "divider":
            text = "---"
        elif kind in {"child_page", "child_database"}:
            area = "pages" if kind == "child_page" else "databases"
            text = f"[{body.get('title', kind)}](../{area}/{block['id']}.md)"
        elif kind in {"image", "file", "pdf", "video", "audio"}:
            target = body.get("_backup_path") or body.get(body.get("type"), {}).get("url", "")
            if body.get("_backup_path"):
                target = "../" + target
            caption = plain(body.get("caption", [])) or body.get("name", kind)
            text = f"{'!' if kind == 'image' else ''}[{caption}]({quote(target, safe='/:?=&%')})"
        elif kind in {"bookmark", "embed", "link_preview"}:
            text = body.get("url", "")
        elif kind == "equation":
            text = f"$$\n{body.get('expression', '')}\n$$"
        elif kind == "table_row":
            cells = [
                plain(cell).replace("|", "\\|").replace("\n", "<br>")
                for cell in body.get("cells", [])
            ]
            text = "| " + " | ".join(cells) + " |"
        elif kind == "link_to_page":
            target_id = body.get(body.get("type"), "")
            text = f"[Page reference](../pages/{target_id}.md)"
        elif not text and kind not in {"column_list", "column", "table", "synced_block"}:
            text = f"[{kind}: see JSON]"
        if text:
            lines.append("  " * depth + text)
        children = block.get("_backup_children", [])
        if children:
            nested = render_blocks(
                children,
                depth + (kind in {"bulleted_list_item", "numbered_list_item", "toggle", "to_do"}),
            )
            if kind == "table" and nested:
                width = body.get("table_width", 1)
                nested.insert(1, "| " + " | ".join(["---"] * width) + " |")
            lines.extend(nested)
    return lines


class Exporter:
    """Per-run exporter. Roots, visited objects and credentials never cross entries."""

    def __init__(self, notion, directory: Path, label: str, roots: list[str]):
        self.notion, self.directory, self.label, self.roots = notion, directory, label, roots
        self.queue = deque()
        self.visited = set()
        self.manifest = {
            "format_version": 1,
            "integration_version": VERSION,
            "notion_api_version": NOTION_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "label": label,
            "roots": roots,
            "objects": [],
            "attachments": [],
            "errors": [],
            "external_links": [],
            "scope": (
                "Selected roots and their accessible descendants; "
                "relation targets are not traversed."
            ),
            "limitations": [
                "Not a workspace restore image; permissions, history and comments "
                "are not exported.",
                "Notion API unsupported or inaccessible content is reported as incomplete.",
                "External bookmarks/embeds remain links. "
                "Public external file objects are downloaded.",
                "Export is read over time, not a transactionally consistent snapshot.",
            ],
        }
        self.download_session = None
        self._attachments = {}

    def error(self, location, error):
        self.manifest["errors"].append({"location": location, "reason": str(error)})

    async def write(self, relative, data):
        def save():
            target = self.directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(data, encoding="utf-8")

        await asyncio.to_thread(save)

    async def write_json(self, relative, data):
        # Serialization and IO both run outside the event loop.
        await asyncio.to_thread(self._write_json, relative, data)

    def _write_json(self, relative, data):
        target = self.directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def run(self):
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=PublicResolver()),
            timeout=aiohttp.ClientTimeout(total=900, sock_read=60),
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
        ) as self.download_session:
            for root_id in self.roots:
                try:
                    kind, obj = await self.notion.root(root_id)
                    self.queue.append((kind, obj["id"]))
                except BackupError as err:
                    self.error(root_id, err)
            while self.queue:
                kind, object_id = self.queue.popleft()
                if (kind, object_id) in self.visited:
                    continue
                self.visited.add((kind, object_id))
                try:
                    await self.export_object(kind, object_id)
                except BackupError as err:
                    self.error(f"{kind}/{object_id}", err)
        if not self.manifest["objects"]:
            raise BackupError("No Notion content exported; check roots and sharing permissions")
        self.manifest["complete"] = not self.manifest["errors"]
        await self.write_json("manifest.json", self.manifest)
        index = [f"# {self.label}", "", f"Created: {self.manifest['created_at']}", ""]
        if not self.manifest["complete"]:
            index += ["**INCOMPLETE BACKUP: inspect errors in manifest.json.**", ""]
        index += [f"- [{item['title']}]({item['path']})" for item in self.manifest["objects"]]
        await self.write("index.md", "\n".join(index) + "\n")
        return self.manifest

    async def export_object(self, kind, object_id):
        area = {"page": "pages", "database": "databases", "data_source": "data_sources"}[kind]
        obj = await self.notion.request("GET", f"{area}/{object_id}")
        if kind == "page":
            # Page retrieval can truncate title/rich_text/people/relation values.
            property_items = {}
            for name, prop in obj.get("properties", {}).items():
                if prop.get("type") in {"title", "rich_text", "people", "relation"}:
                    pid = quote(unquote(prop["id"]), safe="")
                    property_items[name] = await self.notion.paginate(
                        "GET",
                        f"pages/{object_id}/properties/{pid}",
                    )
            obj["_backup_property_items"] = property_items
            # Restore full text for the readable title/properties as well.
            for name, items in property_items.items():
                prop = obj["properties"][name]
                prop[prop["type"]] = [item[prop["type"]] for item in items if prop["type"] in item]
            await self.attachments(obj, object_id)
            obj["_backup_blocks"] = await self.blocks(object_id)
            markdown = [f"# {title(obj)}", ""]
            for name, prop in obj.get("properties", {}).items():
                if prop.get("type") != "title":
                    markdown += [f"**{name}:** {self.property_text(prop)}", ""]
            markdown += render_blocks(obj["_backup_blocks"])
        elif kind == "database":
            for source in obj.get("data_sources", []):
                self.queue.append(("data_source", source["id"]))
            await self.attachments(obj, object_id)
            markdown = [f"# {title(obj)}", ""] + [
                f"- [Data source](../data_sources/{s['id']}.md)"
                for s in obj.get("data_sources", [])
            ]
        else:
            if database_id := obj.get("parent", {}).get("database_id"):
                self.queue.append(("database", database_id))
            rows = await self.notion.paginate("POST", f"data_sources/{object_id}/query")
            obj["_backup_page_ids"] = [row["id"] for row in rows]
            for row in rows:
                self.queue.append(("page", row["id"]))
            await self.attachments(obj, object_id)
            markdown = [f"# {title(obj)}", ""] + [
                f"- [{title(row)}](../pages/{row['id']}.md)" for row in rows
            ]
        await self.write_json(f"{area}/{object_id}.json", obj)
        await self.write(f"{area}/{object_id}.md", "\n".join(markdown) + "\n")
        self.manifest["objects"].append(
            {
                "id": object_id,
                "type": kind,
                "title": title(obj),
                "path": f"{area}/{object_id}.md",
            }
        )

    @staticmethod
    def property_text(prop):
        kind = prop.get("type")
        value = prop.get(kind)
        if kind in {"title", "rich_text"}:
            return plain(value or [])
        if kind == "files":
            return ", ".join(
                f"[{f.get('name', 'file')}](../{quote(f['_backup_path'])})"
                if f.get("_backup_path")
                else f.get("name", "file")
                for f in value or []
            )
        return json.dumps(value, ensure_ascii=False)

    async def blocks(self, block_id, depth=0):
        if depth > 100:
            raise BackupError("Notion nesting exceeds 100 levels")
        result = await self.notion.paginate("GET", f"blocks/{block_id}/children")
        for block in result:
            kind = block.get("type")
            if kind == "child_page":
                self.queue.append(("page", block["id"]))
            elif kind == "child_database":
                self.queue.append(("database", block["id"]))
            elif kind == "unsupported":
                self.error(block["id"], "Notion API returned unsupported content")
            if kind in {"bookmark", "embed", "link_preview", "link_to_page"}:
                self.manifest["external_links"].append({"id": block["id"], "type": kind})
            await self.attachments(block, block["id"])
            if block.get("has_children") and kind not in {"child_page", "child_database"}:
                try:
                    block["_backup_children"] = await self.blocks(block["id"], depth + 1)
                except BackupError as err:
                    self.error(block["id"], err)
        return result

    async def attachments(self, obj, owner, path=""):
        if isinstance(obj, list):
            for i, value in enumerate(obj):
                await self.attachments(value, owner, f"{path}/{i}")
        elif isinstance(obj, dict):
            kind = obj.get("type")
            if (
                kind in {"file", "external"}
                and isinstance(obj.get(kind), dict)
                and "url" in obj[kind]
            ):
                url = obj[kind]["url"]
                key = f"{owner}:{path}"
                try:
                    relative = await self.download(url, obj.get("name"), key)
                    obj["_backup_path"] = relative
                except BackupError as err:
                    self.error(f"attachment/{owner}{path}", err)
                return
            if kind == "file_upload":
                self.error(f"attachment/{owner}{path}", "File upload has no downloadable URL")
            for key, value in list(obj.items()):
                if not key.startswith("_backup"):
                    await self.attachments(value, owner, f"{path}/{key}")

    async def download(self, url, name, key):
        if url in self._attachments:
            return self._attachments[url]
        basename = safe_name(name or unquote(urlsplit(url).path.split("/")[-1]), "attachment")
        relative = f"attachments/{hashlib.sha256(key.encode()).hexdigest()[:20]}_{basename}"
        target = self.directory / relative
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        current = url
        try:
            # Validate every redirect; the resolver also rejects private DNS results.
            for _ in range(6):
                validate_download_url(current)
                async with self.download_session.get(current, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        current = urljoin(current, response.headers.get("Location", ""))
                        continue
                    if response.status != 200:
                        raise BackupError(f"Attachment download failed (HTTP {response.status})")
                    if (response.content_length or 0) > MAX_ATTACHMENT_BYTES:
                        raise BackupError("Attachment exceeds the 1 GiB safety limit")
                    handle = await asyncio.to_thread(target.open, "wb")
                    size, digest = 0, hashlib.sha256()
                    try:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_ATTACHMENT_BYTES:
                                raise BackupError("Attachment exceeds the 1 GiB safety limit")
                            digest.update(chunk)
                            await asyncio.to_thread(handle.write, chunk)
                    finally:
                        await asyncio.to_thread(handle.close)
                    if (
                        response.content_length is not None
                        and response.headers.get("Content-Encoding") is None
                        and size != response.content_length
                    ):
                        raise BackupError("Attachment download was truncated")
                    self.manifest["attachments"].append(
                        {
                            "path": relative,
                            "size": size,
                            "sha256": digest.hexdigest(),
                        }
                    )
                    self._attachments[url] = relative
                    return relative
            raise BackupError("Too many attachment redirects")
        except (aiohttp.ClientError, TimeoutError, OSError):
            await asyncio.to_thread(target.unlink, missing_ok=True)
            raise BackupError("Attachment download connection or disk error") from None
        except BackupError:
            await asyncio.to_thread(target.unlink, missing_ok=True)
            raise


def create_zip(directory: Path, output: Path):
    """Create checksums, compress, and read every ZIP member back before upload."""
    checksums = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            checksums.append(f"{digest}  {path.relative_to(directory).as_posix()}")
    (directory / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=5, allowZip64=True
    ) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(directory).as_posix())
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise BackupError("ZIP verification failed")
