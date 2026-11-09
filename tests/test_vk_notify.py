"""Unit tests for VK Notify v1.7.0 (formatting, redaction, MIME detection, LongPoll attachments)."""
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
        mock_aiohttp = types.ModuleType("aiohttp")
        mock_aiohttp.ClientSession = object
        mock_aiohttp.FormData = object
        mock_aiohttp.ClientTimeout = object
        sys.modules.setdefault("aiohttp", mock_aiohttp)

        cls.fmt = _load_module_ast("formatting.py")
        cls.media = _load_module_ast(
            "media.py",
            extra_globals={
                "redact_sensitive_text": cls.fmt["redact_sensitive_text"],
                "redact_vk_payload": cls.fmt["redact_vk_payload"],
            },
        )
        cls.lp = _load_module_ast(
            "longpoll.py",
            extra_globals={
                "redact_sensitive_text": cls.fmt["redact_sensitive_text"],
                "redact_vk_payload": cls.fmt["redact_vk_payload"],
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


if __name__ == "__main__":
    unittest.main()

