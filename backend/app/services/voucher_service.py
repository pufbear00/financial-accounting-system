from __future__ import annotations

import hashlib
import io
import json
import re
from copy import copy
from datetime import date, datetime
from difflib import SequenceMatcher
from html import escape
from math import ceil
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps
from pypdf import PageObject, PdfReader, PdfWriter, Transformation
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    LayoutError,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.config import settings
from app.models.voucher import VoucherDocument, VoucherItem


MIME_SIGNATURES = {
    "pdf": ("application/pdf", b"%PDF"),
    "jpg": ("image/jpeg", b"\xff\xd8\xff"),
    "png": ("image/png", b"\x89PNG\r\n\x1a\n"),
}
MAX_VOUCHER_IMAGE_PIXELS = 40_000_000
PAYEE_LABELS = ("收款方", "收款人", "供应商", "账户名称", "户名", "付款对象", "商户名称")
PAYEE_NEGATIVE_LABELS = ("付款人", "付款方", "付款账户", "申请人")
AMOUNT_LABELS = ("金额", "付款金额", "报销金额", "支付金额", "本次付款金额", "实付金额")
DATE_LABELS = ("日期", "付款日期", "支付日期", "费用日期", "发生日期")
DESC_LABELS = ("事由", "付款事由", "报销事由", "用途", "摘要", "说明", "备注")
_FONT_NAME = "VoucherCJK"
_FONT_REGISTERED = False


def _validated_image_size(image: Image.Image) -> tuple[int, int]:
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("图片尺寸无效")
    if width * height > MAX_VOUCHER_IMAGE_PIXELS:
        raise ValueError("图片像素过大，最大支持 4000 万像素")
    return width, height


def _json_value(value: Any) -> Any:
    """Decode nested JSON strings saved by different Feishu form controls."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped[:1] not in ("[", "{"):
        return value
    try:
        return json.loads(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _date_from_text(value: str | None) -> date | None:
    if not value:
        return None
    for pattern in (
        r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})日?",
        r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)",
    ):
        match = re.search(pattern, value)
        if match:
            try:
                return date(*(int(match.group(i)) for i in (1, 2, 3)))
            except ValueError:
                pass
    return None


def extract_receipt_metadata(
    filename: str, data: bytes, mime_type: str
) -> dict[str, Any]:
    """Best-effort local extraction for text PDFs; images are never guessed."""
    text = ""
    result: dict[str, Any] = {
        "receipt_direction": "unknown",
        "receipt_category": "pending",
    }
    if mime_type == "application/pdf":
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return result
    if not text.strip():
        return result

    search_text = text.replace("\xa0", " ").replace("\u3000", " ")
    compact_text = re.sub(r"\s+", "", search_text[:500])
    if "入账回单" in compact_text:
        result["receipt_direction"] = "income"
        result["receipt_category"] = "income"
    elif "出账回单" in compact_text:
        result["receipt_direction"] = "outgoing"
        result["receipt_category"] = "pending"
    else:
        result["receipt_direction"] = "unknown"
        result["receipt_category"] = "pending"
    number = r"([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)"
    amount_labels = (
        "交易金额", "付款金额", "转账金额", "汇款金额", "实付金额",
        "金额（小写）", "金额(小写)", "小写金额", "金额",
    )
    for label in amount_labels:
        match = re.search(
            re.escape(label) + r"\s*[:：]?\s*(?:人民币|CNY|RMB|¥|￥)?\s*" + number,
            search_text,
            re.IGNORECASE,
        )
        if match:
            result["amount"] = abs(float(match.group(1).replace(",", "")))
            break

    payee_labels = (
        "收款人户名", "收款人名称", "收款方名称", "收款账户名称", "对方户名",
        "对方名称", "收款单位", "收款人", "收款方",
    )
    payer_labels = ("付款人户名", "付款人名称", "付款方名称", "付款单位", "付款人", "付款方")
    next_labels = (
        "付款人", "付款方", "收款人", "收款方", "付款账号", "收款账号", "对方账号", "账号", "账户",
        "开户行", "金额", "币种", "交易日期", "付款日期", "用途", "摘要",
    )
    party_labels = payer_labels if result["receipt_direction"] == "income" else payee_labels
    for line in search_text.splitlines():
        compact = line.strip()
        for label in party_labels:
            match = re.search(re.escape(label) + r"\s*[:：]?\s*(.+)$", compact)
            if not match:
                continue
            value = match.group(1).strip(" ：:")
            value = re.split(
                r"\s+(?:" + "|".join(map(re.escape, next_labels)) + r")\s*[:：]?",
                value,
                maxsplit=1,
            )[0].strip()
            if value:
                result["payee"] = value[:255]
                break
        if result.get("payee"):
            break

    for label in ("交易日期", "付款日期", "转账日期", "汇款日期", "记账日期", "交易时间"):
        match = re.search(re.escape(label) + r"\s*[:：]?\s*([^\n]{6,32})", search_text)
        parsed = _date_from_text(match.group(1)) if match else None
        if parsed:
            result["transaction_date"] = parsed
            break

    for label in ("回单编号", "交易流水号", "银行流水号", "流水号"):
        match = re.search(
            re.escape(label) + r"\s*[:：]?\s*([A-Za-z0-9_-]{4,80})", search_text
        )
        if match:
            result["bank_reference"] = match.group(1)
            break
    return result


def extract_payee_names(raw_summaries: str | None) -> list[str]:
    """Extract all explicitly labelled payees in form order."""
    try:
        payload = json.loads(raw_summaries or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []

    def scalar_values(value: Any) -> list[str]:
        value = _json_value(value)
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            return [text[:255]] if text else []
        if isinstance(value, list):
            result: list[str] = []
            for child in value:
                result.extend(scalar_values(child))
            return result
        if isinstance(value, dict):
            for key in ("name", "text", "value", "label"):
                found = scalar_values(value.get(key))
                if found:
                    return found
        return []

    result: list[str] = []

    def walk(node: Any) -> None:
        node = _json_value(node)
        if isinstance(node, dict):
            label = " ".join(
                str(node.get(key) or "") for key in ("name", "label", "title")
            )
            if _is_payee_label(label):
                for found in scalar_values(node.get("value")):
                    if found not in result:
                        result.append(found)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    return result


def extract_payee_name(raw_summaries: str | None) -> str | None:
    """Return every explicit payee; never guesses from applicant fields."""
    names = extract_payee_names(raw_summaries)
    return "、".join(names)[:255] if names else None


def _scalar_text(value: Any) -> str | None:
    value = _json_value(value)
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return text or None
    if isinstance(value, list):
        parts = [part for child in value if (part := _scalar_text(child))]
        return "；".join(parts) if parts else None
    if isinstance(value, dict):
        for key in ("text", "label", "name", "value"):
            found = _scalar_text(value.get(key))
            if found:
                return found
    return None


def _amount_from_text(value: Any) -> float | None:
    text = _scalar_text(value)
    if not text:
        return None
    match = re.search(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?", text)
    if not match:
        return None
    try:
        return abs(float(match.group(0).replace(",", "")))
    except ValueError:
        return None


def _matches_label(label: str, candidates: tuple[str, ...]) -> bool:
    normalized = re.sub(r"\s+", "", label or "")
    return any(candidate in normalized for candidate in candidates)


def _is_payee_label(label: str) -> bool:
    """Match beneficiary fields without mistaking payer/account fields for payees."""
    normalized = re.sub(r"\s+", "", label or "")
    return (
        not any(candidate in normalized for candidate in PAYEE_NEGATIVE_LABELS)
        and any(candidate in normalized for candidate in PAYEE_LABELS)
    )


def _line_field_map(row: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}

    def add(label: Any, value: Any) -> None:
        label_text = str(label or "").strip()
        if not label_text or value in (None, ""):
            return
        fields.setdefault(label_text, value)

    def walk(node: Any) -> None:
        node = _json_value(node)
        if isinstance(node, dict):
            label = node.get("name") or node.get("label") or node.get("title")
            if label and node.get("value") not in (None, ""):
                add(label, node.get("value"))
            for key in ("value", "fieldList", "children", "fields", "items"):
                if key in node:
                    walk(node[key])
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(row)
    return fields


def _container_rows(value: Any) -> list[Any]:
    value = _json_value(value)
    if isinstance(value, dict):
        for key in ("value", "fieldList", "children", "fields", "items", "records", "list"):
            rows = _container_rows(value.get(key))
            if rows:
                return rows
        return []
    if not isinstance(value, list):
        return []
    if value and all(isinstance(row, list) for row in value):
        return value
    if value and all(isinstance(row, dict) for row in value):
        nested = []
        for row in value:
            control_type = str(row.get("type") or row.get("componentType") or "").lower()
            if "field" in control_type or row.get("fieldList"):
                nested.append(row)
        return nested or value
    return []


def _payment_line_from_fields(
    fields: dict[str, Any],
    *,
    index: int,
    fallback_description: str | None,
    fallback_payee: str | None,
) -> dict[str, Any] | None:
    amount = None
    payee = None
    expense_date = None
    description = None
    for label, value in fields.items():
        if amount is None and _matches_label(label, AMOUNT_LABELS):
            amount = _amount_from_text(value)
        if payee is None and _is_payee_label(label):
            payee = _scalar_text(value)
        if expense_date is None and _matches_label(label, DATE_LABELS):
            expense_date = _date_from_text(_scalar_text(value))
        if description is None and _matches_label(label, DESC_LABELS):
            description = _scalar_text(value)
    if amount is None:
        return None
    return {
        "source_line_key": f"line-{index}",
        "source_line_label": f"第 {index} 笔款项",
        "amount": amount,
        "amount_cny": amount,
        "payee_name": (payee or fallback_payee),
        "expense_date": expense_date,
        "description": description or fallback_description,
    }


def extract_payment_lines(
    raw_summaries: str | None,
    *,
    fallback_amount: float | None = None,
    fallback_amount_cny: float | None = None,
    fallback_description: str | None = None,
    fallback_payee: str | None = None,
) -> list[dict[str, Any]]:
    """Legacy form parser retained for diagnostics and backward compatibility.

    Voucher identity must never be derived from these rows: one approval is one
    voucher, while multiple payments are represented by multiple receipt links.
    """
    try:
        payload = json.loads(raw_summaries or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = []

    rows: list[dict[str, Any]] = []
    seen: set[tuple[float | None, str | None, date | None, str | None]] = set()

    def walk(node: Any) -> None:
        node = _json_value(node)
        if isinstance(node, dict):
            label = str(node.get("name") or node.get("label") or node.get("title") or "")
            control_type = str(node.get("type") or node.get("componentType") or "").lower()
            likely_detail = (
                "field" in control_type
                or any(word in label for word in ("明细", "款项", "分期", "付款计划"))
            )
            if likely_detail:
                for row in _container_rows(node):
                    line = _payment_line_from_fields(
                        _line_field_map(row),
                        index=len(rows) + 1,
                        fallback_description=fallback_description,
                        fallback_payee=fallback_payee,
                    )
                    if not line:
                        continue
                    key = (
                        line.get("amount"),
                        line.get("payee_name"),
                        line.get("expense_date"),
                        line.get("description"),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(line)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    if rows:
        return rows
    amount = fallback_amount if fallback_amount is not None else fallback_amount_cny
    return [
        {
            "source_line_key": "main",
            "source_line_label": None,
            "amount": amount,
            "amount_cny": fallback_amount_cny,
            "payee_name": fallback_payee,
            "expense_date": None,
            "description": fallback_description,
        }
    ]


def parse_source_attachment_metadata(raw_summaries: str | None) -> list[dict[str, Any]]:
    """Conservatively extract attachment/image metadata; never download URLs."""
    try:
        payload = json.loads(raw_summaries or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []

    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    control_words = ("attachment", "image", "upload", "file", "附件", "图片")

    def values(node: Any) -> list[dict[str, Any]]:
        node = _json_value(node)
        found: list[dict[str, Any]] = []
        if isinstance(node, dict):
            aliases = {
                "name": ("name", "file_name", "filename"),
                "token": ("file_token", "fileToken", "token"),
                "url": ("url", "download_url", "downloadUrl"),
                "mime_type": ("mime_type", "mimeType"),
                "size": ("size",),
            }
            useful: dict[str, Any] = {}
            for target, sources in aliases.items():
                for source in sources:
                    if node.get(source) not in (None, ""):
                        useful[target] = node[source]
                        break
            if useful and (
                useful.get("token")
                or useful.get("url")
                or re.search(r"\.(pdf|jpe?g|png)(?:\s*,|$)", str(useful.get("name", "")), re.I)
            ):
                found.append(useful)
            for child in node.values():
                found.extend(values(child))
        elif isinstance(node, list):
            for child in node:
                found.extend(values(child))
        return found

    def walk(node: Any) -> None:
        node = _json_value(node)
        if isinstance(node, dict):
            control_type = str(node.get("type") or node.get("componentType") or "")
            label = str(node.get("name") or node.get("label") or node.get("title") or "")
            if any(word.lower() in (control_type + " " + label).lower() for word in control_words):
                for metadata in values(node.get("value", node.get("fieldList", node))):
                    row = {
                        "control_type": control_type or "attachment",
                        "label": label or None,
                        **metadata,
                        "metadata_only": True,
                    }
                    key = (row.get("name"), row.get("token"), row.get("url"))
                    if key not in seen:
                        seen.add(key)
                        result.append(row)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    return result


def extract_approval_form_rows(raw_summaries: str | None) -> list[tuple[str, str]]:
    """Turn the frozen Feishu form snapshot into printable label/value rows."""
    try:
        payload = json.loads(raw_summaries or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []

    rows: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def printable(value: Any) -> str:
        value = _json_value(value)
        if value is None:
            return ""
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, (str, int, float)):
            return str(value).strip()
        if isinstance(value, list):
            return "；".join(filter(None, (printable(child) for child in value)))
        if isinstance(value, dict):
            for key in ("text", "label", "name", "value"):
                rendered = printable(value.get(key))
                if rendered:
                    return rendered
            parts = []
            for key, child in value.items():
                rendered = printable(child)
                if rendered:
                    parts.append(f"{key}: {rendered}")
            return "；".join(parts)
        return ""

    def add(label: Any, value: Any) -> None:
        label_text = str(label or "").strip()[:120]
        value_text = printable(value)[:1200]
        if not label_text or not value_text:
            return
        pair = (label_text, value_text)
        if pair not in seen:
            seen.add(pair)
            rows.append(pair)

    def walk(node: Any) -> None:
        node = _json_value(node)
        if isinstance(node, dict):
            label = node.get("name") or node.get("label") or node.get("title")
            control_type = str(node.get("type") or node.get("componentType") or "")
            value = node.get("value")
            if label and value not in (None, ""):
                attachments = parse_source_attachment_metadata(json.dumps([node], ensure_ascii=False))
                if attachments:
                    names = [str(item.get("name") or item.get("token") or "附件") for item in attachments]
                    add(label, "已上传附件（文件需另行归档）：" + "、".join(names))
                elif not any(word in control_type.lower() for word in ("field", "container")):
                    add(label, value)
            for key in ("value", "fieldList", "children", "fields"):
                if key in node:
                    walk(node[key])
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    return rows


def validate_upload(filename: str, data: bytes) -> tuple[str, str]:
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    if suffix == "jpeg":
        suffix = "jpg"
    if suffix not in MIME_SIGNATURES:
        raise ValueError("仅支持 PDF、JPG、PNG 文件")
    mime, signature = MIME_SIGNATURES[suffix]
    if not data.startswith(signature):
        raise ValueError("文件内容与扩展名不符")
    try:
        if suffix == "pdf":
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("PDF 已加密，无法装订")
            if not reader.pages:
                raise ValueError("PDF 没有可打印页面")
        else:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                _validated_image_size(image)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("文件已损坏或无法读取") from exc
    return suffix, mime


def split_pdf_pages(filename: str, data: bytes) -> list[tuple[str, bytes]]:
    """Return one printable PDF per page, preserving single-page uploads."""
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("PDF 已加密，无法拆分")
    total = len(reader.pages)
    if total <= 1:
        return [(filename, data)]

    stem = Path(filename or "receipt.pdf").stem or "receipt"
    result: list[tuple[str, bytes]] = []
    for index, page in enumerate(reader.pages, 1):
        writer = PdfWriter()
        writer.add_page(page)
        output = io.BytesIO()
        writer.write(output)
        result.append((f"{stem}-第{index:03d}页.pdf", output.getvalue()))
    return result


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_resolve(root: Path, relative_path: str) -> Path:
    resolved_root = root.resolve()
    target = (resolved_root / relative_path).resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise ValueError("非法文件路径")
    return target


def _normalized(text: str | None) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


def score_receipt(item: VoucherItem, document: VoucherDocument) -> dict[str, Any]:
    """Return an explainable 100-point candidate score without mutating state."""
    item_amount = item.amount if item.amount is not None else item.amount_cny
    amount_score = 0.0
    if item_amount is None or document.amount is None:
        amount_reason = "审批或回单缺少金额"
    else:
        difference = abs(abs(item_amount) - abs(document.amount))
        denominator = max(abs(item_amount), 1.0)
        amount_score = 70.0 * max(0.0, 1.0 - difference / denominator)
        if difference <= 0.01:
            amount_score = 70.0
        amount_reason = f"金额差 {difference:.2f}，得 {amount_score:.1f}/70"

    payee_score = 0.0
    payee = _normalized(document.payee)
    approval_payees = extract_payee_names(item.raw_summaries_snapshot)
    if item.payee_name_snapshot:
        approval_payees.extend(
            part.strip()
            for part in re.split(r"[、；;]", item.payee_name_snapshot)
            if part.strip()
        )
    normalized_payees = list(
        dict.fromkeys(filter(None, (_normalized(value) for value in approval_payees)))
    )
    description = _normalized(item.description)
    if not payee:
        payee_reason = "回单缺少收款方"
    elif any(payee in candidate or candidate in payee for candidate in normalized_payees):
        payee_score = 20.0
        payee_reason = "收款方与审批明细中的收款方一致，得 20.0/20"
    elif normalized_payees:
        similarity = max(
            SequenceMatcher(None, payee, candidate).ratio()
            for candidate in normalized_payees
        )
        payee_score = round(20.0 * similarity, 1)
        payee_reason = f"收款方与审批收款方相似度 {similarity:.0%}，得 {payee_score:.1f}/20"
    elif description:
        similarity = 1.0 if payee in description else SequenceMatcher(None, payee, description).ratio()
        payee_score = round(20.0 * similarity, 1)
        payee_reason = f"审批未填收款方，按事由相似度 {similarity:.0%}，得 {payee_score:.1f}/20"
    else:
        payee_reason = "审批缺少可比对的收款方/事由"

    date_score = 0.0
    item_date = item.submit_date or item.expense_date
    if not item_date or not document.transaction_date:
        date_reason = "审批或回单缺少日期"
    else:
        days = abs((item_date - document.transaction_date).days)
        if days == 0:
            date_score = 10.0
        elif days <= 3:
            date_score = 8.0
        elif days <= 7:
            date_score = 5.0
        elif days <= 31:
            date_score = 2.0
        date_reason = f"日期相差 {days} 天，得 {date_score:.1f}/10"

    score = round(amount_score + payee_score + date_score, 1)
    return {
        "score": score,
        "breakdown": {
            "amount": {"score": round(amount_score, 1), "max": 70, "reason": amount_reason},
            "payee": {"score": round(payee_score, 1), "max": 20, "reason": payee_reason},
            "date": {"score": round(date_score, 1), "max": 10, "reason": date_reason},
        },
    }


def _font_candidates() -> list[Path]:
    candidates = [
        settings.VOUCHER_FONT_PATH,
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ]
    return [Path(candidate) for candidate in candidates if candidate and Path(candidate).is_file()]


def _register_font() -> str:
    global _FONT_REGISTERED
    if not _FONT_REGISTERED:
        for font_path in _font_candidates():
            try:
                pdfmetrics.registerFont(
                    TTFont(_FONT_NAME, str(font_path), subfontIndex=0)
                )
                _FONT_REGISTERED = True
                break
            except Exception:
                continue
        if not _FONT_REGISTERED:
            raise RuntimeError(
                "未找到中文 TrueType 字体，请在本地 app/config.py 中设置 VOUCHER_FONT_PATH（不会读取生产环境变量）"
            )
    return _FONT_NAME


def _feishu_time(value: Any) -> str:
    """Format a Feishu millisecond timestamp in the dashboard timezone."""
    if value in (None, "", 0, "0"):
        return ""
    try:
        parsed = datetime.fromtimestamp(
            int(value) / 1000, tz=ZoneInfo("Asia/Shanghai")
        )
    except (TypeError, ValueError, OverflowError):
        return ""
    return parsed.strftime("%Y-%m-%d %H:%M")


def _feishu_status(value: Any, fallback: str = "") -> str:
    labels = {
        "APPROVED": "已通过",
        "PASSED": "已通过",
        "PASS": "已通过",
        "DONE": "已完成",
        "PENDING": "待审批",
        "REJECTED": "已拒绝",
        "CANCELED": "已撤销",
        "CANCELLED": "已撤销",
        "REVERTED": "已退回",
    }
    return labels.get(str(value or "").upper(), fallback)


def _decode_comment_value(value: Any) -> Any:
    """Decode the JSON strings used by different Feishu comment versions."""
    for _ in range(3):
        if not isinstance(value, str):
            break
        stripped = value.strip()
        if not stripped or stripped[:1] not in ("[", "{", '"'):
            break
        try:
            decoded = json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            break
        if decoded == value:
            break
        value = decoded
    return value


def _comment_container_rows(value: Any) -> list[dict[str, Any]]:
    """Unwrap the list containers returned by old and current comment APIs."""
    value = _decode_comment_value(value)
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if not isinstance(value, dict):
        return []
    if isinstance(value.get("data"), dict):
        nested = _comment_container_rows(value["data"])
        if nested:
            return nested
    for key in (
        "comments", "comment_list", "items", "list", "records", "replies",
    ):
        nested_value = _decode_comment_value(value.get(key))
        if isinstance(nested_value, (list, dict)):
            return _comment_container_rows(nested_value)
    return [value]


def _comment_replies(comment: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key in ("replies", "reply_list", "replyList", "children"):
        value = _decode_comment_value(comment.get(key))
        if isinstance(value, (list, dict)):
            result.extend(_comment_container_rows(value))
    return result


def _comment_is_deleted(comment: dict[str, Any]) -> bool:
    for key in ("is_delete", "is_deleted", "deleted"):
        value = comment.get(key)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, int) and value == 1:
            return True
        if isinstance(value, str) and value.strip().lower() in {
            "1", "true", "yes", "deleted",
        }:
            return True
    return str(comment.get("status") or "").strip().upper() == "DELETED"


def _comment_id(comment: dict[str, Any]) -> str:
    for key in ("comment_id", "reply_id", "id"):
        value = comment.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _clean_comment_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    lines = []
    for raw_line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b\ufeff]", "", raw_line)
        line = re.sub(r"[ \t\u3000]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _comment_content_text(value: Any) -> str:
    """Return visible comment prose without walking files, URLs or ``extra``."""
    value = _decode_comment_value(value)
    if isinstance(value, str):
        return _clean_comment_text(value)
    if isinstance(value, list):
        pieces = [_comment_content_text(row) for row in value]
        return "\n".join(piece for piece in pieces if piece)
    if not isinstance(value, dict):
        return ""

    node_type = str(value.get("type") or value.get("tag") or "").lower()
    if node_type in {
        "attachment", "file", "image", "imagev2", "media", "video", "audio",
    }:
        return ""

    # Prefer the explicit visible-text fields. Deliberately do not recursively
    # walk arbitrary dictionary values: Feishu stores signed attachment URLs
    # and our idempotency marker in sibling ``files``/``extra`` fields.
    for key in ("text", "plain_text", "content_text"):
        rendered = _comment_content_text(value.get(key))
        if rendered:
            return rendered
    rendered = _comment_content_text(value.get("text_run"))
    if rendered:
        return rendered
    rendered = _comment_content_text(value.get("content"))
    if rendered:
        return rendered
    if node_type in {"text", "plain_text", "text_run"}:
        rendered = _comment_content_text(value.get("value"))
        if rendered:
            return rendered

    for key, separator in (
        ("elements", ""),
        ("children", ""),
        ("paragraphs", "\n"),
        ("blocks", "\n"),
    ):
        rows = _decode_comment_value(value.get(key))
        if not isinstance(rows, list):
            continue
        pieces = [_comment_content_text(row) for row in rows]
        rendered = separator.join(piece for piece in pieces if piece)
        if rendered:
            return rendered
    return ""


def _comment_actor(
    comment: dict[str, Any], names: dict[str, str]
) -> tuple[str, str]:
    identities: list[dict[str, Any]] = [comment]
    for key in ("user", "creator", "sender", "operator"):
        value = _decode_comment_value(comment.get(key))
        if isinstance(value, dict):
            identities.append(value)

    identifier = ""
    for identity in identities:
        for key in (
            "open_id", "user_id", "commentator", "creator_id",
            "creator_open_id", "creator_user_id", "sender_id", "operator_id",
            "comment_user_id",
        ):
            value = identity.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                identifier = str(value).strip()
                break
        if identifier:
            break

    actor = names.get(identifier, "") if identifier else ""
    if not actor:
        for identity in identities:
            for key in (
                "display_name", "user_name", "creator_name", "sender_name", "name",
            ):
                value = identity.get(key)
                if isinstance(value, str) and value.strip():
                    actor = value.strip()
                    break
            if actor:
                break
    return actor or "评论人", identifier


def _comment_time(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?", value.strip())
    ):
        try:
            timestamp = float(value)
            if timestamp > 10**14:
                timestamp /= 1000
            elif timestamp < 10**11:
                timestamp *= 1000
            return _feishu_time(int(timestamp))
        except (TypeError, ValueError, OverflowError):
            return ""
    if isinstance(value, str):
        stripped = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(stripped)
        except ValueError:
            return ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    return ""


def format_feishu_approval_comments(
    comment_rows: Any,
    feishu_names: dict[str, str] | None = None,
    *,
    approval_detail: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Flatten current Feishu comments into safe, printable records.

    The result contains visible text, actor, timestamp and reply depth only.
    Attachments continue through the normal voucher document pipeline.
    """
    names = feishu_names or {}
    roots = _comment_container_rows(comment_rows)
    detail = approval_detail or {}
    if isinstance(detail.get("data"), dict):
        detail = detail["data"]
    if isinstance(detail, dict):
        for key in ("comments", "comment_list"):
            roots.extend(_comment_container_rows(detail.get(key)))

    deleted_ids: set[str] = set()

    def collect_deleted(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            row_id = _comment_id(row)
            if row_id and _comment_is_deleted(row):
                deleted_ids.add(row_id)
            collect_deleted(_comment_replies(row))

    collect_deleted(roots)
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    def visit(rows: list[dict[str, Any]], depth: int) -> None:
        for row in rows:
            row_id = _comment_id(row)
            deleted = _comment_is_deleted(row) or bool(
                row_id and row_id in deleted_ids
            )
            if not deleted:
                text = _comment_content_text(row.get("content"))
                if not text:
                    for key in ("text", "comment", "message"):
                        text = _comment_content_text(row.get(key))
                        if text:
                            break
                if text:
                    actor, actor_id = _comment_actor(row, names)
                    created_at = _comment_time(
                        row.get("create_time")
                        or row.get("created_time")
                        or row.get("created_at")
                        or row.get("timestamp")
                        or row.get("time")
                    )
                    identity = (
                        ("id", row_id)
                        if row_id
                        else ("content", actor_id or actor, created_at, text, depth)
                    )
                    if identity not in seen:
                        seen.add(identity)
                        result.append(
                            {
                                "comment_id": row_id,
                                "actor": actor,
                                "created_at": created_at,
                                "text": text,
                                "depth": depth,
                            }
                        )
            # Live replies remain meaningful even if their parent was deleted.
            visit(_comment_replies(row), depth + 1)

    visit(roots, 0)
    return result


def _approval_pdf(
    item: VoucherItem,
    documents: Iterable[tuple[VoucherDocument, Path]] = (),
    printed_by: str | None = None,
    approval_detail: dict[str, Any] | None = None,
    feishu_names: dict[str, str] | None = None,
    approval_comments: Any = None,
) -> bytes:
    """Render the frozen approval snapshot in Feishu's printable table style."""
    font_name = _register_font()
    output = io.BytesIO()
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "voucher-title", parent=styles["Title"], fontName=font_name,
        fontSize=18, leading=23, alignment=TA_CENTER, spaceAfter=0,
    )
    body = ParagraphStyle(
        "voucher-body", parent=styles["BodyText"], fontName=font_name,
        fontSize=9.5, leading=13, textColor=colors.HexColor("#1f2937"),
        wordWrap="CJK",
    )
    small = ParagraphStyle(
        "voucher-small", parent=body, fontSize=8.5, leading=12,
        textColor=colors.HexColor("#4b5563"),
    )
    section = ParagraphStyle(
        "voucher-section", parent=body, fontSize=10, leading=14,
        alignment=TA_CENTER,
    )
    doc = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=56,
        leftMargin=56,
        topMargin=28,
        bottomMargin=46,
    )

    def value_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (date, datetime)):
            return value.strftime("%Y-%m-%d")
        return str(value)

    def paragraph(value: Any, style: ParagraphStyle = body) -> Paragraph:
        return Paragraph(
            escape(value_text(value)).replace("\n", "<br/>"), style
        )

    def first_form_value(
        form_rows: list[tuple[str, str]], keywords: tuple[str, ...]
    ) -> str:
        for label, value in form_rows:
            normalized = re.sub(r"\s+", "", label)
            if any(keyword in normalized for keyword in keywords):
                return value
        return ""

    def printable_date(value: Any) -> str:
        rendered = value_text(value)
        match = re.search(r"20\d{2}-\d{1,2}-\d{1,2}", rendered)
        return match.group(0) if match else rendered

    detail = approval_detail or {}
    if isinstance(detail.get("data"), dict):
        detail = detail["data"]
    names = feishu_names or {}
    local_status = {1: "待审批", 2: "已通过", 3: "已拒绝", 4: "已撤销"}.get(
        item.approval_status, "未知"
    )
    status_label = _feishu_status(detail.get("status"), local_status)
    live_form = detail.get("form")
    if isinstance(live_form, (list, dict)):
        live_form = json.dumps(live_form, ensure_ascii=False)
    form_rows = extract_approval_form_rows(
        live_form if isinstance(live_form, str) else item.raw_summaries_snapshot
    )
    department = first_form_value(form_rows, ("所属部门", "申请部门", "部门"))
    detail_type = first_form_value(
        form_rows, ("报销类型", "费用类型", "付款类型", "费用分类", "分类")
    ) or (item.definition_name or "")
    detail_date = printable_date(
        first_form_value(
            form_rows, ("费用日期", "付款日期", "发生日期", "日期")
        )
        or item.expense_date
        or item.submit_date
    )
    currency = item.currency or "CNY"
    amount_text = f"{item.amount or 0:,.2f} {currency}"
    if currency == "CNY":
        amount_text += "-人民币元"
    elif item.amount_cny is not None:
        amount_text += f"（折合人民币 {item.amount_cny:,.2f} 元）"
    document_count = sum(1 for _document, _path in documents)

    title_text = detail.get("approval_name") or item.definition_name or "费用审批"
    application_number = detail.get("serial_number") or item.instance_code or ""
    applicant_name = item.applicant_name or ""
    applicant_open_id = str(detail.get("open_id") or "")
    applicant_user_id = str(detail.get("user_id") or "")
    applicant_name = (
        names.get(applicant_open_id)
        or names.get(applicant_user_id)
        or applicant_name
    )
    application_time = _feishu_time(detail.get("start_time")) or value_text(
        item.submit_date
    )
    printed_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    meta = Table(
        [
            [
                paragraph(f"申请编号：{application_number}", small),
                paragraph(f"打印人员：{printed_by or '系统管理员'}", small),
            ],
            ["", paragraph(f"打印时间：{printed_at}", small)],
        ],
        colWidths=[301, 182],
    )
    meta.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )

    def text_chunks(value: Any, limit: int = 480) -> list[str]:
        rendered = value_text(value)
        return [rendered[index:index + limit] for index in range(0, len(rendered), limit)] or [""]

    data: list[list[Any]] = [
        [paragraph("申请人"), paragraph(applicant_name), paragraph("申请时间"), paragraph(application_time)],
        [paragraph("所属部门"), paragraph(department), paragraph("审批状态"), paragraph(status_label)],
    ]
    section_rows: set[int] = set()
    full_value_rows: set[int] = set()

    def append_section(label: str) -> None:
        row = len(data)
        data.append([paragraph(label, section), "", "", ""])
        section_rows.add(row)

    def append_full_value(label: str, value: Any) -> None:
        for index, chunk in enumerate(text_chunks(value)):
            row = len(data)
            row_label = label if index == 0 else f"{label}（续）"
            data.append([paragraph(row_label), paragraph(chunk), "", ""])
            full_value_rows.add(row)

    append_section("审批详情")
    append_full_value("审批类型", detail_type)
    append_full_value("审批事由", item.description)
    append_section("费用明细")
    detail_pair_row = len(data)
    data.append(
        [
            paragraph("内容"),
            paragraph(detail_type),
            paragraph("日期（年-月-日）"),
            paragraph(detail_date),
        ]
    )
    append_full_value("金额", amount_text)

    skip_keywords = (
        "申请人", "所属部门", "申请部门", "审批状态", "报销类型",
        "费用类型", "付款类型", "费用分类", "报销事由", "付款事由",
        "申请事由", "费用明细", "付款明细", "发票", "附件", "支付证明",
        "内容", "日期", "金额",
    )
    for label, value in form_rows:
        normalized = re.sub(r"\s+", "", label)
        if any(keyword in normalized for keyword in skip_keywords):
            continue
        append_full_value(label, value)

    append_full_value("发票及支付证明", f"{document_count} 个")
    append_section("审批记录")
    launch_row = len(data)
    data.append(
        [
            paragraph("发起"),
            paragraph(
                f"{applicant_name} / 发起审批"
                + (f" / {application_time}" if application_time else "")
            ),
            "",
            "",
        ]
    )
    full_value_rows.add(launch_row)
    tasks = detail.get("task_list")
    if isinstance(tasks, list) and tasks:
        for task in tasks:
            if not isinstance(task, dict):
                continue
            node_name = str(task.get("node_name") or "审批")
            open_id = str(task.get("open_id") or "")
            user_id = str(task.get("user_id") or "")
            actor = names.get(open_id) or names.get(user_id)
            if not actor and (open_id == applicant_open_id or user_id == applicant_user_id):
                actor = applicant_name
            actor = actor or "审批人"
            task_status = _feishu_status(task.get("status"), "已处理")
            task_time = _feishu_time(task.get("end_time") or task.get("start_time"))
            detail_parts = [actor, task_status]
            if task_time:
                detail_parts.append(task_time)
            result_row = len(data)
            data.append(
                [
                    paragraph(node_name),
                    paragraph(" / ".join(detail_parts)),
                    "",
                    "",
                ]
            )
            full_value_rows.add(result_row)
    else:
        result_row = len(data)
        data.append([paragraph("审批状态"), paragraph(status_label), "", ""])
        full_value_rows.add(result_row)

    # Comments are optional live metadata. Any unexpected response variant is
    # ignored so a temporary Feishu issue can never block voucher export.
    try:
        printable_comments = format_feishu_approval_comments(
            approval_comments,
            names,
            approval_detail=detail,
        )
    except Exception:
        printable_comments = []
    if printable_comments:
        append_section("审批评论")
        for comment in printable_comments:
            depth = max(0, int(comment.get("depth") or 0))
            label = "评论" if depth == 0 else "回复"
            actor = str(comment.get("actor") or "评论人")
            created_at = str(comment.get("created_at") or "")
            heading = " / ".join(
                part for part in (actor, created_at) if part
            )
            indent = "　" * min(depth, 4)
            text = str(comment.get("text") or "")
            append_full_value(label, f"{indent}{heading}\n{indent}{text}")

    table = Table(data, colWidths=[93, 149, 92, 149], repeatRows=2, splitByRow=1)
    commands: list[tuple[Any, ...]] = [
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#2b2f36")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BACKGROUND", (0, 0), (0, 1), colors.HexColor("#f2f3f5")),
        ("BACKGROUND", (2, 0), (2, 1), colors.HexColor("#f2f3f5")),
    ]
    for row in sorted(section_rows):
        commands.extend(
            [
                ("SPAN", (0, row), (3, row)),
                ("ALIGN", (0, row), (3, row), "CENTER"),
                ("TOPPADDING", (0, row), (3, row), 4),
                ("BOTTOMPADDING", (0, row), (3, row), 4),
            ]
        )
    for row in sorted(full_value_rows):
        commands.extend(
            [
                ("SPAN", (1, row), (3, row)),
                ("BACKGROUND", (0, row), (0, row), colors.HexColor("#f2f3f5")),
            ]
        )
    commands.extend(
        [
            ("BACKGROUND", (0, detail_pair_row), (0, detail_pair_row), colors.HexColor("#f2f3f5")),
            ("BACKGROUND", (2, detail_pair_row), (2, detail_pair_row), colors.HexColor("#f2f3f5")),
        ]
    )
    table.setStyle(
        TableStyle(commands)
    )
    story: list[Any] = [
        Paragraph(title_text, title),
        Spacer(1, 4),
        meta,
        Spacer(1, 5),
        table,
    ]
    try:
        doc.build(story)
    except LayoutError as exc:
        raise ValueError("审批内容过长，无法生成打印版") from exc
    return output.getvalue()


def _image_pdf(image_path: Path) -> bytes:
    output = io.BytesIO()
    c = canvas.Canvas(output, pagesize=A4)
    with Image.open(image_path) as image:
        width, height = _validated_image_size(image)
        image_reader = ImageReader(image.convert("RGB"))
        page_w, page_h = A4
        margin = 48
        scale = min((page_w - margin * 2) / width, (page_h - margin * 2) / height)
        draw_w, draw_h = width * scale, height * scale
        c.drawImage(
            image_reader,
            (page_w - draw_w) / 2,
            (page_h - draw_h) / 2,
            draw_w,
            draw_h,
            preserveAspectRatio=True,
        )
    c.showPage()
    c.save()
    return output.getvalue()


def _invoice_image_pdf(image_path: Path, *, effective_scale: float = 1.0) -> bytes:
    """Render one invoice image as a full portrait A4 source page."""
    output = io.BytesIO()
    page_w, page_h = A4
    c = canvas.Canvas(output, pagesize=A4)
    margin = 48.0
    with Image.open(image_path) as source:
        _validated_image_size(source)
        oriented = ImageOps.exif_transpose(source)
        width, height = oriented.size
        scale = min(
            (page_w - margin * 2) / width,
            (page_h - margin * 2) / height,
        )
        draw_width = width * scale
        draw_height = height * scale
        # Limit embedded pixels to 300 DPI at the final print size. Paired
        # pages pass an effective scale below 1 because the A4 source page is
        # subsequently reduced into a half-page slot.
        max_pixels = (
            max(1, ceil(draw_width * effective_scale * 300 / 72)),
            max(1, ceil(draw_height * effective_scale * 300 / 72)),
        )
        oriented.thumbnail(max_pixels, Image.Resampling.LANCZOS)
        if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info:
            rgba = oriented.convert("RGBA")
            printable = Image.new("RGB", rgba.size, "white")
            printable.paste(rgba, mask=rgba.getchannel("A"))
        else:
            printable = oriented.convert("RGB")
        c.drawImage(
            ImageReader(printable),
            (page_w - draw_width) / 2,
            (page_h - draw_height) / 2,
            draw_width,
            draw_height,
            preserveAspectRatio=True,
            mask="auto",
        )
    c.showPage()
    c.save()
    return output.getvalue()


def _invoice_image_sheet(image_paths: list[Path]) -> bytes:
    """Place up to four payment screenshots in fixed left-to-right A4 slots."""
    if not 1 <= len(image_paths) <= 4:
        raise ValueError("每页图片发票数量必须为 1 至 4 张")
    output = io.BytesIO()
    page_w, page_h = landscape(A4)
    c = canvas.Canvas(output, pagesize=(page_w, page_h))
    margin_x = 24.0
    margin_y = 28.0
    column_gap = 10.0
    # A lone image gets the full printable sheet. The monthly exporter later
    # places this landscape sheet into one A4 half, so wide tables remain
    # readable instead of being shrunk into one quarter of that half-page.
    # Multi-image groups retain the stable four-column cutting grid.
    column_count = 1 if len(image_paths) == 1 else 4
    column_width = (
        page_w
        - margin_x * 2
        - column_gap * (column_count - 1)
    ) / column_count
    available_height = page_h - margin_y * 2
    for column, image_path in enumerate(image_paths):
        with Image.open(image_path) as source:
            _validated_image_size(source)
            oriented = ImageOps.exif_transpose(source)
            width, height = oriented.size
            scale = min(column_width / width, available_height / height)
            draw_width = width * scale
            draw_height = height * scale
            max_pixels = (
                max(1, ceil(draw_width * 300 / 72)),
                max(1, ceil(draw_height * 300 / 72)),
            )
            oriented.thumbnail(max_pixels, Image.Resampling.LANCZOS)
            if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                printable = Image.new("RGB", rgba.size, "white")
                printable.paste(rgba, mask=rgba.getchannel("A"))
            else:
                printable = oriented.convert("RGB")
            column_left = margin_x + column * (column_width + column_gap)
            draw_x = column_left + (column_width - draw_width) / 2
            draw_y = page_h - margin_y - draw_height
            c.drawImage(
                ImageReader(printable),
                draw_x,
                draw_y,
                draw_width,
                draw_height,
                preserveAspectRatio=True,
                mask="auto",
            )

    # Only occupied neighbouring slots need a cutting guide.
    for boundary in range(1, len(image_paths)):
        x = (
            margin_x
            + boundary * column_width
            + (boundary - 0.5) * column_gap
        )
        c.setStrokeColor(colors.HexColor("#9ca3af"))
        c.setLineWidth(0.7)
        c.setDash(5, 4)
        c.line(x, 18, x, page_h - 18)
        c.setDash()
        c.setFillColor(colors.white)
        c.rect(x - 20, 4, 40, 12, fill=1, stroke=0)
        c.setFillColor(colors.HexColor("#6b7280"))
        c.setFont(_register_font(), 8)
        c.drawCentredString(x, 6, "裁剪线")
    c.showPage()
    c.save()
    return output.getvalue()


def _receipt_cut_overlay() -> bytes:
    """Draw a clear half-page cutting guide without covering receipt content."""
    output = io.BytesIO()
    page_w, page_h = A4
    middle = page_h / 2
    c = canvas.Canvas(output, pagesize=A4)
    c.setStrokeColor(colors.HexColor("#9ca3af"))
    c.setLineWidth(0.7)
    c.setDash(5, 4)
    c.line(18, middle, page_w - 18, middle)
    c.setDash()
    c.setFillColor(colors.white)
    c.rect(page_w / 2 - 25, middle - 6, 50, 12, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#6b7280"))
    c.setFont(_register_font(), 8)
    c.drawCentredString(page_w / 2, middle - 3, "裁剪线")
    c.showPage()
    c.save()
    return output.getvalue()


def _flatten_stamp_annotations(source: PageObject) -> PageObject:
    """Paint visible stamp appearances into page content before transforming.

    Tax invoice seals are commonly stored as PDF stamp annotations. Page
    content scales during two-up layout, while annotation rectangles otherwise
    stay at their original coordinates and make a genuine seal appear moved.
    Flattening only the existing appearance preserves its exact pixels and
    position; the archived source PDF is never modified.
    """
    page = copy(source)
    annotation_refs = list(page.get("/Annots", []))
    if not annotation_refs:
        return page

    overlay = PageObject.create_blank_page(
        width=float(page.mediabox.width),
        height=float(page.mediabox.height),
    )
    xobjects = DictionaryObject()
    commands: list[str] = []
    remaining = ArrayObject()
    for index, reference in enumerate(annotation_refs):
        try:
            annotation = reference.get_object()
            normal = (annotation.get("/AP") or {}).get("/N")
            appearance = normal.get_object() if normal is not None else None
            rect = [float(value) for value in annotation.get("/Rect", [])]
            bbox = [float(value) for value in appearance.get("/BBox", [])]
            if (
                annotation.get("/Subtype") != "/Stamp"
                or appearance is None
                or len(rect) != 4
                or len(bbox) != 4
                or appearance.get("/Matrix") not in (None, [1, 0, 0, 1, 0, 0])
            ):
                remaining.append(reference)
                continue
            bbox_width = bbox[2] - bbox[0]
            bbox_height = bbox[3] - bbox[1]
            if bbox_width <= 0 or bbox_height <= 0:
                remaining.append(reference)
                continue
            scale_x = (rect[2] - rect[0]) / bbox_width
            scale_y = (rect[3] - rect[1]) / bbox_height
            translate_x = rect[0] - bbox[0] * scale_x
            translate_y = rect[1] - bbox[1] * scale_y
            name = NameObject(f"/VoucherStamp{index}")
            xobjects[name] = appearance
            commands.append(
                f"q {scale_x:.8f} 0 0 {scale_y:.8f} "
                f"{translate_x:.8f} {translate_y:.8f} cm {name} Do Q\n"
            )
        except Exception:
            remaining.append(reference)

    if commands:
        overlay[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/XObject"): xobjects}
        )
        stream = DecodedStreamObject()
        stream.set_data("".join(commands).encode("ascii"))
        overlay[NameObject("/Contents")] = stream
        page.merge_page(overlay)
    if remaining:
        page[NameObject("/Annots")] = remaining
    else:
        page.pop(NameObject("/Annots"), None)
    return page


def _merge_page_into_slot(
    target: PageObject,
    source: PageObject,
    *,
    slot_bottom: float,
) -> None:
    """Fit one source page into an A4 half-page slot with printable margins."""
    page = _flatten_stamp_annotations(source)
    if page.rotation:
        page.transfer_rotation_to_content()
    crop_left, crop_bottom, crop_right, crop_top = _receipt_content_box(page)
    page_w, page_h = A4
    half_h = page_h / 2
    margin_x = 18.0
    margin_y = 18.0
    available_w = page_w - margin_x * 2
    available_h = half_h - margin_y * 2
    source_w = crop_right - crop_left
    source_h = crop_top - crop_bottom
    if source_w <= 0 or source_h <= 0:
        raise ValueError("银行回单页面尺寸无效")
    scale = min(available_w / source_w, available_h / source_h)
    draw_w = source_w * scale
    draw_h = source_h * scale
    target_x = (page_w - draw_w) / 2
    target_y = slot_bottom + margin_y + (available_h - draw_h) / 2
    transform = (
        Transformation()
        .translate(-crop_left, -crop_bottom)
        .scale(scale)
        .translate(target_x, target_y)
    )
    target.merge_transformed_page(page, transform, expand=False)


def _receipt_content_box(page: PageObject) -> tuple[float, float, float, float]:
    """Estimate printable receipt bounds from PDF text, falling back to full page."""
    page_left = float(page.mediabox.left)
    page_bottom = float(page.mediabox.bottom)
    page_right = float(page.mediabox.right)
    page_top = float(page.mediabox.top)
    positions: list[tuple[float, float, float]] = []

    def collect_text(
        text: str,
        cm: list[float],
        tm: list[float],
        _font: dict[str, Any] | None,
        font_size: float,
    ) -> None:
        if not text.strip():
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        if page_left <= x <= page_right and page_bottom <= y <= page_top:
            positions.append((float(x), float(y), float(font_size or 0)))

    try:
        page.extract_text(visitor_text=collect_text)
    except Exception:
        positions = []
    if len(positions) < 3:
        # ReportLab and many mobile scanners place one image onto an A4 shell.
        # Recover that image's drawing matrix so the actual receipt, rather
        # than the surrounding blank A4 canvas, is fitted into the print slot.
        image_boxes: list[tuple[float, float, float, float]] = []
        try:
            stream = page.get_contents().get_data()
            number = rb"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
            draw_pattern = re.compile(
                rb"(" + number + rb")\s+(" + number + rb")\s+("
                + number + rb")\s+(" + number + rb")\s+(" + number
                + rb")\s+(" + number + rb")\s+cm\s*/[^\s]+\s+Do"
            )
            for match in draw_pattern.finditer(stream):
                a, b, c, d, e, f = (float(value) for value in match.groups())
                corners = (
                    (e, f),
                    (a + e, b + f),
                    (c + e, d + f),
                    (a + c + e, b + d + f),
                )
                xs = [point[0] for point in corners]
                ys = [point[1] for point in corners]
                image_boxes.append((min(xs), min(ys), max(xs), max(ys)))
        except Exception:
            image_boxes = []
        if image_boxes:
            return (
                max(page_left, min(box[0] for box in image_boxes) - 12),
                max(page_bottom, min(box[1] for box in image_boxes) - 12),
                min(page_right, max(box[2] for box in image_boxes) + 12),
                min(page_top, max(box[3] for box in image_boxes) + 12),
            )
        return page_left, page_bottom, page_right, page_top

    min_x = min(row[0] for row in positions)
    max_x = max(row[0] for row in positions)
    min_y = min(row[1] for row in positions)
    max_y = max(row[1] + max(row[2], 8.0) for row in positions)
    content_width = max_x - min_x
    content_height = max_y - min_y
    page_width = page_right - page_left
    page_height = page_top - page_bottom
    if content_width < page_width * 0.2 or content_height < page_height * 0.08:
        return page_left, page_bottom, page_right, page_top

    left = max(page_left, min_x - 28)
    right = min(page_right, max_x + 92)
    bottom = max(page_bottom, min_y - 42)
    top = min(page_top, max_y + 28)
    if right - left < page_width * 0.4 or top - bottom < page_height * 0.15:
        return page_left, page_bottom, page_right, page_top
    return left, bottom, right, top


def _two_up_receipt_page(first: PageObject, second: PageObject) -> PageObject:
    """Place two printable pages on one A4 sheet, top and bottom."""
    sheet = PageObject.create_blank_page(width=A4[0], height=A4[1])
    _merge_page_into_slot(sheet, first, slot_bottom=A4[1] / 2)
    _merge_page_into_slot(sheet, second, slot_bottom=0)
    overlay = PdfReader(io.BytesIO(_receipt_cut_overlay()), strict=False).pages[0]
    sheet.merge_page(overlay)
    return sheet


def _single_receipt_page(source: PageObject) -> PageObject:
    """Place an unpaired receipt on a full A4 sheet without half-page blank space."""
    page = _flatten_stamp_annotations(source)
    if page.rotation:
        page.transfer_rotation_to_content()
    crop_left, crop_bottom, crop_right, crop_top = _receipt_content_box(page)
    source_w = crop_right - crop_left
    source_h = crop_top - crop_bottom
    if source_w <= 0 or source_h <= 0:
        raise ValueError("银行回单页面尺寸无效")

    # A lone landscape receipt is much clearer on landscape A4. Portrait
    # receipts continue to use portrait A4. Even-numbered small receipts still
    # use the existing two-up portrait layout.
    sheet_w, sheet_h = landscape(A4) if source_w > source_h else A4
    sheet = PageObject.create_blank_page(width=sheet_w, height=sheet_h)
    margin = 24.0
    available_w = sheet_w - margin * 2
    available_h = sheet_h - margin * 2
    scale = min(available_w / source_w, available_h / source_h)
    draw_w = source_w * scale
    draw_h = source_h * scale
    target_x = (sheet_w - draw_w) / 2
    target_y = (sheet_h - draw_h) / 2
    transform = (
        Transformation()
        .translate(-crop_left, -crop_bottom)
        .scale(scale)
        .translate(target_x, target_y)
    )
    sheet.merge_transformed_page(page, transform, expand=False)
    return sheet


def _is_a4_page(page: PageObject) -> bool:
    """Return whether a page is already a full A4 sheet in either orientation."""
    box = page.mediabox
    width = abs(float(box.right) - float(box.left))
    height = abs(float(box.top) - float(box.bottom))
    actual = sorted((width, height))
    expected = sorted((float(A4[0]), float(A4[1])))
    return all(abs(value - target) <= target * 0.02 for value, target in zip(actual, expected))


def _is_full_a4_receipt_page(page: PageObject) -> bool:
    """Distinguish a real full-page receipt from a small receipt saved on A4.

    Some bank PDFs use an A4 media box but draw a landscape receipt only in the
    upper part of that box. Treating those files as complete A4 pages leaves a
    large blank lower half and prevents them from being paired for printing.
    """
    if not _is_a4_page(page):
        return False

    page_left = float(page.mediabox.left)
    page_bottom = float(page.mediabox.bottom)
    page_width = float(page.mediabox.right) - page_left
    page_height = float(page.mediabox.top) - page_bottom
    content_left, content_bottom, content_right, content_top = _receipt_content_box(page)
    content_width_ratio = (content_right - content_left) / page_width
    content_height_ratio = (content_top - content_bottom) / page_height

    # A wide but shallow content area is a small landscape receipt placed on an
    # A4 canvas. If the content bounds cannot be established (for example a
    # sparse text PDF), preserve the A4 page instead of guessing and cropping
    # away material that may not be extractable as text.
    return not (content_width_ratio >= 0.62 and content_height_ratio <= 0.55)


def _add_receipt_pages(writer: PdfWriter, pages: Iterable[PageObject]) -> None:
    """Keep genuine full A4 receipts intact and pair smaller receipt pages."""
    pending_small: PageObject | None = None
    for page in pages:
        if _is_full_a4_receipt_page(page):
            writer.add_page(page)
            continue
        if pending_small is None:
            pending_small = page
        else:
            writer.add_page(_two_up_receipt_page(pending_small, page))
            pending_small = None
    if pending_small is not None:
        writer.add_page(_single_receipt_page(pending_small))


def _merge_invoice_page_into_slot(
    target: PageObject,
    source: PageObject,
    *,
    slot_bottom: float,
) -> None:
    """Fit a complete PDF invoice crop box into one A4 half-page slot."""
    page = _flatten_stamp_annotations(source)
    if page.rotation:
        page.transfer_rotation_to_content()
    source_box = page.cropbox
    source_left = float(source_box.left)
    source_bottom = float(source_box.bottom)
    source_right = float(source_box.right)
    source_top = float(source_box.top)
    source_width = source_right - source_left
    source_height = source_top - source_bottom
    if source_width <= 0 or source_height <= 0:
        raise ValueError("发票页面尺寸无效")

    page_w, page_h = A4
    half_h = page_h / 2
    margin_x = 18.0
    margin_y = 18.0
    available_w = page_w - margin_x * 2
    available_h = half_h - margin_y * 2
    scale = min(available_w / source_width, available_h / source_height)
    draw_width = source_width * scale
    draw_height = source_height * scale
    target_x = (page_w - draw_width) / 2
    target_y = slot_bottom + margin_y + (available_h - draw_height) / 2
    transform = (
        Transformation()
        .translate(-source_left, -source_bottom)
        .scale(scale)
        .translate(target_x, target_y)
    )
    target.merge_transformed_page(page, transform, expand=False)


def _two_up_invoice_page(first: PageObject, second: PageObject) -> PageObject:
    """Place two complete formal-invoice pages on one portrait A4 sheet."""
    sheet = PageObject.create_blank_page(width=A4[0], height=A4[1])
    _merge_invoice_page_into_slot(sheet, first, slot_bottom=A4[1] / 2)
    _merge_invoice_page_into_slot(sheet, second, slot_bottom=0)
    overlay = PdfReader(io.BytesIO(_receipt_cut_overlay()), strict=False).pages[0]
    sheet.merge_page(overlay)
    return sheet


def _single_invoice_a4_page(source: PageObject) -> PageObject:
    """Fit one complete standalone form onto one portrait A4 sheet."""
    sheet = PageObject.create_blank_page(width=A4[0], height=A4[1])
    page = _flatten_stamp_annotations(source)
    if page.rotation:
        page.transfer_rotation_to_content()
    box = page.cropbox
    source_left = float(box.left)
    source_bottom = float(box.bottom)
    source_width = float(box.right) - source_left
    source_height = float(box.top) - source_bottom
    if source_width <= 0 or source_height <= 0:
        raise ValueError("单据页面尺寸无效")
    margin = 18.0
    scale = min(
        (A4[0] - margin * 2) / source_width,
        (A4[1] - margin * 2) / source_height,
    )
    draw_width = source_width * scale
    draw_height = source_height * scale
    transform = (
        Transformation()
        .translate(-source_left, -source_bottom)
        .scale(scale)
        .translate((A4[0] - draw_width) / 2, (A4[1] - draw_height) / 2)
    )
    sheet.merge_transformed_page(page, transform, expand=False)
    return sheet


def _is_standalone_invoice(document: VoucherDocument, page: PageObject) -> bool:
    """Keep notices such as payment reminders as one document per A4 page."""
    name = document.original_filename or ""
    if any(keyword in name for keyword in ("催缴", "缴款通知", "付款通知")):
        return True
    try:
        text_content = page.extract_text() or ""
    except Exception:
        return False
    return any(keyword in text_content for keyword in ("账单催缴函", "缴款通知书"))


def _two_sequential_pages(first: PageObject, second: PageObject) -> PageObject:
    """Place two adjacent landscape pages top-to-bottom without reordering."""
    sheet = PageObject.create_blank_page(width=A4[0], height=A4[1])
    _merge_invoice_page_into_slot(sheet, first, slot_bottom=A4[1] / 2)
    _merge_invoice_page_into_slot(sheet, second, slot_bottom=0)
    overlay = PdfReader(io.BytesIO(_receipt_cut_overlay()), strict=False).pages[0]
    sheet.merge_page(overlay)
    return sheet


def _single_sequential_page(first: PageObject) -> PageObject:
    """Keep an odd final landscape page on top and leave the lower half blank."""
    sheet = PageObject.create_blank_page(width=A4[0], height=A4[1])
    _merge_invoice_page_into_slot(sheet, first, slot_bottom=A4[1] / 2)
    overlay = PdfReader(io.BytesIO(_receipt_cut_overlay()), strict=False).pages[0]
    sheet.merge_page(overlay)
    return sheet


def merge_monthly_print_pdfs(parts: Iterable[bytes]) -> bytes:
    """Pack adjacent landscape pages two-up while preserving exact page order."""
    writer = PdfWriter()
    pending_landscape: PageObject | None = None
    for content in parts:
        for page in PdfReader(io.BytesIO(content), strict=False).pages:
            if _is_landscape_page(page):
                if pending_landscape is None:
                    pending_landscape = page
                else:
                    writer.add_page(_two_sequential_pages(pending_landscape, page))
                    pending_landscape = None
                continue
            if pending_landscape is not None:
                writer.add_page(_single_sequential_page(pending_landscape))
                pending_landscape = None
            writer.add_page(page)
    if pending_landscape is not None:
        writer.add_page(_single_sequential_page(pending_landscape))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _is_landscape_page(page: PageObject) -> bool:
    page = copy(page)
    if page.rotation:
        page.transfer_rotation_to_content()
    box = page.cropbox
    width = abs(float(box.right) - float(box.left))
    height = abs(float(box.top) - float(box.bottom))
    return width > height * 1.1


def _is_landscape_receipt_content(page: PageObject) -> bool:
    page = copy(page)
    if page.rotation:
        page.transfer_rotation_to_content()
    left, bottom, right, top = _receipt_content_box(page)
    return (right - left) > (top - bottom) * 1.1


def _invoice_receipt_page(invoice: PageObject, receipt: PageObject) -> PageObject:
    """Place one invoice above its matching receipt on one portrait A4 sheet."""
    sheet = PageObject.create_blank_page(width=A4[0], height=A4[1])
    _merge_invoice_page_into_slot(sheet, invoice, slot_bottom=A4[1] / 2)
    _merge_page_into_slot(sheet, receipt, slot_bottom=0)
    overlay = PdfReader(io.BytesIO(_receipt_cut_overlay()), strict=False).pages[0]
    sheet.merge_page(overlay)
    return sheet


def build_item_pdf(
    item: VoucherItem | None,
    documents: Iterable[tuple[VoucherDocument, Path]],
    printed_by: str | None = None,
    approval_detail: dict[str, Any] | None = None,
    feishu_names: dict[str, str] | None = None,
    approval_comments: Any = None,
    *,
    include_approval: bool = True,
    pair_single_invoice_receipt: bool = False,
) -> bytes:
    """Approval, formal invoices, payment screenshots, then bank receipts."""
    document_rows = list(documents)
    writer = PdfWriter()
    if include_approval:
        if item is None:
            raise ValueError("导出审批页时缺少凭证条目")
        for page in PdfReader(
            io.BytesIO(
                _approval_pdf(
                    item,
                    document_rows,
                    printed_by,
                    approval_detail,
                    feishu_names,
                    approval_comments,
                )
            )
        ).pages:
            writer.add_page(page)
    formal_invoice_pages: list[tuple[PageObject, bool]] = []
    payment_screenshots: list[tuple[VoucherDocument, Path]] = []
    other_pages: list[PageObject] = []
    receipt_pages: list[PageObject] = []
    for document, path in document_rows:
        if (
            document.document_type == "invoice"
            and document.mime_type in {"image/jpeg", "image/png"}
        ):
            payment_screenshots.append((document, path))
            continue
        try:
            source = (
                path.read_bytes()
                if document.mime_type == "application/pdf"
                else _image_pdf(path)
            )
            reader = PdfReader(io.BytesIO(source), strict=False)
            for page in reader.pages:
                if (
                    document.document_type == "invoice"
                    and document.mime_type == "application/pdf"
                ):
                    formal_invoice_pages.append(
                        (page, _is_standalone_invoice(document, page))
                    )
                elif document.document_type == "bank_receipt":
                    receipt_pages.append(page)
                else:
                    other_pages.append(page)
        except Exception as exc:
            raise ValueError(f"附件 {document.original_filename} 无法转换为 PDF") from exc

    pair_one_to_one = (
        pair_single_invoice_receipt
        and not include_approval
        and len(formal_invoice_pages) == 1
        and not formal_invoice_pages[0][1]
        and not payment_screenshots
        and not other_pages
        and len(receipt_pages) == 1
        and _is_landscape_page(formal_invoice_pages[0][0])
        and _is_landscape_receipt_content(receipt_pages[0])
    )
    if pair_one_to_one:
        writer.add_page(
            _invoice_receipt_page(formal_invoice_pages[0][0], receipt_pages[0])
        )
        formal_invoice_pages = []
        receipt_pages = []

    pending_invoice: PageObject | None = None
    for page, standalone in formal_invoice_pages:
        if standalone:
            if pending_invoice is not None:
                writer.add_page(pending_invoice)
                pending_invoice = None
            writer.add_page(_single_invoice_a4_page(page))
            continue
        if pending_invoice is None:
            pending_invoice = page
        else:
            writer.add_page(_two_up_invoice_page(pending_invoice, page))
            pending_invoice = None
    if pending_invoice is not None:
        writer.add_page(pending_invoice)

    for index in range(0, len(payment_screenshots), 4):
        group = payment_screenshots[index : index + 4]
        try:
            sheet = _invoice_image_sheet([path for _document, path in group])
            for page in PdfReader(io.BytesIO(sheet), strict=False).pages:
                writer.add_page(page)
        except Exception as exc:
            filenames = "、".join(
                document.original_filename for document, _path in group
            )
            raise ValueError(f"附件 {filenames} 无法转换为 PDF") from exc

    for page in other_pages:
        writer.add_page(page)

    _add_receipt_pages(writer, receipt_pages)
    output = io.BytesIO()
    try:
        writer.write(output)
    except Exception as exc:
        raise ValueError("PDF 写入失败，附件可能已损坏") from exc
    return output.getvalue()


def build_receipt_packet_pdf(
    documents: Iterable[tuple[VoucherDocument, Path]],
) -> bytes:
    """Render optional invoice(s) first and their receipt last as one packet."""
    rows = list(documents)
    if sum(document.document_type == "bank_receipt" for document, _path in rows) != 1:
        raise ValueError("每组回单资料必须且只能包含一张银行回单")
    if any(
        document.document_type not in {"invoice", "bank_receipt"}
        for document, _path in rows
    ):
        raise ValueError("回单资料中包含不支持的附件")
    return build_item_pdf(
        None,
        rows,
        include_approval=False,
        pair_single_invoice_receipt=True,
    )


def build_receipt_pool_pdf(
    documents: Iterable[tuple[VoucherDocument, Path]],
) -> bytes:
    """Preserve A4 receipts and render only smaller receipts two-up."""
    receipt_pages: list[PageObject] = []
    for document, path in documents:
        if document.document_type != "bank_receipt":
            raise ValueError("只能导出银行回单")
        try:
            source = (
                path.read_bytes()
                if document.mime_type == "application/pdf"
                else _image_pdf(path)
            )
            receipt_pages.extend(
                PdfReader(io.BytesIO(source), strict=False).pages
            )
        except Exception as exc:
            raise ValueError(f"附件 {document.original_filename} 无法转换为 PDF") from exc
    if not receipt_pages:
        raise ValueError("没有可导出的银行回单")

    writer = PdfWriter()
    _add_receipt_pages(writer, receipt_pages)
    output = io.BytesIO()
    try:
        writer.write(output)
    except Exception as exc:
        raise ValueError("PDF 写入失败，附件可能已损坏") from exc
    return output.getvalue()


def merge_pdfs(pdf_files: Iterable[bytes]) -> bytes:
    try:
        writer = PdfWriter()
        for content in pdf_files:
            for page in PdfReader(io.BytesIO(content), strict=False).pages:
                writer.add_page(page)
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF 合并失败，附件可能已损坏") from exc


def completeness(link_types: Iterable[str]) -> dict[str, Any]:
    present = set(link_types)
    missing = [
        label
        for key, label in (("bank_receipt", "银行回单"), ("invoice", "发票"))
        if key not in present
    ]
    return {"complete": not missing, "missing": missing, "present": sorted(present)}
