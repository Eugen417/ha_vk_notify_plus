"""Unit tests for VK Notify v1.0.0 (formatting, redaction, MIME detection, LongPoll attachments)."""
from __future__ import annotations

import ast
import json
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "custom_components" / "vk_notify"


def _load_module_ast(filename: str, extra_globals: dict | None = None) -> dict:
    source = (PKG_DIR / filename).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=filename)
    allowed_imports = {"__future__", "typing", "urllib.parse", "collections.abc"}
    body = [
        node
        for node in tree.body
        if not isinstance(node, ast.ImportFrom) or node.module in allowed_imports
    ]
    ns: dict = dict(extra_globals or {})
    exec(compile(ast.Module(body=body, type_ignores=[]), filename, "exec"), ns)
    return ns


class TestVkNotify(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        class _FakeClientError(Exception):
            pass

        class _FakeFormData:
            def __init__(self) -> None:
                self.fields: list[tuple] = []

            def add_field(self, name, value, filename=None, content_type=None) -> None:
                self.fields.append((name, value, filename, content_type))

        mock_aiohttp = types.ModuleType("aiohttp")
        mock_aiohttp.ClientSession = object
        mock_aiohttp.FormData = _FakeFormData
        mock_aiohttp.ClientTimeout = lambda **kw: kw
        mock_aiohttp.ClientError = _FakeClientError
        sys.modules.setdefault("aiohttp", mock_aiohttp)

        cls.const = _load_module_ast("const.py")
        cls.fmt = _load_module_ast("formatting.py")
        cls.media = _load_module_ast(
            "media.py",
            extra_globals={
                **cls.const,
                "HomeAssistantError": RuntimeError,
                "redact_sensitive_text": cls.fmt["redact_sensitive_text"],
                "redact_vk_payload": cls.fmt["redact_vk_payload"],
            },
        )
        cls.lp = _load_module_ast(
            "longpoll.py",
            extra_globals={
                **cls.const,
                "ConfigEntry": object,
                "HomeAssistant": object,
                "redact_sensitive_text": cls.fmt["redact_sensitive_text"],
                "redact_vk_payload": cls.fmt["redact_vk_payload"],
            },
        )
        cls.diag = _load_module_ast(
            "diagnostics.py",
            extra_globals={
                **cls.const,
                "ConfigEntry": object,
                "HomeAssistant": object,
                "async_redact_data": lambda d, keys: {
                    k: ("**REDACTED**" if k in keys else v) for k, v in d.items()
                },
            },
        )

    def test_parse_vk_formatting_html_and_markdown(self) -> None:
        parse_fn = self.fmt["parse_vk_formatting"]
        clean, fmt_json = parse_fn("Привет <b>мир</b> и [ссылка](https://ha.io)!", "html")
        self.assertEqual(clean, "Привет мир и ссылка!")
        self.assertIsNotNone(fmt_json)
        data = json.loads(fmt_json)
        self.assertEqual(data["version"], 1)
        types_found = {item["type"] for item in data["items"]}
        self.assertEqual(types_found, {"bold", "url"})

        plain_text, plain_fmt = parse_fn("<b>Без парсинга</b>", "plain")
        self.assertEqual(plain_text, "<b>Без парсинга</b>")
        self.assertIsNone(plain_fmt)

    def test_build_vk_keyboard_no_mutation(self) -> None:
        build_kb = self.fmt["build_vk_keyboard"]
        original = {
            "buttons": [
                [
                    {
                        "action": {
                            "type": "callback",
                            "label": "Тест",
                            "payload": '{"cmd": "on"}',
                        }
                    }
                ]
            ]
        }
        result_str = build_kb(original, inline_keyboard=True, auto_answer_callback=True)
        result = json.loads(result_str)
        self.assertTrue(result["inline"])
        payload_obj = json.loads(result["buttons"][0][0]["action"]["payload"])
        self.assertTrue(payload_obj["_ha_auto"])
        self.assertEqual(original["buttons"][0][0]["action"]["payload"], '{"cmd": "on"}')

    def test_redaction_of_tokens_urls_and_payloads(self) -> None:
        rt = self.fmt["redact_sensitive_text"]
        rp = self.fmt["redact_vk_payload"]

        raw_url = (
            "http://admin:SuperSecret123@192.168.1.50/snap.jpg"
            "?token=camToken999&access_token=vk1.a.MySecretVkToken123456789"
        )
        cleaned = rt(raw_url)
        self.assertNotIn("SuperSecret123", cleaned)
        self.assertNotIn("camToken999", cleaned)
        self.assertNotIn("MySecretVkToken123456789", cleaned)
        self.assertIn("***REDACTED***", cleaned)

        payload = {
            "server": 858116,
            "photo": "",
            "hash": "a1b2c3d4e5",
            "peer_id": 2000000001,
            "request_params": [
                {"key": "peer_id", "value": "123456789"},
                {"key": "access_token", "value": "secret"},
            ],
        }
        redacted = rp(payload)
        self.assertEqual(redacted["hash"], "***REDACTED***")
        self.assertEqual(redacted["peer_id"], "20***01")
        self.assertEqual(redacted["request_params"][0]["value"], "12***89")
        self.assertEqual(redacted["request_params"][1]["value"], "***REDACTED***")

    def test_image_mime_and_filename_detection(self) -> None:
        detect = self.media["_detect_image_mime_and_filename"]

        self.assertEqual(
            detect(b"\xff\xd8\xff\xe0test", "/config/www/vk_test.jpg"),
            ("image/jpeg", "vk_test.jpg"),
        )
        self.assertEqual(
            detect(
                b"\x89PNG\r\n\x1a\ntest",
                "http://192.168.1.10:5000/api/events/123/snapshot.jpg?bbox=1",
            ),
            ("image/png", "snapshot.png"),
        )
        self.assertEqual(
            detect(
                b"RIFF\x00\x00\x00\x00WEBPVP8 ",
                "http://192.168.1.10:5000/api/cam/latest.webp",
            ),
            ("image/webp", "latest.webp"),
        )
        self.assertEqual(
            detect(b"GIF89atest", "/tmp/anim.gif"),
            ("image/gif", "anim.gif"),
        )
        self.assertEqual(
            detect(b"\xff\xd8\xff\xe0", "http://192.168.1.10:5000/api/events/123/thumbnail"),
            ("image/jpeg", "thumbnail.jpg"),
        )

    def test_extract_vk_attachments_and_voice_transcript(self) -> None:
        extract = self.lp["extract_vk_attachments"]
        raw_atts = [
            {
                "type": "photo",
                "photo": {
                    "id": 10,
                    "owner_id": -100,
                    "text": "cam",
                    "sizes": [
                        {"width": 130, "height": 100, "url": "https://vk.com/small.jpg"},
                        {"width": 1280, "height": 720, "url": "https://vk.com/hd.jpg"},
                    ],
                },
            },
            {
                "type": "audio_message",
                "audio_message": {
                    "id": 20,
                    "owner_id": 123,
                    "duration": 4,
                    "link_ogg": "https://vk.com/voice.ogg",
                    "link_mp3": "https://vk.com/voice.mp3",
                    "transcript": "включи свет в гостиной",
                    "transcript_state": "done",
                },
            },
        ]
        parsed, transcript = extract(raw_atts)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["url"], "https://vk.com/hd.jpg")
        self.assertEqual(transcript, "включи свет в гостиной")

    def test_normalize_str_list_for_multi_upload(self) -> None:
        norm = self.media["_normalize_str_list"]
        self.assertEqual(norm(None), [])
        self.assertEqual(norm("/config/www/1.jpg"), ["/config/www/1.jpg"])
        self.assertEqual(
            norm("/config/www/1.jpg, /config/www/2.jpg\n/config/www/3.jpg"),
            ["/config/www/1.jpg", "/config/www/2.jpg", "/config/www/3.jpg"],
        )
        self.assertEqual(
            norm(["camera.front", "camera.back"]),
            ["camera.front", "camera.back"],
        )

    def test_diagnostics_excludes_personal_names_and_raw_entity_ids(self) -> None:
        import asyncio

        sub = types.SimpleNamespace(
            subentry_id="sub_1",
            subentry_type="chat",
            title="Ivan Petrov (123456789)",
            data={"peer_id": 123456789, "name": "Ivan"},
        )
        entry = types.SimpleNamespace(
            entry_id="entry_1",
            version=1,
            data={
                "access_token": "vk1.a.secret",
                "peer_id": 123456789,
                "group_id": 987654321,
                "mode": "longpoll",
                "name": "Ivan Petrov",
                "verify_ssl": True,
            },
            options={},
            subentries={"sub_1": sub},
        )
        ent = types.SimpleNamespace(
            entity_id="notify.vk_notify_123456789",
            unique_id="entry_1_123456789",
            config_subentry_id="sub_1",
            disabled=False,
        )
        hass = types.SimpleNamespace(
            data={},
            states=types.SimpleNamespace(
                get=lambda _eid: types.SimpleNamespace(
                    state="2026-11-16T00:00:00+00:00",
                    attributes={"last_message_id": 10, "last_cmid": 5},
                )
            ),
        )
        self.diag["er"] = types.SimpleNamespace(
            async_get=lambda _h: "reg",
            async_entries_for_config_entry=lambda _r, _eid: [ent],
        )

        result = asyncio.run(self.diag["async_get_config_entry_diagnostics"](hass, entry))
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("Ivan", serialized)
        self.assertNotIn("Petrov", serialized)
        self.assertNotIn("123456789", serialized)
        self.assertNotIn("987654321", serialized)
        self.assertNotIn("notify.vk_notify_123456789", serialized)
        self.assertEqual(result["config_entry"]["subentries"][0]["subentry_type"], "chat")
        self.assertNotIn("title", result["config_entry"]["subentries"][0])
        self.assertEqual(result["entities"][0]["index"], 1)
        self.assertEqual(result["entities"][0]["masked_peer_id"], "12***89")

    def test_longpoll_auth_error_stops_and_triggers_reauth(self) -> None:
        import asyncio

        reauth_called = []
        entry = types.SimpleNamespace(
            entry_id="entry_lp",
            data={"group_id": 555},
            async_start_reauth=lambda h: reauth_called.append(h),
        )
        hass = types.SimpleNamespace(
            config_entries=types.SimpleNamespace(async_entries=lambda _d: [entry])
        )

        class FakeResp:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self, content_type=None):
                return {"error": {"error_code": 5, "error_msg": "User authorization failed"}}

        class FakeSession:
            def get(self, *args, **kwargs):
                return FakeResp()

        self.lp["async_get_clientsession"] = lambda _h, verify_ssl=True: FakeSession()
        sleeps = []

        async def fake_sleep(sec):
            sleeps.append(sec)

        orig_sleep = self.lp["asyncio"].sleep
        self.lp["asyncio"].sleep = fake_sleep
        try:
            mgr = self.lp["VkLongPollManager"](hass, "revoked_tok", 555, entry=entry)
            asyncio.run(mgr._run())
        finally:
            self.lp["asyncio"].sleep = orig_sleep

        self.assertEqual(len(reauth_called), 1)
        self.assertEqual(sleeps, [])

    def test_longpoll_network_error_exponential_backoff(self) -> None:
        import asyncio

        calls = 0

        class FakeSession:
            def get(self, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls <= 3:
                    raise OSError("temporary network outage")
                raise asyncio.CancelledError

        self.lp["async_get_clientsession"] = lambda _h, verify_ssl=True: FakeSession()
        sleeps = []

        async def fake_sleep(sec):
            sleeps.append(sec)

        orig_sleep = self.lp["asyncio"].sleep
        self.lp["asyncio"].sleep = fake_sleep
        try:
            mgr = self.lp["VkLongPollManager"](object(), "tok", 555)
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(mgr._run())
        finally:
            self.lp["asyncio"].sleep = orig_sleep

        self.assertEqual(sleeps, [5.0, 10.0, 20.0])

    def test_photo_upload_retries_http_500_and_empty_photo_across_nodes(self) -> None:
        import asyncio

        jpeg_bytes = b"\xff\xd8\xff\xe0" + (b"\x11" * 1024)
        upload_posts: list[str] = []
        get_server_params: list[dict] = []

        class FakeUploadResp:
            def __init__(self, status: int, body: str) -> None:
                self.status = status
                self._body = body
                self.headers = {"Content-Type": "application/json"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def text(self) -> str:
                return self._body

        class FakeUploadSession:
            def post(self, url, data=None, timeout=None, allow_redirects=False):
                upload_posts.append(url)
                idx = len(upload_posts)
                if idx == 1:
                    return FakeUploadResp(500, "<html>Temporarily unavailable</html>")
                if idx == 2:
                    return FakeUploadResp(
                        200, '{"server": 844529, "photo": "", "hash": "sec"}'
                    )
                return FakeUploadResp(
                    200,
                    '{"server": 231331, "photo": "[{\\"photo\\":\\"ok\\"}]", "hash": "good"}',
                )

        class FakeVkClient:
            def __init__(self, _hass, _token, verify_ssl=True) -> None:
                pass

            async def async_request(self, endpoint, params=None, **kwargs):
                if endpoint.endswith("photos.getMessagesUploadServer"):
                    get_server_params.append(dict(params or {}))
                    n = len(get_server_params)
                    return {"upload_url": f"https://pu.vk.com/c{n}/upload.php"}
                if endpoint.endswith("photos.saveMessagesPhoto"):
                    return [{"owner_id": -100, "id": 777, "access_key": "ak"}]
                raise AssertionError(f"Unexpected endpoint: {endpoint}")

        class FakeHass:
            config = types.SimpleNamespace(is_allowed_path=lambda _p: True)

            async def async_add_executor_job(self, func, *args):
                if func is self_outer.media["_read_file_safe"]:
                    return jpeg_bytes
                return func(*args)

        self_outer = self
        self.media["async_get_clientsession"] = lambda _h, verify_ssl=True: FakeUploadSession()
        self.media["VkApiClient"] = FakeVkClient

        async def fast_sleep(_sec):
            return None

        orig_sleep = self.media["asyncio"].sleep
        self.media["asyncio"].sleep = fast_sleep
        try:
            att = asyncio.run(
                self.media["async_upload_photo"](
                    FakeHass(),
                    "vk1.a.test",
                    2000000003,
                    filepath="/config/www/cam3.jpg",
                )
            )
        finally:
            self.media["asyncio"].sleep = orig_sleep

        self.assertEqual(att, "photo-100_777_ak")
        self.assertEqual(len(upload_posts), 3)
        self.assertEqual(
            get_server_params,
            [{"peer_id": 2000000003}, {}, {"peer_id": 2000000003}],
        )

    def test_upload_photos_prefetches_all_three_cameras_and_delivers_all(self) -> None:
        import asyncio

        events: list[str] = []

        async def fake_capture(_hass, cam_entity, _timeout):
            events.append(f"capture:{cam_entity}")
            raw = b"\xff\xd8\xff\xe0" + cam_entity.encode("utf-8") + (b"\x00" * 600)
            return raw, raw, "image/jpeg", "image.jpg"

        async def fake_upload_photo(
            _hass,
            _token,
            _peer_id,
            url=None,
            filepath=None,
            camera_entity=None,
            verify_ssl=True,
            timeout=60,
            *,
            _prefetched_camera=None,
        ):
            self.assertIsNotNone(_prefetched_camera)
            events.append(f"upload:{camera_entity}")
            idx = camera_entity.rsplit("_", 1)[-1]
            return f"photo-1_{idx}"

        orig_cap = self.media["_async_capture_camera_snapshot"]
        orig_up = self.media["async_upload_photo"]
        orig_sleep = self.media["asyncio"].sleep

        async def fast_sleep(_sec):
            return None

        self.media["_async_capture_camera_snapshot"] = fake_capture
        self.media["async_upload_photo"] = fake_upload_photo
        self.media["asyncio"].sleep = fast_sleep
        try:
            res = asyncio.run(
                self.media["async_upload_photos"](
                    object(),
                    "tok",
                    2000000003,
                    camera_entity=[
                        "camera.cam_1",
                        "camera.cam_2",
                        "camera.cam_3",
                    ],
                )
            )
        finally:
            self.media["_async_capture_camera_snapshot"] = orig_cap
            self.media["async_upload_photo"] = orig_up
            self.media["asyncio"].sleep = orig_sleep

        self.assertEqual(res, "photo-1_1,photo-1_2,photo-1_3")
        # Все 3 камеры захватываются параллельно ДО старта загрузок в ВК
        self.assertEqual(
            events[:3],
            ["capture:camera.cam_1", "capture:camera.cam_2", "capture:camera.cam_3"],
        )
        self.assertEqual(
            events[3:],
            ["upload:camera.cam_1", "upload:camera.cam_2", "upload:camera.cam_3"],
        )


if __name__ == "__main__":
    unittest.main()

