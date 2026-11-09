"""
VK Notify helpers.py v1.7.0
Compatibility facade re-exporting formatting, redaction, and media upload helpers.
"""
from __future__ import annotations

from .formatting import (
    build_vk_keyboard,
    encode_utf16_len,
    parse_vk_formatting,
    redact_sensitive_text,
    redact_vk_payload,
)
from .media import (
    _detect_file_mime,
    _detect_image_mime_and_filename,
    _detect_magic_image_mime,
    _extract_filename_from_source,
    _post_multipart_to_vk,
    _read_file_safe,
    async_upload_file,
    async_upload_photo,
    async_upload_video,
    is_local_url,
)

__all__ = [
    "_detect_file_mime",
    "_detect_image_mime_and_filename",
    "_detect_magic_image_mime",
    "_extract_filename_from_source",
    "_post_multipart_to_vk",
    "_read_file_safe",
    "async_upload_file",
    "async_upload_photo",
    "async_upload_video",
    "build_vk_keyboard",
    "encode_utf16_len",
    "is_local_url",
    "parse_vk_formatting",
    "redact_sensitive_text",
    "redact_vk_payload",
]

