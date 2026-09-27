import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qqbot_hk_media_test", ROOT / "plugins" / "smart_group_qq" / "media.py"
)
MEDIA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEDIA)


def image(name):
    return {"type": "image_url", "image_url": {"url": name}}


class QQMediaTests(unittest.IsolatedAsyncioTestCase):
    def test_five_image_budget_spans_user_tool_and_api_content(self):
        messages = [
            {"role": "user", "content": [image("old-user-1"), image("old-user-2")]},
            {"role": "tool", "content": [image("old-tool")]},
            {"role": "assistant", "api_content": [image("api-1"), image("api-2"), image("api-3")]},
            {"role": "user", "content": [image("new-1"), image("new-2")]},
        ]
        result = MEDIA.trim_images(messages)
        self.assertIn("api_content", messages[2])
        effective = [message.get("api_content", message["content"]) for message in messages]
        retained = [part["image_url"]["url"] for content in effective
                    for part in content if part.get("type") == "image_url"]
        self.assertEqual(retained, ["api-1", "api-2", "api-3", "new-1", "new-2"])
        retired = [part["text"] for message in messages
                   for content in (message["content"], message.get("api_content", []))
                   if isinstance(content, list) for part in content
                   if part.get("type") == "text"]
        self.assertEqual(retired, ["[较早图片已从上下文清理]"] * 3)

    def test_api_sidecar_text_does_not_replace_persisted_message_content(self):
        original = [{"type": "text", "text": "durable user text"}]
        sidecar = [{"type": "text", "text": "ephemeral injected context"}]
        messages = [{"role": "user", "content": original, "api_content": sidecar}]
        MEDIA.trim_images(messages)
        self.assertEqual(messages[0]["content"], original)
        self.assertEqual(messages[0]["api_content"], sidecar)

    def test_five_attachment_limit_keeps_latest_images_and_nonimages(self):
        attachments = [{"id": f"image-{index}", "content_type": "image/png"}
                       for index in range(7)] + [{"id": "file", "content_type": "application/pdf"}]
        result = MEDIA.limit_attachments(attachments)
        self.assertEqual([item["id"] for item in result],
                         ["image-2", "image-3", "image-4", "image-5", "image-6", "file"])

    def test_payload_image_budget_is_shared_with_direct_images_first_without_mutation(self):
        payload = {
            "message_type": "103",
            "attachments": [
                {"id": f"direct-{index}", "content_type": "image/png"}
                for index in range(3)
            ],
            "msg_elements": [
                {"attachments": [
                    {"id": f"quoted-{index}", "content_type": "image/png"}
                    for index in range(4)
                ]}
            ],
        }
        original = {
            "message_type": payload["message_type"],
            "attachments": [dict(item) for item in payload["attachments"]],
            "msg_elements": [{"attachments": [dict(item) for item in
                              payload["msg_elements"][0]["attachments"]]}],
        }

        result = MEDIA.limit_payload_images(payload)

        direct = [item["id"] for item in result["attachments"]]
        quoted = [item["id"] for item in result["msg_elements"][0]["attachments"]]
        self.assertEqual(direct + quoted,
                         ["direct-0", "direct-1", "direct-2", "quoted-2", "quoted-3"])
        self.assertEqual(sum(item["content_type"].startswith("image/")
                             for item in result["attachments"] +
                             result["msg_elements"][0]["attachments"]), 5)
        self.assertEqual(payload, original)

    async def test_manual_image_inputs_are_cache_scoped_bounded_and_limited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cache"
            root.mkdir()
            paths = []
            for index in range(6):
                path = root / f"{index}.png"
                path.write_bytes(bytes([index + 1]))
                paths.append(str(path))
            external = Path(tmp) / "outside.png"
            external.write_bytes(b"outside")
            loaded = await MEDIA.image_inputs(paths, [root])
            self.assertEqual([item["file_name"] for item in loaded],
                             [f"{index}.png" for index in range(1, 6)])
            self.assertEqual([item["data"] for item in loaded],
                             [bytes([index + 1]) for index in range(1, 6)])
            outside = await MEDIA.image_inputs([str(external)], [root])
            self.assertEqual(outside, [])

    async def test_image_cache_rejects_oversized_symlink_and_nonimage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cache"
            root.mkdir()
            oversized = root / "large.png"
            oversized.write_bytes(b"x" * (MEDIA.MAX_IMAGE_BYTES + 1))
            text = root / "note.txt"
            text.write_text("not image", encoding="utf-8")
            external = Path(tmp) / "outside.png"
            external.write_bytes(b"outside")
            link = root / "escape.png"
            try:
                link.symlink_to(external)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable on this platform")
            result = await MEDIA.image_inputs(
                [str(oversized), str(text), str(link), str(external)], [root])
            self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
