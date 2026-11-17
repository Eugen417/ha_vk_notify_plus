"""
VK Notify media.py v1.0.0
Media downloading, camera snapshot capture, media-source resolution,
MIME/magic-bytes detection, WebP/BMP conversion, and VK multipart uploads.
"""
from __future__ import annotations

from collections.abc import Callable
import asyncio
import io
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
from homeassistant.helpers.network import get_url

from .api import VkApiClient
from .const import (
    VK_API_DOC_SAVE,
    VK_API_DOC_UPLOAD_SERVER,
    VK_API_PHOTO_SAVE,
    VK_API_PHOTO_UPLOAD_SERVER,
    VK_API_VERSION,
    VK_API_VIDEO_SAVE,
)
from .formatting import redact_sensitive_text, redact_vk_payload

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


def _read_file_safe(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _convert_webp_or_bmp_sync(data: bytes, stem: str) -> tuple[bytes, str, str] | None:
    """Конвертирует WebP/BMP в PNG/JPEG в памяти через Pillow (если установлен)."""
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(io.BytesIO(data)) as img:
            out = io.BytesIO()
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                img.save(out, format="PNG")
                return out.getvalue(), "image/png", "image.png"
            rgb = img.convert("RGB")
            rgb.save(out, format="JPEG", quality=92)
            return out.getvalue(), "image/jpeg", "image.jpg"
    except Exception:
        return None


def _normalize_image_for_vk_sync(
    data: bytes,
    mime_type: str,
    force_reencode: bool = False,
    *,
    max_side: int = 2560,
    jpeg_quality: int = 88,
    prefer_png: bool = False,
) -> tuple[bytes, str, str, bool]:
    """Нормализует изображение под требования сервера загрузки ВК:
    - ресайз до max_side по длинной стороне и апскейл слишком мелких кадров до >= 64px;
    - стандартные таблицы Хаффмана JFIF (optimize=False, subsampling=0) без вырождения на тёмных кадрах;
    - защита от ложного срабатывания decompression-ratio фильтра ВК на сплошных чёрных 2K/4K кадрах;
    - опциональный PNG-фоллбек (prefer_png=True) при повторных отказах JPEG-парсера ВК.
    """
    ext = _MIME_DEFAULT_IMAGE_EXT.get(mime_type, ".jpg")
    short_filename = f"image{ext}"
    if mime_type == "image/gif" and not force_reencode and not prefer_png:
        return data, mime_type, "image.gif", True

    try:
        from PIL import Image, ImageFile, ImageOps  # noqa: PLC0415
    except ImportError:
        return data, mime_type, short_filename, True

    try:
        ImageFile.LOAD_TRUNCATED_IMAGES = True

        with Image.open(io.BytesIO(data)) as img:
            img.load()
            w, h = img.size
            if w < 4 or h < 4:
                return data, mime_type, short_filename, False

            work = ImageOps.exif_transpose(img)
            w, h = work.size

            if min(w, h) < 64:
                up_scale = min(64.0 / float(min(w, h)), float(max_side) / float(max(w, h)))
                if up_scale > 1.0:
                    new_w = max(64, int(round(w * up_scale)))
                    new_h = max(64, int(round(h * up_scale)))
                    work = work.resize((new_w, new_h), Image.Resampling.BILINEAR)
                    w, h = work.size

            if max(w, h) > max_side:
                scale = float(max_side) / float(max(w, h))
                new_w = max(1, int(w * scale))
                new_h = max(1, int(h * scale))
                work = work.resize((new_w, new_h), Image.Resampling.LANCZOS)
                w, h = work.size

            if (max(w, h) / max(1, min(w, h))) > 19.0:
                if w > h:
                    target_h = max(1, int(w / 15.0))
                    canvas = Image.new("RGB", (w, target_h), (0, 0, 0))
                    canvas.paste(work.convert("RGB"), (0, (target_h - h) // 2))
                    work = canvas
                else:
                    target_w = max(1, int(h / 15.0))
                    canvas = Image.new("RGB", (target_w, h), (0, 0, 0))
                    canvas.paste(work.convert("RGB"), ((target_w - w) // 2, 0))
                    work = canvas
                w, h = work.size

            out = io.BytesIO()
            has_alpha = work.mode in ("RGBA", "LA") or "transparency" in work.info
            if prefer_png or (
                mime_type == "image/png" and not force_reencode and has_alpha
            ):
                png_img = work if has_alpha else work.convert("RGB")
                png_img.save(out, format="PNG")
                return out.getvalue(), "image/png", "image.png", True

            rgb = work.convert("RGB")
            rgb.save(
                out,
                format="JPEG",
                quality=jpeg_quality,
                optimize=False,
                progressive=False,
                subsampling=0,
            )
            encoded = out.getvalue()

            # На сплошных тёмных/однотонных 2K/4K кадрах (< 32 КБ) отношение разжатого буфера к файлу
            # превышает 400:1, из-за чего upload-сервер ВК может вернуть пустой photo: "".
            if max(w, h) > 1280 and len(encoded) < 32768:
                scale_dark = 1280.0 / float(max(w, h))
                dw = max(64, int(w * scale_dark))
                dh = max(64, int(h * scale_dark))
                dark_rgb = rgb.resize((dw, dh), Image.Resampling.LANCZOS)
                out_dark = io.BytesIO()
                dark_rgb.save(
                    out_dark,
                    format="JPEG",
                    quality=90,
                    optimize=False,
                    progressive=False,
                    subsampling=0,
                )
                encoded = out_dark.getvalue()

            return encoded, "image/jpeg", "image.jpg", True
    except Exception as err:
        _LOGGER.debug("Image normalization via Pillow failed: %s", err)
        return data, mime_type, short_filename, False


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
        cd_match = re.search(
            r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)',
            content_disposition,
            flags=re.IGNORECASE,
        )
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
    raw_filename = _extract_filename_from_source(
        source, content_disposition, default_stem="image"
    )
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


async def _async_resolve_media_source(
    hass: HomeAssistant,
    url: str | None,
    filepath: str | None,
) -> tuple[str | None, str | None]:
    """Преобразует media-source:// URI в локальный путь или HTTP URL."""
    target = (filepath or url or "").strip()
    if not target.startswith("media-source://"):
        return url, filepath

    try:
        from homeassistant.components import media_source  # noqa: PLC0415

        sourced = await media_source.async_resolve_media(hass, target, None)
        if sourced.path:
            return None, str(sourced.path)
        if sourced.url:
            if sourced.url.startswith("/"):
                return f"{get_url(hass)}{sourced.url}", None
            return sourced.url, None
    except Exception as err:
        raise HomeAssistantError(
            f"Не удалось разрешить media-source URI: {redact_sensitive_text(err)}"
        ) from err

    return url, filepath


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
    *,
    return_transient_error: bool = False,
) -> dict[str, Any]:
    """Отправляет multipart/form-data на сервер загрузки ВК с защитой от редиректа POST->GET и точной диагностикой."""
    current_url = upload_url
    raw_text = ""
    status = 0
    resp_ct = ""
    allow_hdr = ""

    try:
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
    except HomeAssistantError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as net_err:
        safe_url = current_url.split("?")[0]
        safe_err = redact_sensitive_text(net_err)
        if return_transient_error:
            return {
                "_transient_error": f"Сбой соединения с нодой загрузки ВК ({safe_url}): {safe_err}"
            }
        _LOGGER.error(
            "VK upload server network error for %s (url=%s): %s",
            media_label,
            safe_url,
            safe_err,
        )
        raise HomeAssistantError(
            f"Ошибка соединения с сервером загрузки ВКонтакте ({safe_url}): {safe_err}"
        ) from net_err

    safe_url = current_url.split("?")[0]
    body_preview = redact_sensitive_text(raw_text.strip()[:250])

    if status == 429 or status >= 500:
        if return_transient_error:
            return {
                "_transient_error": (
                    f"HTTP {status} ({safe_url}): {body_preview or 'пустой ответ'}"
                )
            }
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

    if not expect_json:
        return {}

    try:
        upload_result = json.loads(raw_text)
    except Exception as err:
        if return_transient_error:
            return {
                "_transient_error": (
                    f"Не-JSON ответ HTTP {status} ({safe_url}): {body_preview or 'пустой ответ'}"
                )
            }
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
        if return_transient_error:
            return {"_transient_error": f"Неожиданный ответ ноды ВК: {body_preview}"}
        raise HomeAssistantError(
            f"Неожиданный формат ответа сервера загрузки ВКонтакте: {body_preview}"
        )

    return upload_result


async def _async_capture_camera_snapshot(
    hass: HomeAssistant,
    camera_entity: str,
    timeout: int,
) -> tuple[bytes, bytes, str, str]:
    """Захватывает и нормализует снимок с камеры HA полностью в памяти (до 3 попыток).
    Возвращает (raw_bytes, norm_bytes, norm_mime, norm_filename).
    """
    last_cam_err: Exception | None = None
    for cam_attempt in range(3):
        try:
            from homeassistant.components import camera as ha_camera  # noqa: PLC0415

            image = await ha_camera.async_get_image(
                hass, camera_entity, timeout=timeout
            )
            raw_bytes = image.content
            header_ct = image.content_type
            if raw_bytes and len(raw_bytes) > 512:
                det_mime, _ = _detect_image_mime_and_filename(
                    raw_bytes, source="image.jpg", header_content_type=header_ct
                )
                norm_bytes, norm_mime, norm_name, is_valid = (
                    await hass.async_add_executor_job(
                        _normalize_image_for_vk_sync, raw_bytes, det_mime, True
                    )
                )
                if is_valid and len(norm_bytes) > 512:
                    return raw_bytes, norm_bytes, norm_mime, norm_name
        except Exception as err:
            last_cam_err = err
        if cam_attempt < 2:
            await asyncio.sleep(0.6 * (cam_attempt + 1))

    if last_cam_err:
        raise HomeAssistantError(
            f"Не удалось получить снимок с камеры '{camera_entity}': {redact_sensitive_text(last_cam_err)}"
        ) from last_cam_err
    raise HomeAssistantError(
        f"Камера '{camera_entity}' вернула пустой или повреждённый кадр."
    )


async def async_upload_photo(
    hass: HomeAssistant,
    access_token: str,
    peer_id: int,
    url: str | None = None,
    filepath: str | None = None,
    camera_entity: str | None = None,
    verify_ssl: bool = True,
    timeout: int = 60,
    *,
    _prefetched_camera: tuple[bytes, bytes, str, str] | None = None,
) -> str:
    """Загрузка фотографии в ВК с камеры HA (camera_entity), по URL, media-source:// или локальному пути."""
    timeout_ctx = aiohttp.ClientTimeout(total=timeout)
    vk_session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    client = VkApiClient(hass, access_token, verify_ssl=verify_ssl)
    url, filepath = await _async_resolve_media_source(hass, url, filepath)

    if camera_entity:
        if _prefetched_camera is not None:
            source_raw_bytes, file_bytes, content_type, filename = _prefetched_camera
        else:
            source_raw_bytes, file_bytes, content_type, filename = (
                await _async_capture_camera_snapshot(hass, camera_entity, timeout)
            )
        orig_content_type, _ = _detect_image_mime_and_filename(
            source_raw_bytes, source="image.jpg"
        )
        upload_filename = "image.jpg" if content_type == "image/jpeg" else filename
    elif url:
        clean_url = url.strip()
        bypass_dl_ssl = (not verify_ssl) or is_local_url(clean_url)
        dl_session = async_get_clientsession(hass, verify_ssl=(not bypass_dl_ssl))
        ssl_ctx = False if bypass_dl_ssl else None
        try:
            async with dl_session.get(clean_url, ssl=ssl_ctx, timeout=timeout_ctx) as img_resp:
                if img_resp.status >= 400:
                    err_body = redact_sensitive_text((await img_resp.text())[:200])
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
            preview = redact_sensitive_text(
                file_bytes[:200].decode("utf-8", errors="replace").strip()
            )
            raise HomeAssistantError(
                f"По URL получено не изображение (Content-Type: {header_ct or 'неизвестен'}): {preview}"
            )

        content_type, filename = _detect_image_mime_and_filename(
            file_bytes,
            source=clean_url,
            header_content_type=header_ct,
            content_disposition=content_disp,
        )
        source_raw_bytes = file_bytes
        orig_content_type = content_type
    elif filepath:
        clean_path = filepath.strip()
        if not hass.config.is_allowed_path(clean_path):
            raise HomeAssistantError(f"Path '{clean_path}' not allowed.")
        file_bytes = await hass.async_add_executor_job(_read_file_safe, clean_path)
        if not file_bytes:
            raise HomeAssistantError(f"Локальный файл изображения пуст (0 байт): '{clean_path}'")
        content_type, filename = _detect_image_mime_and_filename(file_bytes, source=clean_path)
        source_raw_bytes = file_bytes
        orig_content_type = content_type
    else:
        raise HomeAssistantError(
            "Для отправки фото укажите camera_entity, url или локальный путь file."
        )

    if not camera_entity:
        file_bytes, content_type, upload_filename, _ = await hass.async_add_executor_job(
            _normalize_image_for_vk_sync,
            file_bytes,
            content_type,
            False,
        )

    max_upload_attempts = 4
    upload_result: dict[str, Any] = {}
    photo_payload: Any = None
    source_label = camera_entity or filename

    for attempt in range(max_upload_attempts):
        req_params: dict[str, Any] = {}
        if peer_id and (attempt % 2 == 0):
            req_params["peer_id"] = peer_id

        try:
            resp_data = await client.async_request(
                VK_API_PHOTO_UPLOAD_SERVER,
                req_params,
                http_method="GET",
                timeout=timeout,
            )
        except HomeAssistantError:
            if attempt < max_upload_attempts - 1:
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
            raise

        upload_url = (resp_data or {}).get("upload_url")
        if not upload_url:
            if attempt < max_upload_attempts - 1:
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
            raise HomeAssistantError(
                f"VK API did not return upload_url: {redact_vk_payload(resp_data)}"
            )

        def _build_form(
            payload_bytes: bytes = file_bytes,
            payload_name: str = upload_filename,
            payload_mime: str = content_type,
        ) -> aiohttp.FormData:
            form = aiohttp.FormData()
            form.add_field(
                "photo",
                payload_bytes,
                filename=payload_name,
                content_type=payload_mime,
            )
            return form

        upload_result = await _post_multipart_to_vk(
            session=vk_session,
            upload_url=upload_url,
            form_factory=_build_form,
            timeout_ctx=timeout_ctx,
            media_label="photo",
            filename=upload_filename,
            content_type=content_type,
            file_size=len(file_bytes),
            return_transient_error=(attempt < max_upload_attempts - 1),
        )

        transient_err = upload_result.get("_transient_error")
        photo_payload = upload_result.get("photo")
        if (
            not transient_err
            and "error" not in upload_result
            and photo_payload
            and photo_payload not in ("[]", '""')
        ):
            break

        if attempt < max_upload_attempts - 1:
            reason = transient_err or f"empty/error photo ({redact_vk_payload(upload_result)})"
            _LOGGER.warning(
                "VK photo upload retry %d/%d for %s (mime=%s, size=%d): %s",
                attempt + 1,
                max_upload_attempts - 1,
                redact_sensitive_text(source_label),
                content_type,
                len(file_bytes),
                redact_sensitive_text(reason),
            )
            await asyncio.sleep(0.8 * (attempt + 1))

            use_png = attempt >= 1
            target_side = 1280 if use_png else 1920

            def _reencode_for_retry(
                raw_b: bytes = source_raw_bytes,
                raw_ct: str = orig_content_type,
                side: int = target_side,
                as_png: bool = use_png,
            ) -> tuple[bytes, str, str, bool]:
                return _normalize_image_for_vk_sync(
                    raw_b,
                    raw_ct,
                    True,
                    max_side=side,
                    jpeg_quality=85,
                    prefer_png=as_png,
                )

            re_bytes, re_mime, re_name, re_ok = await hass.async_add_executor_job(
                _reencode_for_retry
            )
            if re_ok and re_bytes:
                file_bytes = re_bytes
                content_type = re_mime
                upload_filename = re_name

    if (
        upload_result.get("_transient_error")
        or "error" in upload_result
        or not photo_payload
        or photo_payload in ("[]", '""')
    ):
        safe_result = redact_vk_payload(upload_result)
        _LOGGER.error(
            "VK photo upload returned empty/error photo field (source=%s, content_type=%s, size=%d): %s",
            redact_sensitive_text(source_label),
            content_type,
            len(file_bytes),
            safe_result,
        )
        raise HomeAssistantError(
            f"Photo upload error (source={redact_sensitive_text(source_label)}, mime={content_type}, size={len(file_bytes)} B): {safe_result}"
        )

    saved_photos = await client.async_request(
        VK_API_PHOTO_SAVE,
        {
            "photo": photo_payload,
            "server": upload_result["server"],
            "hash": upload_result["hash"],
        },
        http_method="POST",
        timeout=timeout,
    )
    if not isinstance(saved_photos, list) or not saved_photos:
        raise HomeAssistantError(
            f"VK API error (saveMessagesPhoto): unexpected response {redact_vk_payload(saved_photos)}"
        )

    photo = saved_photos[0]
    access_key = photo.get("access_key", "")
    return f"photo{photo['owner_id']}_{photo['id']}{'_' + access_key if access_key else ''}"


async def async_upload_file(
    hass: HomeAssistant,
    access_token: str,
    peer_id: int,
    filepath: str | None = None,
    url: str | None = None,
    verify_ssl: bool = True,
    timeout: int = 60,
    force_voice: bool = False,
) -> str:
    """Загрузка документа или голосового сообщения (.ogg) в ВК по локальному пути, URL или media-source://."""
    vk_session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    timeout_ctx = aiohttp.ClientTimeout(total=timeout)
    url, filepath = await _async_resolve_media_source(hass, url, filepath)

    if url:
        clean_url = url.strip()
        bypass_dl_ssl = (not verify_ssl) or is_local_url(clean_url)
        dl_session = async_get_clientsession(hass, verify_ssl=(not bypass_dl_ssl))
        ssl_ctx = False if bypass_dl_ssl else None
        try:
            async with dl_session.get(clean_url, ssl=ssl_ctx, timeout=timeout_ctx) as doc_resp:
                if doc_resp.status >= 400:
                    err_body = redact_sensitive_text((await doc_resp.text())[:200])
                    raise HomeAssistantError(
                        f"Источник файла вернул HTTP {doc_resp.status}: {err_body}"
                    )
                file_bytes = await doc_resp.read()
                content_disp = doc_resp.headers.get("Content-Disposition")
                filename = _extract_filename_from_source(
                    clean_url, content_disp, default_stem="document"
                )
        except HomeAssistantError:
            raise
        except Exception as err:
            safe_dl_url = redact_sensitive_text(clean_url)
            safe_err = redact_sensitive_text(err)
            _LOGGER.error("Ошибка скачивания файла по URL %s: %s", safe_dl_url, safe_err)
            raise HomeAssistantError(f"Failed to download file: {safe_err}") from err
    elif filepath:
        clean_path = filepath.strip()
        if not hass.config.is_allowed_path(clean_path):
            raise HomeAssistantError(f"Path '{clean_path}' not allowed.")
        filename = os.path.basename(clean_path) or "document"
        file_bytes = await hass.async_add_executor_job(_read_file_safe, clean_path)
    else:
        raise HomeAssistantError("Neither filepath nor URL provided for file upload.")

    if not file_bytes:
        raise HomeAssistantError(f"Файл пуст (0 байт): '{filename}'")

    is_voice = force_voice or filename.lower().endswith((".ogg", ".oga", ".opus"))
    doc_type = "audio_message" if is_voice else "doc"
    content_type = "audio/ogg" if is_voice else _detect_file_mime(filename)
    client = VkApiClient(hass, access_token, verify_ssl=verify_ssl)

    upload_result: dict[str, Any] = {}
    max_doc_attempts = 3
    for attempt in range(max_doc_attempts):
        resp_data = await client.async_request(
            VK_API_DOC_UPLOAD_SERVER,
            {"peer_id": peer_id, "type": doc_type},
            http_method="GET",
            timeout=timeout,
        )
        upload_url = (resp_data or {}).get("upload_url")
        if not upload_url:
            raise HomeAssistantError(
                f"VK API did not return doc upload_url: {redact_vk_payload(resp_data)}"
            )

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
            return_transient_error=(attempt < max_doc_attempts - 1),
        )
        transient_err = upload_result.get("_transient_error")
        if not transient_err and "error" not in upload_result and upload_result.get("file"):
            break
        if attempt < max_doc_attempts - 1:
            _LOGGER.warning(
                "VK doc upload retry %d/%d for %s: %s",
                attempt + 1,
                max_doc_attempts - 1,
                redact_sensitive_text(filename),
                redact_sensitive_text(transient_err or upload_result),
            )
            await asyncio.sleep(0.8 * (attempt + 1))

    if upload_result.get("_transient_error") or "error" in upload_result or not upload_result.get("file"):
        raise HomeAssistantError(
            f"Document upload error (file={filename}): {redact_vk_payload(upload_result)}"
        )

    response = await client.async_request(
        VK_API_DOC_SAVE,
        {"file": upload_result["file"], "title": filename},
        http_method="GET",
        timeout=timeout,
    )
    if not isinstance(response, dict):
        raise HomeAssistantError(
            f"VK API error (docs.save): unexpected response {redact_vk_payload(response)}"
        )
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
    url, filepath = await _async_resolve_media_source(hass, url, filepath)

    if url:
        clean_url = url.strip()
        bypass_dl_ssl = (not verify_ssl) or is_local_url(clean_url)
        dl_session = async_get_clientsession(hass, verify_ssl=(not bypass_dl_ssl))
        ssl_ctx = False if bypass_dl_ssl else None
        try:
            async with dl_session.get(clean_url, ssl=ssl_ctx, timeout=timeout_ctx) as vid_resp:
                if vid_resp.status >= 400:
                    err_body = redact_sensitive_text((await vid_resp.text())[:200])
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
        VK_API_VIDEO_SAVE,
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


def _normalize_str_list(value: str | list[str] | None) -> list[str]:
    """Преобразует строку (или список строк) в плоский список непустых значений."""
    if not value:
        return []
    if isinstance(value, str):
        items = [x.strip() for x in re.split(r"[\n,]+", value) if x.strip()]
        return items
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for elem in value:
            if elem:
                s = str(elem).strip()
                if s:
                    result.append(s)
        return result
    return [str(value).strip()]


async def async_upload_photos(
    hass: HomeAssistant,
    access_token: str,
    peer_id: int,
    url: str | list[str] | None = None,
    filepath: str | list[str] | None = None,
    camera_entity: str | list[str] | None = None,
    verify_ssl: bool = True,
    timeout: int = 60,
) -> str:
    """Загружает от 1 до 10 фотографий (с камер HA, по URL или локальным путям) и возвращает строку attachment для ВК."""
    cameras = _normalize_str_list(camera_entity)
    urls = _normalize_str_list(url)
    files = _normalize_str_list(filepath)

    tasks: list[dict[str, str]] = []
    for cam in cameras:
        tasks.append({"camera_entity": cam})
    for u in urls:
        tasks.append({"url": u})
    for f in files:
        tasks.append({"filepath": f})

    if not tasks:
        raise HomeAssistantError(
            "Для отправки фото укажите camera_entity, url или локальный путь file."
        )

    selected_tasks = tasks[:10]
    total_tasks = len(selected_tasks)

    # Параллельный захват снимков со всех камер в момент вызова автоматизации,
    # чтобы все фото фиксировали один момент времени и не задерживали друг друга.
    prefetched_cameras: dict[int, tuple[bytes, bytes, str, str] | Exception] = {}
    cam_indices = [
        idx for idx, item in enumerate(selected_tasks) if item.get("camera_entity")
    ]
    if len(cam_indices) > 1:
        cam_results = await asyncio.gather(
            *(
                _async_capture_camera_snapshot(
                    hass,
                    selected_tasks[idx]["camera_entity"],
                    timeout,
                )
                for idx in cam_indices
            ),
            return_exceptions=True,
        )
        for idx, res in zip(cam_indices, cam_results):
            prefetched_cameras[idx] = res

    attachments: list[str] = []
    errors: list[str] = []

    for idx, item in enumerate(selected_tasks):
        if idx > 0:
            await asyncio.sleep(0.45)
        try:
            prefetched = prefetched_cameras.get(idx)
            if isinstance(prefetched, Exception):
                raise prefetched

            att = await async_upload_photo(
                hass,
                access_token,
                peer_id,
                url=item.get("url"),
                filepath=item.get("filepath"),
                camera_entity=item.get("camera_entity"),
                verify_ssl=verify_ssl,
                timeout=timeout,
                _prefetched_camera=prefetched,
            )
            attachments.append(att)
        except Exception as err:
            src_label = (
                item.get("camera_entity")
                or item.get("filepath")
                or item.get("url")
                or f"item_{idx + 1}"
            )
            if total_tasks == 1:
                raise
            _LOGGER.warning(
                "Не удалось загрузить фото %d/%d (%s), пропускаем: %s",
                idx + 1,
                total_tasks,
                redact_sensitive_text(src_label),
                redact_sensitive_text(err),
            )
            errors.append(f"{src_label}: {err}")

    if not attachments:
        raise HomeAssistantError(
            f"Не удалось загрузить ни одного фото из {total_tasks}: {'; '.join(errors)}"
        )

    return ",".join(attachments)


async def async_upload_files(
    hass: HomeAssistant,
    access_token: str,
    peer_id: int,
    filepath: str | list[str] | None = None,
    url: str | list[str] | None = None,
    verify_ssl: bool = True,
    timeout: int = 60,
) -> str:
    """Загружает от 1 до 10 документов (по локальным путям или URL) и возвращает строку attachment для ВК."""
    files = _normalize_str_list(filepath)
    urls = _normalize_str_list(url)

    tasks: list[dict[str, str]] = []
    for f in files:
        tasks.append({"filepath": f})
    for u in urls:
        tasks.append({"url": u})

    if not tasks:
        raise HomeAssistantError("Neither filepath nor URL provided for file upload.")

    attachments: list[str] = []
    for item in tasks[:10]:
        att = await async_upload_file(
            hass,
            access_token,
            peer_id,
            filepath=item.get("filepath"),
            url=item.get("url"),
            verify_ssl=verify_ssl,
            timeout=timeout,
        )
        attachments.append(att)

    return ",".join(attachments)







