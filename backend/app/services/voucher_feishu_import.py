"""Read-only Feishu attachment import helpers for the voucher module.

This module deliberately does not reuse or change the scheduled Feishu sync
workflow.  A voucher import always obtains a fresh tenant token, reads one
approval instance, and downloads only the short-lived attachment URLs returned
by that response.
"""

from __future__ import annotations

import base64
import binascii
import csv
import ipaddress
import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from fastapi.concurrency import run_in_threadpool

from app.config import settings


# Feishu's approval API examples use this dedicated ByteDance CDN hostname for
# approval attachments.  Keep it exact: allowing all of ``byteimg.com`` would
# unnecessarily widen the SSRF allow-list.
MAX_ATTACHMENTS_PER_IMPORT = 50
MAX_TOTAL_IMPORT_BYTES = 100 * 1024 * 1024
MAX_COMMENT_PAGES = 100


class FeishuImportError(RuntimeError):
    """An actionable Feishu API or attachment download failure."""


@dataclass(frozen=True)
class FeishuAttachment:
    source: str
    filename: str | None
    url: str | None
    declared_type: str | None = None
    declared_size: int | None = None


def _new_http_client() -> httpx.AsyncClient:
    raise FeishuImportError("本地空数据版：外部附件、评论和下载连接已停用")


def _decode_json(value: Any) -> Any:
    """Decode the nested JSON strings used by Feishu form controls."""
    while isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped[0] not in "[{\"":
            break
        try:
            decoded = json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            break
        if decoded == value:
            break
        value = decoded
    return value


def _coerce_size(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _metadata_from_node(node: Any) -> dict[str, Any]:
    node = _decode_json(node)
    if not isinstance(node, dict):
        return {}
    filename = next(
        (
            str(node[key]).strip()
            for key in ("title", "name", "file_name", "filename", "fileName")
            if node.get(key) not in (None, "")
        ),
        None,
    )
    url = next(
        (
            str(node[key]).strip()
            for key in ("url", "download_url", "downloadUrl")
            if node.get(key) not in (None, "")
        ),
        None,
    )
    declared_type = next(
        (
            str(node[key]).strip()
            for key in ("type", "mime_type", "mimeType", "content_type")
            if node.get(key) not in (None, "")
        ),
        None,
    )
    size = next(
        (
            _coerce_size(node[key])
            for key in ("file_size", "size", "fileSize")
            if node.get(key) not in (None, "")
        ),
        None,
    )
    return {
        "filename": filename,
        "url": url,
        "declared_type": declared_type,
        "declared_size": size,
    }


def _walk(node: Any) -> Iterable[Any]:
    node = _decode_json(node)
    yield node
    if isinstance(node, dict):
        for child in node.values():
            yield from _walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk(child)


def _ext_scalar_filenames(value: str) -> list[str]:
    """Parse legacy ``ext`` filename strings without evaluating them.

    The official response can contain a single CSV-like string such as
    ``'invoice-1.pdf','invoice-2.pdf'`` instead of a JSON list.
    """
    text = value.strip()
    if not text or text.startswith(("http://", "https://")):
        return []
    try:
        parsed = next(
            csv.reader(
                [text],
                delimiter=",",
                quotechar="'",
                skipinitialspace=True,
                strict=True,
            )
        )
    except (csv.Error, StopIteration):
        parsed = [text]
    result: list[str] = []
    for part in parsed:
        filename = part.strip().strip("\"'").strip()
        if filename and Path(filename).suffix:
            result.append(filename)
    return result


def _form_control_attachments(control: dict[str, Any]) -> list[FeishuAttachment]:
    """Pair names in ``ext`` with the 12-hour URLs in ``value``."""
    ext_rows: list[dict[str, Any]] = []
    ext_scalar_names: list[str] = []
    for node in _walk(control.get("ext")):
        if isinstance(node, dict):
            metadata = _metadata_from_node(node)
            if metadata.get("filename"):
                ext_rows.append(metadata)
        elif isinstance(node, str):
            # Feishu returns both plain names and a legacy single-quoted,
            # comma-separated representation here.
            ext_scalar_names.extend(_ext_scalar_filenames(node))

    value_rows: list[dict[str, Any]] = []
    for node in _walk(control.get("value")):
        if isinstance(node, dict):
            metadata = _metadata_from_node(node)
            if metadata.get("url"):
                value_rows.append(metadata)
        elif isinstance(node, str) and node.strip().startswith(("https://", "http://")):
            value_rows.append(
                {
                    "filename": None,
                    "url": node.strip(),
                    "declared_type": None,
                    "declared_size": None,
                }
            )

    # Recursive walking sees a metadata object's URL both in the object and as
    # its scalar child.  Preserve order while removing that structural duplicate.
    unique_values: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for row in value_rows:
        url = str(row.get("url") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        unique_values.append(row)

    names = ext_rows or [
        {
            "filename": name,
            "url": None,
            "declared_type": None,
            "declared_size": None,
        }
        for name in ext_scalar_names
    ]
    result: list[FeishuAttachment] = []
    if unique_values:
        for index, value_row in enumerate(unique_values):
            ext_row = names[index] if index < len(names) else (names[0] if len(names) == 1 else {})
            result.append(
                FeishuAttachment(
                    source="form",
                    filename=value_row.get("filename") or ext_row.get("filename"),
                    url=value_row.get("url"),
                    declared_type=value_row.get("declared_type") or ext_row.get("declared_type"),
                    declared_size=value_row.get("declared_size")
                    if value_row.get("declared_size") is not None
                    else ext_row.get("declared_size"),
                )
            )
    else:
        result.extend(
            FeishuAttachment(
                source="form",
                filename=row.get("filename"),
                url=None,
                declared_type=row.get("declared_type"),
                declared_size=row.get("declared_size"),
            )
            for row in names
        )
    return result


def _container_rows(value: Any) -> list[dict[str, Any]]:
    value = _decode_json(value)
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("items", "list", "records", "comments", "operation_records"):
            nested = _decode_json(value.get(key))
            if isinstance(nested, list):
                return [row for row in nested if isinstance(row, dict)]
        return [value]
    return []


def _file_rows(value: Any, source: str) -> list[FeishuAttachment]:
    value = _decode_json(value)
    if isinstance(value, dict):
        rows = [value]
    elif isinstance(value, list):
        rows = [row for row in value if isinstance(row, dict)]
    else:
        rows = []
    result = []
    for row in rows:
        metadata = _metadata_from_node(row)
        result.append(
            FeishuAttachment(
                source=source,
                filename=metadata.get("filename"),
                url=metadata.get("url"),
                declared_type=metadata.get("declared_type"),
                declared_size=metadata.get("declared_size"),
            )
        )
    return result


def _comment_is_deleted(comment: dict[str, Any]) -> bool:
    value = comment.get("is_delete")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip() == "1"
    return False


def _comment_attachments(comment: dict[str, Any]) -> list[FeishuAttachment]:
    """Read both legacy detail comments and v4 comment-list content."""
    result: list[FeishuAttachment] = []
    if not _comment_is_deleted(comment):
        result.extend(_file_rows(comment.get("files"), "comment"))
        content = _decode_json(comment.get("content"))
        if isinstance(content, dict):
            result.extend(_file_rows(content.get("files"), "comment"))

    # A deleted parent may still contain live replies in Feishu's response.
    for reply in _container_rows(comment.get("replies")):
        result.extend(_comment_attachments(reply))
    return result


def _deduplicate_attachment_urls(
    attachments: Iterable[FeishuAttachment],
) -> list[FeishuAttachment]:
    """Avoid downloading the same signed URL from overlapping response fields."""
    result: list[FeishuAttachment] = []
    seen_urls: set[str] = set()
    for attachment in attachments:
        if attachment.url:
            if attachment.url in seen_urls:
                continue
            seen_urls.add(attachment.url)
        result.append(attachment)
    return result


def parse_feishu_instance_attachments(
    detail: dict[str, Any],
    comment_rows: Iterable[dict[str, Any]] | None = None,
) -> list[FeishuAttachment]:
    """Extract attachment candidates from all three supported Feishu sources."""
    if isinstance(detail.get("data"), dict):
        detail = detail["data"]
    result: list[FeishuAttachment] = []

    form = _decode_json(detail.get("form"))
    for node in _walk(form):
        if not isinstance(node, dict):
            continue
        control_type = str(node.get("type") or node.get("componentType") or "").lower()
        if control_type in {"attachment", "attachmentv2", "image", "imagev2"}:
            result.extend(_form_control_attachments(node))

    for key in ("comments", "comment_list"):
        for comment in _container_rows(detail.get(key)):
            result.extend(_comment_attachments(comment))
    if comment_rows is not None:
        for comment in comment_rows:
            if isinstance(comment, dict):
                result.extend(_comment_attachments(comment))
    for key in ("operation_records", "operation_record_list"):
        for record in _container_rows(detail.get(key)):
            result.extend(_file_rows(record.get("files"), "operation_record"))
    for row in _container_rows(detail.get("timeline")):
        result.extend(_file_rows(row.get("files"), "timeline"))
    return _deduplicate_attachment_urls(result)


async def fetch_feishu_tenant_token() -> str:
    raise FeishuImportError("本地空数据版：外部附件、评论和下载连接已停用")


async def fetch_feishu_instance_detail_with_token(
    instance_code: str, token: str
) -> dict[str, Any]:
    raise FeishuImportError("本地空数据版：外部附件、评论和下载连接已停用")


def _has_more_comments(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"0", "false"}:
            return False
        if normalized in {"1", "true"}:
            return True
    raise FeishuImportError("读取飞书审批评论失败：分页状态格式异常")


async def fetch_feishu_instance_comments_with_token(
    instance_code: str,
    token: str,
    actor_open_id: str,
) -> list[dict[str, Any]]:
    raise FeishuImportError("本地空数据版：外部附件、评论和下载连接已停用")


async def fetch_feishu_instance_detail(instance_code: str) -> tuple[str, dict[str, Any]]:
    raise FeishuImportError("本地空数据版：外部附件、评论和下载连接已停用")


def _resolved_addresses(hostname: str, port: int) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    raise FeishuImportError("本地空数据版：外部附件、评论和下载连接已停用")


async def validate_feishu_download_url(url: str) -> None:
    raise FeishuImportError("本地空数据版：外部附件、评论和下载连接已停用")


async def download_feishu_attachment(url: str, max_bytes: int) -> bytes:
    raise FeishuImportError("本地空数据版：外部附件、评论和下载连接已停用")


def safe_attachment_filename(candidate: FeishuAttachment, index: int) -> str:
    """Return a basename, adding an extension only when Feishu supplied a type."""
    raw = Path(unquote(candidate.filename or "")).name.strip()
    if raw and Path(raw).suffix:
        return raw
    declared = (candidate.declared_type or "").lower()
    suffix = None
    if "pdf" in declared:
        suffix = ".pdf"
    elif "png" in declared:
        suffix = ".png"
    elif "jpeg" in declared or "jpg" in declared:
        suffix = ".jpg"
    if raw and suffix:
        return raw + suffix
    return raw or f"feishu-attachment-{index}"


def _content_extension(content: bytes) -> tuple[str, set[str]] | None:
    """Return a canonical extension and its accepted aliases for known magic."""
    if content.startswith(b"%PDF"):
        return ".pdf", {".pdf"}
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", {".png"}
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg", {".jpg", ".jpeg"}
    return None


def decode_feishu_base64_attachment(content: bytes) -> bytes:
    """Decode a purely Base64-wrapped supported file from Feishu only.

    Common ASCII whitespace is allowed, but the remaining payload must pass
    ``b64decode(validate=True)``.  Even then, the decoded bytes replace the
    download only when their magic is unambiguously PDF, PNG, or JPEG.  The
    caller still sends the result through ``validate_upload`` for full parsing.
    """
    if _content_extension(content) is not None:
        return content
    compact = b"".join(content.split())
    if not compact:
        return content
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return content
    return decoded if _content_extension(decoded) is not None else content


def filename_with_content_extension(filename: str, content: bytes) -> str:
    """Align an automatic Feishu filename with a conservative magic check.

    This is only a naming aid.  ``validate_upload`` still performs the complete
    PDF/image integrity and extension/content consistency validation afterwards.
    It is deliberately used only by the Feishu import path, so manual uploads
    continue to reject extension/content mismatches instead of being renamed.
    """
    detected = _content_extension(content)
    current_suffix = Path(filename).suffix.lower()
    if detected is None:
        return filename
    detected_suffix, equivalent_suffixes = detected
    if current_suffix in equivalent_suffixes:
        return filename
    if current_suffix:
        return str(Path(filename).with_suffix(detected_suffix))
    return filename + detected_suffix
