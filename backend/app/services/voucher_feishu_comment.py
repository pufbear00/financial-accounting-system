"""Upload voucher invoices and attach them to Feishu approval comments."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx




class FeishuCommentSyncError(RuntimeError):
    """An actionable Feishu delivery failure.

    ``uncertain`` means the create request may have reached Feishu before the
    response was lost. Callers must check the unique event marker before
    attempting another create request.
    """

    def __init__(
        self,
        message: str,
        *,
        uncertain: bool = False,
        attachment_invalid: bool = False,
        code: str | int | None = None,
    ):
        super().__init__(message)
        self.uncertain = uncertain
        self.attachment_invalid = attachment_invalid
        self.code = code


@dataclass(frozen=True)
class FeishuUploadedAttachment:
    url: str
    code: str | None = None


def _new_http_client() -> httpx.AsyncClient:
    raise FeishuCommentSyncError("本地空数据版：外部评论写入和附件上传已停用")


def _explicit_attachment_failure(message: str) -> bool:
    normalized = message.casefold()
    attachment_words = ("attachment", "file", "url", "附件", "文件", "链接")
    invalid_words = (
        "invalid",
        "expired",
        "expire",
        "not found",
        "does not exist",
        "无效",
        "过期",
        "失效",
        "不存在",
    )
    return any(word in normalized for word in attachment_words) and any(
        word in normalized for word in invalid_words
    )


def _payload(
    response: httpx.Response,
    action: str,
    *,
    comment_creation: bool = False,
) -> dict[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if response.is_error:
            raise FeishuCommentSyncError(
                f"{action}失败：飞书返回 HTTP {response.status_code}"
            ) from exc
        raise FeishuCommentSyncError(f"{action}失败：响应格式异常") from exc
    if not isinstance(payload, dict):
        raise FeishuCommentSyncError(f"{action}失败：响应格式异常")
    if response.is_error or payload.get("code") != 0:
        message = str(
            payload.get("msg")
            or payload.get("message")
            or f"飞书返回 HTTP {response.status_code}"
        )
        raise FeishuCommentSyncError(
            f"{action}失败：{message[:300]}",
            attachment_invalid=(
                comment_creation and _explicit_attachment_failure(message)
            ),
            code=payload.get("code"),
        )
    return payload


async def upload_feishu_approval_attachment(
    token: str,
    *,
    filename: str,
    mime_type: str,
    content: bytes,
) -> FeishuUploadedAttachment:
    raise FeishuCommentSyncError("本地空数据版：外部评论写入和附件上传已停用")


async def create_feishu_approval_comment(
    token: str,
    *,
    instance_code: str,
    actor_open_id: str,
    filename: str,
    size_bytes: int,
    attachment_url: str,
    event_key: str,
) -> str:
    raise FeishuCommentSyncError("本地空数据版：外部评论写入和附件上传已停用")


def _decode_nested_json(value: Any) -> Any:
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


def _extra_matches_event(value: Any, event_key: str) -> bool:
    value = _decode_nested_json(value)
    if isinstance(value, dict):
        event_id = value.get("event_id")
        if event_id is not None:
            return str(event_id) == event_key
        return any(
            _extra_matches_event(child, event_key)
            for child in value.values()
            if isinstance(child, (dict, list))
        )
    if isinstance(value, list):
        return any(_extra_matches_event(child, event_key) for child in value)
    return isinstance(value, str) and value == event_key


def _comment_with_event(node: Any, event_key: str) -> str | None:
    node = _decode_nested_json(node)
    if isinstance(node, dict):
        if "extra" in node and _extra_matches_event(node.get("extra"), event_key):
            comment_id = node.get("comment_id") or node.get("id")
            return str(comment_id) if comment_id else "found-without-id"
        for value in node.values():
            if isinstance(value, (dict, list)):
                found = _comment_with_event(value, event_key)
                if found:
                    return found
    elif isinstance(node, list):
        for value in node:
            found = _comment_with_event(value, event_key)
            if found:
                return found
    return None


async def find_feishu_comment_by_event(
    token: str,
    *,
    instance_code: str,
    actor_open_id: str,
    event_key: str,
) -> str | None:
    raise FeishuCommentSyncError("本地空数据版：外部评论写入和附件上传已停用")
