"""
VK Notify helpers.py v1.6.2
Added: MIME-type & magic-bytes detection for photo/file/video uploads,
redirect-safe multipart POST, and detailed VK upload server diagnostics.
"""
from __future__ import annotations

from collections.abc import Callable
import ipaddress
import json
import logging
import os
import re
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    VK_API_DOC_SAVE,
    VK_API_DOC_UPLOAD_SERVER,
    VK_API_PHOTO_SAVE,
    VK_API_PHOTO_UPLOAD_SERVER,
    VK_API_VERSION,
)

_LOGGER = logging.getLogger(__name__)

_IMAGE_EXT_TO_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

_MIME_TO_IMAGE_EXTS: dict[str, tuple[str, ...]] = {
    "image/jpeg": (".jpg", ".jpeg", ".jpe"),
    "image/png": (".png",),
    "image/gif": (".gif",),
    "image/webp": (".webp",),
    "image/bmp": (".bmp",),
}

_MIME_DEFAULT_IMAGE_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}

_FILE_EXT_TO_MIME: dict[str, str] = {
    **_IMAGE_EXT_TO_MIME,
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".xml": "application/xml",
    ".zip": "application/zip",
    ".gz": "application/gzip",
    ".tar": "application/x-tar",
    ".7z": "application/x-7z-compressed",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def is_local_url(url: str) -> bool:
    """Проверяет, является ли URL локальным (192.168.x.x, 10.x.x.x, .local и т.д.)."""
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
            return True
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


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
    " peer_id",
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
            k_lower = str(k).lower()
            if k_lower in ("access_token", "video_access_token", "token", "key", "hash", "rhash", "secret", "sig", "sign"):
                sanitized[k] = "***REDACTED***" if v else ""
            elif k_lower in ("peer_id", "peer_ids", "user_id", "user_ids", "group_id"):
                s = str(v)
                sanitized[k] = f"{s[:2]}***{s[-2:]}" if len(s) > 4 else "***"
            elif k_lower == "request_params" and isinstance(v, list):
                params_clean = []
                for item in v:
                    if isinstance(item, dict) and "key" in item:
                        ik = str(item["key"]).lower()
                        iv = item.get("value")
                        if ik in _SENSITIVE_DICT_KEYS:
                            s_iv = str(iv)
                            masked_iv = (
                                f"{s_iv[:2]}***{s_iv[-2:]}"
                                if ik in ("peer_id", "peer_ids", "user_id", "user_ids", "group_id") and len(s_iv) > 4
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

# ==========================================
# 1. ФУНКЦИИ ФОРМАТИРОВАНИЯ ТЕКСТА
# ==========================================

def encode_utf16_len(s: str) -> int:
    return len(s.encode('utf-16-le')) // 2

def parse_vk_formatting(text: str, parse_mode: str = "html") -> tuple[str, str | None]:
    if not isinstance(text, str):
        return str(text), None

    if parse_mode == "plain":
        return text.strip(), None

    if parse_mode == "markdownv2":
        text = re.sub(r'\\([_*\[\]()~`>#+\-=|{}.!])', r'\1', text)

    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    text = re.sub(r'\*\*([^\*]+)\*\*', r'<b>\1</b>', text, flags=re.S)
    text = re.sub(r'(?<!\*)\*([^\s\*][^\*]*[^\s\*]|[^\s\*])\*(?!\*)', r'<i>\1</i>', text, flags=re.S)
    text = re.sub(r'__([^_]+)__', r'<u>\1</u>', text, flags=re.S)
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
    
    pattern = re.compile(r'<(/)?(b|i|u|a|strong|em)(?: [^>]+)?>', flags=re.IGNORECASE)
    
    clean_text = ""
    current_pos = 0
    utf16_offset = 0
    open_tags = []
    items = []
    
    for match in pattern.finditer(text):
        start, end = match.span()
        chunk = text[current_pos:start]
        clean_text += chunk
        utf16_offset += encode_utf16_len(chunk)
        
        is_closing = bool(match.group(1))
        tag_name = match.group(2).lower()
        if tag_name == 'strong': tag_name = 'b'
        elif tag_name == 'em': tag_name = 'i'
        
        if not is_closing:
            tag_info = {"tag": tag_name, "start": utf16_offset}
            if tag_name == 'a':
                href_match = re.search(r'href=["\']([^"\']+)["\']', match.group(0), flags=re.IGNORECASE)
                if href_match:
                    tag_info["url"] = href_match.group(1)
            open_tags.append(tag_info)
        else:
            for i in reversed(range(len(open_tags))):
                if open_tags[i]["tag"] == tag_name:
                    tag_info = open_tags.pop(i)
                    length = utf16_offset - tag_info["start"]
                    if length > 0:
                        vk_type = {"b": "bold", "i": "italic", "u": "underline", "a": "url"}[tag_name]
                        item = {"offset": tag_info["start"], "length": length, "type": vk_type}
                        if vk_type == "url" and "url" in tag_info:
                            item["url"] = tag_info["url"]
                        items.append(item)
                    break
        current_pos = end
    
    chunk = text[current_pos:]
    clean_text += chunk
    clean_text = re.sub(r'<[^>]+>', '', clean_text)
    
    format_data = None
    if items:
        format_data = json.dumps({"version": 1, "items": items}, ensure_ascii=False)
        
    return clean_text.strip(), format_data

# ==========================================
# 2. ФУНКЦИИ ЗАГРУЗКИ МЕДИАФАЙЛОВ
# ==========================================

def _read_file_safe(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _detect_magic_image_mime(data: bytes) -> str | None:
    """Определяет MIME-тип изображения по сигнатуре байтов (magic bytes)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return None


def _extract_filename_from_source(
    source: str,
    content_disposition: str | None = None,
    default_stem: str = "image",
) -> str:
    """Извлекает имя файла из заголовка Content-Disposition, URL или локального пути."""
    if content_disposition:
        cd_match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)', content_disposition, flags=re.IGNORECASE)
        if cd_match:
            candidate = os.path.basename(unquote(cd_match.group(1).strip()))
            if candidate and candidate not in (".", ".."):
                return candidate

    parsed_path = urlparse(source).path if "://" in source else source
    basename = os.path.basename(unquote(parsed_path.strip()))
    if not basename or basename in (".", "..", "/"):
        return default_stem
    return basename


def _detect_image_mime_and_filename(
    file_bytes: bytes,
    source: str,
    header_content_type: str | None = None,
    content_disposition: str | None = None,
) -> tuple[str, str]:
    """Определяет корректные (content_type, filename) для загрузки фото в ВК."""
    raw_filename = _extract_filename_from_source(source, content_disposition, default_stem="image")
    stem, ext = os.path.splitext(raw_filename)
    ext_lower = ext.lower()
    if not stem:
        stem = "image"

    header_mime = ""
    if header_content_type:
        header_mime = header_content_type.split(";", 1)[0].strip().lower()
        if header_mime == "image/jpg":
            header_mime = "image/jpeg"

    magic_mime = _detect_magic_image_mime(file_bytes)

    if magic_mime:
        mime_type = magic_mime
    elif header_mime in _MIME_DEFAULT_IMAGE_EXT:
        mime_type = header_mime
    elif ext_lower in _IMAGE_EXT_TO_MIME:
        mime_type = _IMAGE_EXT_TO_MIME[ext_lower]
    elif header_mime.startswith("image/"):
        mime_type = header_mime
    else:
        mime_type = "image/jpeg"

    valid_exts = _MIME_TO_IMAGE_EXTS.get(mime_type)
    default_ext = _MIME_DEFAULT_IMAGE_EXT.get(mime_type, ".jpg")
    if valid_exts:
        final_ext = ext_lower if ext_lower in valid_exts else default_ext
    else:
        final_ext = ext_lower or default_ext

    return mime_type, f"{stem}{final_ext}"


def _detect_file_mime(filename: str, default: str = "application/octet-stream") -> str:
    """Определяет MIME-тип документа/аудио/видео по расширению без блокирующего чтения диска."""
    _, ext = os.path.splitext(filename.lower())
    return _FILE_EXT_TO_MIME.get(ext, default)


async def _post_multipart_to_vk(
    session: aiohttp.ClientSession,
    upload_url: str,
    form_factory: Callable[[], aiohttp.FormData],
    timeout_ctx: aiohttp.ClientTimeout,
    media_label: str,
    filename: str,
    content_type: str,
    file_size: int,
    expect_json: bool = True,
) -> dict:
    """Отправляет multipart/form-data на сервер загрузки ВК с защитой от редиректа POST->GET и точной диагностикой."""
    current_url = upload_url
    raw_text = ""
    status = 0
    resp_ct = ""
    allow_hdr = ""

    for _redirect_step in range(4):
        async with session.post(
            current_url,
            data=form_factory(),
            timeout=timeout_ctx,
            allow_redirects=False,
        ) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if location:
                    current_url = urljoin(current_url, location)
                    _LOGGER.debug(
                        "VK upload server redirect (HTTP %s) -> %s",
                        resp.status,
                        current_url.split("?")[0],
                    )
                    continue
            raw_text = await resp.text()
            status = resp.status
            resp_ct = resp.headers.get("Content-Type", "")
            allow_hdr = resp.headers.get("Allow", "")
            break
    else:
        raise HomeAssistantError(
            f"Слишком много перенаправлений при загрузке {media_label} на сервер ВКонтакте."
        )

    safe_url = current_url.split("?")[0]
    body_preview = redact_sensitive_text(raw_text.strip()[:250])

    if status == 405:
        _LOGGER.error(
            "VK upload server returned HTTP 405 Method Not Allowed for %s "
            "(url=%s, allow=%s, filename=%s, content_type=%s, size=%d): %s",
            media_label,
            safe_url,
            allow_hdr,
            filename,
            content_type,
            file_size,
            body_preview,
        )
        raise HomeAssistantError(
            f"Сервер загрузки ВКонтакте отклонил HTTP-метод запроса (HTTP 405 Method Not Allowed, "
            f"url={safe_url}, Allow={allow_hdr or 'n/a'}, тип={content_type}, файл={filename}). "
            f"Ответ: {body_preview or 'пусто'}"
        )

    if 400 <= status < 500:
        _LOGGER.error(
            "VK upload server client error HTTP %s for %s "
            "(url=%s, filename=%s, content_type=%s, size=%d): %s",
            status,
            media_label,
            safe_url,
            filename,
            content_type,
            file_size,
            body_preview,
        )
        raise HomeAssistantError(
            f"Сервер загрузки ВКонтакте отклонил запрос (HTTP {status}, "
            f"тип={content_type}, файл={filename}, размер={file_size} Б): {body_preview or 'пустой ответ'}"
        )

    if status >= 500:
        _LOGGER.error(
            "VK upload server unavailable HTTP %s for %s (url=%s): %s",
            status,
            media_label,
            safe_url,
            body_preview,
        )
        raise HomeAssistantError(
            f"Сервер загрузки ВКонтакте временно недоступен (HTTP {status}). "
            f"Попробуйте позже. Ответ: {body_preview or 'пустой ответ'}"
        )

    if not expect_json:
        return {}

    try:
        upload_result = json.loads(raw_text)
    except Exception as err:
        _LOGGER.error(
            "VK upload server returned non-JSON response (HTTP %s, content-type=%s, url=%s): %s",
            status,
            resp_ct,
            safe_url,
            body_preview,
        )
        raise HomeAssistantError(
            f"Некорректный ответ сервера загрузки ВКонтакте (HTTP {status}, "
            f"Content-Type={resp_ct or 'n/a'}): {body_preview or 'пустой ответ'}"
        ) from err

    if not isinstance(upload_result, dict):
        raise HomeAssistantError(
            f"Неожиданный формат ответа сервера загрузки ВКонтакте: {body_preview}"
        )

    return upload_result

async def async_upload_photo(
    hass: HomeAssistant,
    access_token: str,
    peer_id: int,
    url: str | None = None,
    filepath: str | None = None,
    verify_ssl: bool = True,
    timeout: int = 60,
) -> str:
    """Загрузка фотографии в личные сообщения / беседу ВК по URL или локальному пути."""
    timeout_ctx = aiohttp.ClientTimeout(total=timeout)
    vk_session = async_get_clientsession(hass, verify_ssl=verify_ssl)

    if url:
        clean_url = url.strip()
        bypass_dl_ssl = (not verify_ssl) or is_local_url(clean_url)
        dl_session = async_get_clientsession(hass, verify_ssl=(not bypass_dl_ssl))
        ssl_ctx = False if bypass_dl_ssl else None
        try:
            async with dl_session.get(clean_url, ssl=ssl_ctx, timeout=timeout_ctx) as img_resp:
                if img_resp.status >= 400:
                    err_body = (await img_resp.text())[:200]
                    raise HomeAssistantError(
                        f"Источник изображения вернул HTTP {img_resp.status}: {err_body}"
                    )
                file_bytes = await img_resp.read()
                header_ct = img_resp.headers.get("Content-Type")
                content_disp = img_resp.headers.get("Content-Disposition")
        except HomeAssistantError:
            raise
        except Exception as err:
            safe_dl_url = redact_sensitive_text(clean_url)
            safe_err = redact_sensitive_text(err)
            _LOGGER.error("Ошибка скачивания фото по URL %s: %s", safe_dl_url, safe_err)
            raise HomeAssistantError(f"Failed to download image: {safe_err}") from err

        if not file_bytes:
            raise HomeAssistantError(
                f"Скачанное по URL изображение пустое (0 байт): {redact_sensitive_text(clean_url)}"
            )

        magic_mime = _detect_magic_image_mime(file_bytes)
        header_mime = (header_ct or "").split(";", 1)[0].strip().lower()
        if not magic_mime and (
            header_mime in ("text/html", "text/plain", "application/json")
            or file_bytes.lstrip()[:15].lower().startswith((b"<!doctype", b"<html"))
        ):
            preview = redact_sensitive_text(file_bytes[:200].decode("utf-8", errors="replace").strip())
            raise HomeAssistantError(
                f"По URL получено не изображение (Content-Type: {header_ct or 'неизвестен'}): {preview}"
            )

        content_type, filename = _detect_image_mime_and_filename(
            file_bytes,
            source=clean_url,
            header_content_type=header_ct,
            content_disposition=content_disp,
        )
    elif filepath:
        clean_path = filepath.strip()
        if not hass.config.is_allowed_path(clean_path):
            raise HomeAssistantError(f"Path '{clean_path}' not allowed.")
        file_bytes = await hass.async_add_executor_job(_read_file_safe, clean_path)
        if not file_bytes:
            raise HomeAssistantError(f"Локальный файл изображения пуст (0 байт): '{clean_path}'")
        content_type, filename = _detect_image_mime_and_filename(file_bytes, source=clean_path)
    else:
        raise HomeAssistantError("Neither URL nor filepath provided for photo upload.")

    async with vk_session.get(
        VK_API_PHOTO_UPLOAD_SERVER,
        params={"access_token": access_token, "peer_id": peer_id, "v": VK_API_VERSION},
        timeout=timeout_ctx,
    ) as resp:
        data = await resp.json(content_type=None)
    if "error" in data:
        raise HomeAssistantError(
            f"VK API error (getUploadServer): {redact_vk_payload(data['error'])}"
        )

    upload_url = data.get("response", {}).get("upload_url")
    if not upload_url:
        raise HomeAssistantError(f"VK API did not return upload_url: {redact_vk_payload(data)}")

    def _build_form() -> aiohttp.FormData:
        form = aiohttp.FormData()
        form.add_field(
            "photo",
            file_bytes,
            filename=filename,
            content_type=content_type,
        )
        return form

    upload_result = await _post_multipart_to_vk(
        session=vk_session,
        upload_url=upload_url,
        form_factory=_build_form,
        timeout_ctx=timeout_ctx,
        media_label="photo",
        filename=filename,
        content_type=content_type,
        file_size=len(file_bytes),
    )

    photo_payload = upload_result.get("photo")
    if "error" in upload_result or not photo_payload or photo_payload == "[]":
        safe_result = redact_vk_payload(upload_result)
        _LOGGER.error(
            "VK photo upload returned empty/error photo field (filename=%s, content_type=%s, size=%d): %s",
            filename,
            content_type,
            len(file_bytes),
            safe_result,
        )
        raise HomeAssistantError(
            f"Photo upload error (file={filename}, mime={content_type}, size={len(file_bytes)} B): {safe_result}"
        )

    async with vk_session.post(
        VK_API_PHOTO_SAVE,
        data={
            "access_token": access_token,
            "v": VK_API_VERSION,
            "photo": photo_payload,
            "server": upload_result["server"],
            "hash": upload_result["hash"],
        },
        timeout=timeout_ctx,
    ) as resp:
        data = await resp.json(content_type=None)

    if "error" in data:
        raise HomeAssistantError(
            f"VK API error (saveMessagesPhoto): {redact_vk_payload(data['error'])}"
        )

    photo = data["response"][0]
    access_key = photo.get("access_key", "")
    return f"photo{photo['owner_id']}_{photo['id']}{'_' + access_key if access_key else ''}"

async def async_upload_file(
    hass: HomeAssistant,
    access_token: str,
    peer_id: int,
    filepath: str,
    verify_ssl: bool = True,
    timeout: int = 60,
) -> str:
    """Загрузка документа или голосового сообщения (.ogg) в ВК."""
    vk_session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    timeout_ctx = aiohttp.ClientTimeout(total=timeout)
    clean_path = filepath.strip()
    if not hass.config.is_allowed_path(clean_path):
        raise HomeAssistantError(f"Path '{clean_path}' not allowed.")

    filename = os.path.basename(clean_path) or "document"
    file_bytes = await hass.async_add_executor_job(_read_file_safe, clean_path)
    if not file_bytes:
        raise HomeAssistantError(f"Локальный файл пуст (0 байт): '{clean_path}'")

    is_voice = filename.lower().endswith(".ogg")
    doc_type = "audio_message" if is_voice else "doc"
    content_type = "audio/ogg" if is_voice else _detect_file_mime(filename)

    async with vk_session.get(
        VK_API_DOC_UPLOAD_SERVER,
        params={
            "access_token": access_token,
            "peer_id": peer_id,
            "type": doc_type,
            "v": VK_API_VERSION,
        },
        timeout=timeout_ctx,
    ) as resp:
        data = await resp.json(content_type=None)
    if "error" in data:
        raise HomeAssistantError(
            f"VK API error (getUploadServer): {redact_vk_payload(data['error'])}"
        )

    upload_url = data.get("response", {}).get("upload_url")
    if not upload_url:
        raise HomeAssistantError(f"VK API did not return doc upload_url: {redact_vk_payload(data)}")

    def _build_form() -> aiohttp.FormData:
        form = aiohttp.FormData()
        form.add_field(
            "file",
            file_bytes,
            filename=filename,
            content_type=content_type,
        )
        return form

    upload_result = await _post_multipart_to_vk(
        session=vk_session,
        upload_url=upload_url,
        form_factory=_build_form,
        timeout_ctx=timeout_ctx,
        media_label=doc_type,
        filename=filename,
        content_type=content_type,
        file_size=len(file_bytes),
    )

    if "error" in upload_result or not upload_result.get("file"):
        raise HomeAssistantError(
            f"Document upload error (file={filename}): {redact_vk_payload(upload_result)}"
        )

    async with vk_session.get(
        VK_API_DOC_SAVE,
        params={
            "access_token": access_token,
            "v": VK_API_VERSION,
            "file": upload_result["file"],
            "title": filename,
        },
        timeout=timeout_ctx,
    ) as resp:
        data = await resp.json(content_type=None)
    if "error" in data:
        raise HomeAssistantError(f"VK API error (docs.save): {redact_vk_payload(data['error'])}")

    response = data["response"]
    obj = response.get("doc") or response.get("audio_message")
    attachment_type = "doc" if "doc" in response else "audio_message"
    access_key = obj.get("access_key", "")
    return f"{attachment_type}{obj['owner_id']}_{obj['id']}{'_' + access_key if access_key else ''}"

async def async_upload_video(
    hass: HomeAssistant,
    access_token: str,
    peer_id: int,
    video_access_token: str,
    url: str | None = None,
    filepath: str | None = None,
    caption: str = "",
    verify_ssl: bool = True,
    timeout: int = 60,
) -> str:
    """Загрузка нативного видео в плеер ВК с помощью токена пользователя."""
    vk_session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    timeout_ctx = aiohttp.ClientTimeout(total=timeout)

    if url:
        clean_url = url.strip()
        bypass_dl_ssl = (not verify_ssl) or is_local_url(clean_url)
        dl_session = async_get_clientsession(hass, verify_ssl=(not bypass_dl_ssl))
        ssl_ctx = False if bypass_dl_ssl else None
        try:
            async with dl_session.get(clean_url, ssl=ssl_ctx, timeout=timeout_ctx) as vid_resp:
                if vid_resp.status >= 400:
                    err_body = (await vid_resp.text())[:200]
                    raise HomeAssistantError(
                        f"Источник видео вернул HTTP {vid_resp.status}: {err_body}"
                    )
                file_bytes = await vid_resp.read()
                content_disp = vid_resp.headers.get("Content-Disposition")
                filename = _extract_filename_from_source(
                    clean_url, content_disp, default_stem="video.mp4"
                )
                if not os.path.splitext(filename)[1]:
                    filename += ".mp4"
        except HomeAssistantError:
            raise
        except Exception as err:
            safe_dl_url = redact_sensitive_text(clean_url)
            safe_err = redact_sensitive_text(err)
            _LOGGER.error("Ошибка скачивания видео по URL %s: %s", safe_dl_url, safe_err)
            raise HomeAssistantError(f"Failed to download video: {safe_err}") from err
    elif filepath:
        clean_path = filepath.strip()
        if not hass.config.is_allowed_path(clean_path):
            raise HomeAssistantError(f"Path '{clean_path}' not allowed.")
        file_bytes = await hass.async_add_executor_job(_read_file_safe, clean_path)
        filename = os.path.basename(clean_path) or "video.mp4"
    else:
        raise HomeAssistantError("Neither URL nor filepath provided for video upload.")

    if not file_bytes:
        raise HomeAssistantError(f"Файл видео пуст (0 байт): {filename}")

    content_type = _detect_file_mime(filename, default="video/mp4")

    async with vk_session.get(
        "https://api.vk.com/method/video.save",
        params={
            "access_token": video_access_token,
            "name": filename,
            "description": caption[:4000],
            "is_private": 0,
            "v": VK_API_VERSION,
        },
        timeout=timeout_ctx,
    ) as resp:
        data = await resp.json(content_type=None)

    if "error" in data:
        raise HomeAssistantError(f"VK API error (video.save): {redact_vk_payload(data['error'])}")

    upload_url = data["response"]["upload_url"]
    owner_id = data["response"]["owner_id"]
    video_id = data["response"].get("video_id") or data["response"].get("id")
    access_key = data["response"].get("access_key", "")

    def _build_form() -> aiohttp.FormData:
        form = aiohttp.FormData()
        form.add_field(
            "video_file",
            file_bytes,
            filename=filename,
            content_type=content_type,
        )
        return form

    await _post_multipart_to_vk(
        session=vk_session,
        upload_url=upload_url,
        form_factory=_build_form,
        timeout_ctx=timeout_ctx,
        media_label="video",
        filename=filename,
        content_type=content_type,
        file_size=len(file_bytes),
        expect_json=False,
    )

    return f"video{owner_id}_{video_id}{'_' + access_key if access_key else ''}"
