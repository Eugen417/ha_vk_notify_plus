"""
VK Notify api.py v1.0.0
Unified async VK API client with automatic retry/backoff on rate limits (Error 6/9/10)
and built-in sensitive data redaction.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import VK_API_VERSION, VK_RETRYABLE_ERROR_CODES
from .formatting import redact_sensitive_text, redact_vk_payload

_LOGGER = logging.getLogger(__name__)


class VkApiClient:
    """Асинхронный клиент VK API с поддержкой ретраев и маскирования секретов."""

    def __init__(
        self,
        hass: HomeAssistant,
        access_token: str,
        verify_ssl: bool = True,
    ) -> None:
        self._hass = hass
        self._access_token = access_token
        self._verify_ssl = verify_ssl

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def verify_ssl(self) -> bool:
        return self._verify_ssl

    async def async_request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        http_method: str = "POST",
        token_override: str | None = None,
        timeout: int = 60,
        max_retries: int = 3,
    ) -> Any:
        """Выполняет запрос к методу VK API с автоповтором при ошибках 6/9/10 и HTTP 429/5xx."""
        session = async_get_clientsession(self._hass, verify_ssl=self._verify_ssl)
        req_params: dict[str, Any] = dict(params or {})
        req_params.setdefault("access_token", token_override or self._access_token)
        req_params.setdefault("v", VK_API_VERSION)

        timeout_ctx = aiohttp.ClientTimeout(total=timeout)
        method_name = endpoint.rsplit("/", 1)[-1]

        for attempt in range(1, max_retries + 1):
            try:
                if http_method.upper() == "GET":
                    req_cm = session.get(endpoint, params=req_params, timeout=timeout_ctx)
                else:
                    req_cm = session.post(endpoint, data=req_params, timeout=timeout_ctx)

                async with req_cm as resp:
                    if resp.status in (429, 502, 503, 504) and attempt < max_retries:
                        delay = 0.6 * attempt
                        _LOGGER.debug(
                            "VK API %s returned HTTP %s (attempt %d/%d), retrying in %.1fs",
                            method_name,
                            resp.status,
                            attempt,
                            max_retries,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue

                    data = await resp.json(content_type=None)
            except HomeAssistantError:
                raise
            except Exception as err:
                if attempt < max_retries:
                    await asyncio.sleep(0.6 * attempt)
                    continue
                raise HomeAssistantError(
                    f"VK request error ({method_name}): {redact_sensitive_text(err)}"
                ) from err

            if not isinstance(data, dict):
                raise HomeAssistantError(
                    f"Unexpected VK API response ({method_name}): {redact_sensitive_text(data)}"
                )

            if "error" in data:
                err_obj = data["error"]
                err_code = err_obj.get("error_code") if isinstance(err_obj, dict) else None
                if err_code in VK_RETRYABLE_ERROR_CODES and attempt < max_retries:
                    delay = 0.7 * attempt
                    _LOGGER.debug(
                        "VK API %s rate/server limit (error_code=%s, attempt %d/%d), retrying in %.1fs",
                        method_name,
                        err_code,
                        attempt,
                        max_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                raise HomeAssistantError(
                    f"VK API Error ({method_name}): {redact_vk_payload(err_obj)}"
                )

            return data.get("response")

        raise HomeAssistantError(f"VK API request failed after {max_retries} attempts: {method_name}")
