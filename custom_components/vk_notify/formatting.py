"""
VK Notify formatting.py v1.7.0
Text formatting (HTML/Markdown/MarkdownV2 -> VK format_data), keyboard/reply builders,
and sensitive data redaction for logs and error messages.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

_SENSITIVE_QUERY_RE = re.compile(
    r"((?:access_token|video_access_token|token|api_key|apikey|key|hash|rhash|secret|sig|sign|password|passwd|pwd)\s*[=:]\s*)([^&\s\"',;})\]>]+)",
    flags=re.IGNORECASE,
)
_URL_AUTH_RE = re.compile(r"(https?://[^/\s:@]+:)([^@\s/]+)(@)", flags=re.IGNORECASE)
_VK_TOKEN_RE = re.compile(r"\bvk1\.a\.[A-Za-z0-9_\-.]{12,}\b")
_HEX_TOKEN_RE = re.compile(r"\b[a-fA-F0-9]{64,}\b")

_SENSITIVE_DICT_KEYS = {
    "access_token",
    "video_access_token",
    "token",
    "key",
    "hash",
    "rhash",
    "secret",
    "sig",
    "sign",
    "password",
    "peer_id",
    "peer_ids",
    "user_id",
    "user_ids",
    "group_id",
}


def redact_sensitive_text(value: Any) -> str:
    """Маскирует токены, пароли, хэши и ключи в URL и текстах ошибок для безопасной публикации логов."""
    text = str(value)
    if not text:
        return text
    text = _URL_AUTH_RE.sub(r"\1***REDACTED***\3", text)
    text = _VK_TOKEN_RE.sub("vk1.a.***REDACTED***", text)
    text = _HEX_TOKEN_RE.sub("***REDACTED_HEX***", text)
    text = _SENSITIVE_QUERY_RE.sub(r"\1***REDACTED***", text)
    return text


def redact_vk_payload(data: Any) -> Any:
    """Рекурсивно маскирует чувствительные поля в ответах и ошибках VK API."""
    if isinstance(data, dict):
        sanitized: dict[str, Any] = {}
        for k, v in data.items():
            k_lower = str(k).lower().strip()
            if k_lower in (
                "access_token",
                "video_access_token",
                "token",
                "key",
                "hash",
                "rhash",
                "secret",
                "sig",
                "sign",
            ):
                sanitized[k] = "***REDACTED***" if v else ""
            elif k_lower in ("peer_id", "peer_ids", "user_id", "user_ids", "group_id"):
                s = str(v)
                sanitized[k] = f"{s[:2]}***{s[-2:]}" if len(s) > 4 else "***"
            elif k_lower == "request_params" and isinstance(v, list):
                params_clean = []
                for item in v:
                    if isinstance(item, dict) and "key" in item:
                        ik = str(item["key"]).lower().strip()
                        iv = item.get("value")
                        if ik in _SENSITIVE_DICT_KEYS:
                            s_iv = str(iv)
                            masked_iv = (
                                f"{s_iv[:2]}***{s_iv[-2:]}"
                                if ik in ("peer_id", "peer_ids", "user_id", "user_ids", "group_id")
                                and len(s_iv) > 4
                                else "***REDACTED***"
                            )
                            params_clean.append({"key": item["key"], "value": masked_iv})
                        else:
                            params_clean.append(item)
                    else:
                        params_clean.append(redact_vk_payload(item))
                sanitized[k] = params_clean
            else:
                sanitized[k] = redact_vk_payload(v)
        return sanitized
    if isinstance(data, list):
        return [redact_vk_payload(x) for x in data]
    if isinstance(data, str):
        return redact_sensitive_text(data)
    return data


def encode_utf16_len(s: str) -> int:
    """Возвращает длину строки в UTF-16 кодовых единицах (по спецификации VK format_data)."""
    return len(s.encode("utf-16-le")) // 2


def parse_vk_formatting(text: str, parse_mode: str = "html") -> tuple[str, str | None]:
    """Парсит HTML / Markdown / MarkdownV2 в чистый текст и JSON-структуру VK format_data."""
    if not isinstance(text, str):
        return str(text), None

    if parse_mode == "plain":
        return text.strip(), None

    if parse_mode == "markdownv2":
        text = re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!])", r"\1", text)

    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^\*]+)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(
        r"(?<!\*)\*([^\s\*][^\*]*[^\s\*]|[^\s\*])\*(?!\*)",
        r"<i>\1</i>",
        text,
        flags=re.S,
    )
    text = re.sub(r"__([^_]+)__", r"<u>\1</u>", text, flags=re.S)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)

    pattern = re.compile(r"<(/)?(b|i|u|a|strong|em)(?: [^>]+)?>", flags=re.IGNORECASE)

    clean_text = ""
    current_pos = 0
    utf16_offset = 0
    open_tags: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []

    for match in pattern.finditer(text):
        start, end = match.span()
        chunk = text[current_pos:start]
        clean_text += chunk
        utf16_offset += encode_utf16_len(chunk)

        is_closing = bool(match.group(1))
        tag_name = match.group(2).lower()
        if tag_name == "strong":
            tag_name = "b"
        elif tag_name == "em":
            tag_name = "i"

        if not is_closing:
            tag_info: dict[str, Any] = {"tag": tag_name, "start": utf16_offset}
            if tag_name == "a":
                href_match = re.search(
                    r'href=["\']([^"\']+)["\']', match.group(0), flags=re.IGNORECASE
                )
                if href_match:
                    tag_info["url"] = href_match.group(1)
            open_tags.append(tag_info)
        else:
            for i in reversed(range(len(open_tags))):
                if open_tags[i]["tag"] == tag_name:
                    tag_info = open_tags.pop(i)
                    length = utf16_offset - int(tag_info["start"])
                    if length > 0:
                        vk_type = {
                            "b": "bold",
                            "i": "italic",
                            "u": "underline",
                            "a": "url",
                        }[tag_name]
                        item: dict[str, Any] = {
                            "offset": int(tag_info["start"]),
                            "length": length,
                            "type": vk_type,
                        }
                        if vk_type == "url" and "url" in tag_info:
                            item["url"] = tag_info["url"]
                        items.append(item)
                    break
        current_pos = end

    chunk = text[current_pos:]
    clean_text += chunk
    clean_text = re.sub(r"<[^>]+>", "", clean_text)

    format_data = None
    if items:
        format_data = json.dumps({"version": 1, "items": items}, ensure_ascii=False)

    return clean_text.strip(), format_data


def build_vk_keyboard(
    keyboard: dict[str, Any],
    inline_keyboard: bool | None = None,
    auto_answer_callback: bool = True,
) -> str:
    """Формирует JSON-строку клавиатуры ВК без мутации исходного словаря."""
    kb = copy.deepcopy(keyboard)
    if isinstance(kb, dict):
        if inline_keyboard is not None:
            kb["inline"] = inline_keyboard
        elif "inline" not in kb:
            kb["inline"] = True

        if auto_answer_callback:
            for row in kb.get("buttons", []):
                if not isinstance(row, list):
                    continue
                for btn in row:
                    if isinstance(btn, dict) and btn.get("action", {}).get("type") == "callback":
                        act = btn["action"]
                        p = act.get("payload", "{}")
                        if isinstance(p, str):
                            try:
                                p_obj = json.loads(p)
                            except ValueError:
                                p_obj = {"value": p}
                        elif isinstance(p, dict):
                            p_obj = dict(p)
                        else:
                            p_obj = {"value": p}
                        if isinstance(p_obj, dict):
                            p_obj["_ha_auto"] = True
                            act["payload"] = json.dumps(p_obj, ensure_ascii=False)

    return json.dumps(kb, ensure_ascii=False)

