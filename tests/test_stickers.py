import asyncio
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image, PngImagePlugin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
from smart_group_qq import build_handler
from smart_group_qq.character import ResidentCharacter
from smart_group_qq.expressions import STICKER_CATEGORIES
from smart_group_qq.qq_observer import _observe_message
from smart_group_qq.response import ReplyRequest
from smart_group_qq.stickers import StickerService, normalize_sticker, reply_category
from smart_group_qq.store import SCHEMA_VERSION, Store


def png(color="green", *, metadata=None):
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text("description", metadata)
    Image.new("RGBA", (128, 128), color).save(output, format="PNG", pnginfo=info)
    return output.getvalue()


def classification(**overrides):
    return {"is_sticker": True, "safe": True, "confidence": 0.98,
            "category": "开心", "caption": "开心的卡通反应", **overrides}


def add_sticker(store, group="g", member="a", color="green", category="开心", **kwargs):
    profile = store.upsert_group_member(group, member)
    return store.add_group_sticker(
        group, member, png(color), "image/png", category, "安全卡通反应",
        expected_epoch=store.memory_epoch(group), expected_profile_version=profile["profile_version"], **kwargs,
    )


class Context:
    def __init__(self, directory):
        self.settings = {
            "stickers": {"enabled": True, "auto_collect": True, "auto_send": True,
                         "send_cache_dir": str(directory / "sending")},
            "media_cache_roots": [str(directory)],
            "ambient": {"enabled": True, "participation": {"enabled": False}},
        }
        self.llm = SimpleNamespace(acomplete_structured=AsyncMock(return_value=SimpleNamespace(parsed=classification())))

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


class Adapter:
    def __init__(self):
        self.allowed = True
        self.success = True
        self.texts, self.images, self.dispatches = [], [], []
        self._ensure_connected = AsyncMock(return_value=True)
        self._process_attachments = AsyncMock()

    def _is_group_allowed(self, group, member):
        return self.allowed

    async def send(self, chat_id, content, reply_to=None):
        self.texts.append((chat_id, content, reply_to))
        return SimpleNamespace(success=self.success, message_id="reply-" + str(reply_to))

    async def send_image_file(self, chat_id, image_path, reply_to=None):
        self.images.append((chat_id, Path(image_path).read_bytes(), reply_to, image_path))
        return SimpleNamespace(success=self.success)

    async def _on_message(self, event, payload):
        self.dispatches.append((event, payload))


def event(text, message_id, *, group="g", member="a", paths=(), private=False, synthetic=False):
    return SimpleNamespace(
        text=text, message_id=message_id, media_urls=list(paths), timestamp=time.time(),
        raw_message={"_smart_group_qq_nonmention": True} if synthetic else {},
        source=SimpleNamespace(platform="qqbot", chat_type="dm" if private else "group", chat_id=group, user_id=member),
    )


class ImageTests(unittest.TestCase):
    def test_reencoding_removes_metadata_and_preserves_all_animation_frames(self):
        data, mime, preview = normalize_sticker({"data": png(metadata="private-metadata-sentinel")})
        self.assertEqual(mime, "image/png")
        self.assertNotIn(b"private-metadata-sentinel", data)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, (128, 128))
            self.assertEqual(image.info, {})
        output = io.BytesIO()
        Image.new("RGBA", (128, 128), "red").save(
            output, format="GIF", save_all=True, append_images=[Image.new("RGBA", (128, 128), "blue")], duration=[100, 200], loop=0,
        )
        data, mime, preview = normalize_sticker({"data": output.getvalue()})
        self.assertEqual(mime, "image/gif")
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.n_frames, 2)
        with Image.open(io.BytesIO(preview["data"])) as image:
            self.assertEqual(image.size, (320, 160))
            self.assertEqual(image.getpixel((50, 50)), (255, 0, 0))
            self.assertEqual(image.getpixel((210, 50)), (0, 0, 255))

    def test_invalid_and_oversized_inputs_are_rejected(self):
        for data in (b"not an image", b"", b"x" * (4 * 1024 * 1024 + 1)):
            self.assertIsNone(normalize_sticker({"data": data}))
        output = io.BytesIO()
        Image.new("RGB", (2049, 32)).save(output, format="PNG")
        self.assertIsNone(normalize_sticker({"data": output.getvalue()}))
        output = io.BytesIO()
        frames = [Image.new("RGB", (32, 32), (i, 0, 0)) for i in range(31)]
        frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:])
        self.assertIsNone(normalize_sticker({"data": output.getvalue()}))
        data = png()
        with patch.dict(sys.modules, {"PIL": None}):
            self.assertIsNone(normalize_sticker({"data": data}))

    def test_reactions_are_relevant_not_used_for_serious_long_or_optout_replies(self):
        self.assertEqual(reply_category("好消息", "恭喜，太棒了！"), "庆祝")
        self.assertEqual(reply_category("有点丧", "别灰心，加油！"), "鼓励")
        for question, reply in (("请部署代码", "哈哈"), ("不要表情包", "哈哈"),
                                ("", "哈哈" * 100), ("", "平常的一句话"), ("", "")):
            self.assertIsNone(reply_category(question, reply))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(member_secret="test-only-secret")
        self.addCleanup(self.store.close)

    def test_deduplication_scope_ownership_optout_and_forget(self):
        self.assertTrue(add_sticker(self.store))
        self.assertTrue(add_sticker(self.store, member="b"))
        self.assertEqual(self.store.list_group_stickers("g"), [{"category": "开心", "count": 1}])
        self.assertEqual(self.store.list_group_stickers("other"), [])
        sources = self.store.db.execute("SELECT member_ref FROM group_sticker_sources").fetchall()
        self.assertTrue(all(row[0].startswith("m-") for row in sources))
        self.store.forget_group_member("g", "a")
        self.assertEqual(self.store.list_group_stickers("g")[0]["count"], 1)
        self.store.set_member_consent("g", "b", "opted_out")
        self.assertEqual(self.store.list_group_stickers("g"), [])
        profile = self.store.get_group_member("g", member_ref=self.store.member_ref_for("g", "b"))
        self.assertFalse(self.store.add_group_sticker("g", "b", png(), "image/png", "开心", "卡通",
                         expected_epoch=self.store.memory_epoch("g"), expected_profile_version=profile["profile_version"]))

    def test_capacity_expiry_reset_preservation_and_rotation(self):
        for color in ("red", "green", "blue"):
            add_sticker(self.store, color=color, max_per_group=2)
        self.assertEqual(self.store.list_group_stickers("g")[0]["count"], 2)
        self.store.clear_group("g")
        first = self.store.reserve_group_sticker("g", "开心", expected_epoch=self.store.memory_epoch("g"))
        second = self.store.reserve_group_sticker("g", "开心", expected_epoch=self.store.memory_epoch("g"))
        self.assertNotEqual(first["digest"], second["digest"])
        self.assertIsNone(self.store.reserve_group_sticker("g", "开心", expected_epoch=self.store.memory_epoch("g"), cooldown_seconds=60))
        add_sticker(self.store, group="other", max_total=1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM group_stickers").fetchone()[0], 1)
        with self.store.transaction() as db:
            db.execute("UPDATE group_stickers SET expires=0")
        self.assertEqual(self.store.purge_group_stickers(), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM group_sticker_sources").fetchone()[0], 0)

    def test_schema5_migrates_without_losing_member_goals_history_or_polls(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "data.db"
            store = Store(path, member_secret="test-only-secret")
            character = ResidentCharacter(store)
            character.command("g", "a", "/目标 保留用户待办", "goal")
            store.append_history("g", role="user", text="历史记录", member_id="synthetic-ref", message_id="history")
            store.apply_group_poll("g", "a", "create", question="保留投票", options=["甲", "乙"])
            with store.transaction() as db:
                for table in ("group_sticker_sources", "group_sticker_delivery", "group_stickers"):
                    db.execute("DROP TABLE " + table)
                character._add(db, "g", "discovery", "", "旧推送", "https://example.com/release")
                character._add(db, "g", "goal", "", "发现值得一起玩的开源 AI 项目，关注 AIRI、Mindcraft 和 SillyTavern 的新发布")
                db.execute("INSERT OR REPLACE INTO character_state VALUES(?,?)", ("g", json.dumps({"proactive": True, "next_tick": 123, "failures": 2, "platform_blocked": True, "mode": "quiet", "story": {"premise": "保留剧情"}})))
                db.execute("PRAGMA user_version=5")
            store.close()
            for _ in range(2):
                reopened = Store(path, member_secret="test-only-secret")
                try:
                    self.assertEqual(reopened.db.execute("PRAGMA user_version").fetchone()[0], 6)
                    self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM character_items WHERE kind='discovery'").fetchone()[0], 0)
                    self.assertEqual(reopened.db.execute("SELECT text FROM character_items WHERE kind='goal'").fetchone()[0], "保留用户待办")
                    self.assertEqual(reopened.get_history("g")[0]["text"], "历史记录")
                    self.assertEqual(reopened.apply_group_poll("g", "a", "show")["question"], "保留投票")
                    state = json.loads(reopened.db.execute("SELECT payload FROM character_state WHERE scope='g'").fetchone()[0])
                    self.assertEqual(state, {"mode": "quiet", "story": {"premise": "保留剧情"}})
                finally:
                    reopened.close()
            with closing(sqlite3.connect(path)) as db:
                db.execute("PRAGMA user_version=7")
                db.commit()
            with self.assertRaisesRegex(RuntimeError, "newer"):
                Store(path, member_secret="test-only-secret")

    def test_platform_block_survives_service_recreation_and_purge(self):
        add_sticker(self.store)
        ctx = SimpleNamespace()
        service = StickerService(ctx, self.store)
        service.platform_event("g", "GROUP_MSG_REJECT")
        restarted = StickerService(ctx, self.store)
        self.assertTrue(restarted.store.sticker_platform_blocked("g"))
        self.store.purge_group_stickers()
        self.assertTrue(self.store.sticker_platform_blocked("g"))
        restarted.platform_event("g", "GROUP_MSG_RECEIVE")
        self.assertFalse(self.store.sticker_platform_blocked("g"))


class StickerFixture:
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.image = self.root / "incoming.png"
        self.image.write_bytes(png())
        self.store = Store(member_secret="test-only-secret")
        self.addCleanup(self.store.close)
        self.store.upsert_group_member("g", "a")
        self.ctx = Context(self.root)
        self.service = StickerService(self.ctx, self.store, self.ctx.settings["stickers"], roots=[self.root])
        self.adapter = Adapter()

    async def asyncTearDown(self):
        tasks = list(self.service.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def drain(self):
        if self.service.tasks:
            await asyncio.gather(*list(self.service.tasks))
        await asyncio.sleep(0)

    def collect(self, message="collect", **kwargs):
        return self.service.queue_collect("g", "a", message, allowed=lambda: self.adapter.allowed, paths=[self.image], **kwargs)


class ServiceTests(StickerFixture, unittest.IsolatedAsyncioTestCase):
    async def test_collect_once_classify_cache_link_sources_and_no_url_fetch(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("no arbitrary downloads")):
            self.assertTrue(self.collect())
            await self.drain()
            self.assertFalse(self.collect())
            self.store.upsert_group_member("g", "b")
            self.assertTrue(self.service.queue_collect("g", "b", "another-share", allowed=lambda: True, paths=[self.image]))
            await self.drain()
        self.assertEqual(self.ctx.llm.acomplete_structured.await_count, 1)
        self.assertEqual(self.store.list_group_stickers("g"), [{"category": "开心", "count": 1}])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM group_sticker_sources").fetchone()[0], 2)
        self.assertEqual(self.adapter.images, [])
        self.assertNotIn("开心的卡通反应", json.dumps([dict(row) for row in self.store.db.execute("SELECT * FROM audit_events")], ensure_ascii=False))

    async def test_screening_refuses_unsafe_non_sticker_uncertain_and_invalid_outputs(self):
        outputs = [classification(safe=False), classification(is_sticker=False), classification(confidence=0.8),
                   classification(confidence=float("nan")), classification(confidence=True), classification(category="越界"),
                   classification(caption="password=not-a-real-secret"), classification(caption=""), classification(url="https://example.com")]
        for index, parsed in enumerate(outputs):
            self.ctx.llm.acomplete_structured.return_value = SimpleNamespace(parsed=parsed)
            self.assertTrue(self.collect("reject-" + str(index)))
            await self.drain()
        self.assertEqual(self.store.list_group_stickers("g"), [])

    async def test_denied_private_missing_id_optout_old_or_no_collection_requests_do_not_call_model(self):
        self.adapter.allowed = False
        self.assertFalse(self.collect())
        self.adapter.allowed = True
        self.assertFalse(self.collect(created_at=time.time() - 121))
        self.assertFalse(self.collect(message_text="别收藏这张图片"))
        self.assertFalse(self.service.queue_collect("dm:g", "a", "private", allowed=lambda: True, paths=[self.image]))
        self.assertFalse(self.collect(""))
        self.store.set_member_consent("g", "a", "opted_out")
        self.assertFalse(self.collect())
        self.ctx.llm.acomplete_structured.assert_not_awaited()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM message_claims").fetchone()[0], 0)

    async def test_outside_root_and_url_paths_are_never_read_or_classified(self):
        with tempfile.TemporaryDirectory() as folder:
            outside = Path(folder) / "outside.png"
            outside.write_bytes(png())
            self.assertTrue(self.service.queue_collect("g", "a", "outside", allowed=lambda: True,
                                                     paths=[outside, "https://example.com/a.png"]))
            await self.drain()
        self.ctx.llm.acomplete_structured.assert_not_awaited()
        self.assertEqual(self.store.list_group_stickers("g"), [])

    async def test_epoch_consent_and_acl_are_rechecked_after_classification(self):
        for operation in ("forget", "optout", "reset", "acl"):
            scope = "scope-" + operation
            self.store.upsert_group_member(scope, "a")
            self.adapter.allowed = True
            async def complete(**kwargs):
                if operation == "forget":
                    self.store.forget_group_member(scope, "a")
                elif operation == "optout":
                    self.store.set_member_consent(scope, "a", "opted_out")
                elif operation == "reset":
                    self.store.clear_group(scope)
                else:
                    self.adapter.allowed = False
                return SimpleNamespace(parsed=classification())
            self.ctx.llm.acomplete_structured.side_effect = complete
            self.assertTrue(self.service.queue_collect(scope, "a", operation, paths=[self.image], allowed=lambda: self.adapter.allowed))
            await self.drain()
            self.assertEqual(self.store.list_group_stickers(scope), [])

    async def test_queue_is_bounded_and_concurrency_is_two(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        count = 0
        async def complete(**kwargs):
            nonlocal count
            count += 1
            if count == 2:
                entered.set()
            await release.wait()
            return SimpleNamespace(parsed=classification())
        self.ctx.llm.acomplete_structured.side_effect = complete
        for index in range(20):
            self.assertTrue(self.collect(str(index)))
        self.assertFalse(self.collect("overflow"))
        await asyncio.wait_for(entered.wait(), 3)
        self.assertEqual(count, 2)
        release.set()
        await self.drain()
        self.assertEqual(self.ctx.llm.acomplete_structured.await_count, 2)

    async def test_manual_send_uses_stored_file_and_cleans_it_even_on_ambiguous_failure(self):
        add_sticker(self.store)
        self.adapter.send_image_file = AsyncMock(side_effect=RuntimeError("ambiguous"))
        self.assertFalse(await self.service.send_category("g", "开心", self.adapter, reply_to="anchor", allowed=lambda: True, expected_epoch=0))
        self.adapter.send_image_file.assert_awaited_once()
        self.assertEqual(list((self.root / "sending").iterdir()), [])
        self.adapter.send_image_file = AsyncMock(return_value=SimpleNamespace(success=True))
        self.assertIsNone(await self.service.send_category("g", "开心", self.adapter, reply_to="other", allowed=lambda: True, expected_epoch=0, automatic=True))
        self.adapter.send_image_file.assert_not_awaited()
        self.assertFalse(await self.service.send_category("g", "开心", self.adapter, reply_to=None, allowed=lambda: True, expected_epoch=0))

    async def test_reconnection_checks_platform_revocation_and_member_withdrawal(self):
        add_sticker(self.store)
        for operation in ("platform", "privacy"):
            self.store.set_member_consent("g", "a", "unknown")
            self.service.platform_event("g", "GROUP_MSG_RECEIVE")
            with self.store.transaction() as db:
                db.execute("UPDATE group_sticker_delivery SET last_attempt=0")
            async def connected():
                if operation == "platform":
                    self.service.platform_event("g", "GROUP_MSG_REJECT")
                else:
                    self.store.set_member_consent("g", "a", "opted_out")
                return True
            self.adapter._ensure_connected.side_effect = connected
            record = ReplyRequest("a" * 32, "g", self.store.member_ref_for("g", "a"), operation, 0, "official", (operation,), "好玩", True,
                                  pending_message="哈哈，太好玩了", sticker_adapter=self.adapter, sticker_guard=lambda: True)
            self.assertTrue(self.service.queue_reply(record))
            await self.drain()
            self.assertEqual(self.adapter.images, [])

    async def test_asset_owner_withdrawal_during_reconnect_prevents_another_members_send(self):
        add_sticker(self.store, member="b")
        async def connected():
            self.store.set_member_consent("g", "b", "opted_out")
            return True
        self.adapter._ensure_connected.side_effect = connected
        record = ReplyRequest("a" * 32, "g", self.store.member_ref_for("g", "a"), "owner-withdrawal", 0,
                              "official", ("owner-withdrawal",), "好玩", True, pending_message="哈哈",
                              sticker_adapter=self.adapter, sticker_guard=lambda: True)
        self.assertTrue(self.service.queue_reply(record))
        await self.drain()
        self.assertEqual(self.adapter.images, [])


class HandlerTests(StickerFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handler = build_handler(self.ctx, self.store)
        self.service = self.handler.stickers
        self.gateway = SimpleNamespace(
            adapters={"qqbot": self.adapter}, _is_user_authorized_for_source=lambda source: True,
            _check_slash_access=lambda source, name: None, _handle_reset_command=AsyncMock(),
        )

    async def test_official_and_nonmention_collection_and_manual_library_sending(self):
        self.assertEqual((await self.handler(event("这张好好笑", "official", paths=[self.image]), self.gateway))["action"], "rewrite")
        await self.drain()
        self.adapter._process_attachments.return_value = {"image_urls": [str(self.image)]}
        payload = {"d": {"id": "nonmention", "group_openid": "g", "content": "", "author": {"member_openid": "b"},
                         "attachments": [{"url": "https://gchat.qpic.cn/synthetic.png", "content_type": "image/png"}]}}
        self.assertTrue(await _observe_message(self.adapter, payload, self.handler.observe_nonmention))
        await self.drain()
        self.assertEqual(self.adapter.images, [])
        self.assertEqual(self.adapter.dispatches, [])
        self.assertEqual(self.ctx.llm.acomplete_structured.await_count, 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM group_sticker_sources").fetchone()[0], 2)
        incoming = event("/表情 开心", "manual")
        await self.handler(incoming, self.gateway)
        await asyncio.sleep(0.02)
        self.assertEqual(self.adapter.images[0][0:3], ("g", png(), "manual"))
        self.assertFalse(Path(self.adapter.images[0][3]).exists())
        await self.handler(incoming, self.gateway)
        await asyncio.sleep(0)
        self.assertEqual(len(self.adapter.images), 1)

    async def test_successful_short_reply_appends_one_sticker_with_cooldown(self):
        add_sticker(self.store)
        for message in ("one", "two"):
            rewritten = await self.handler(event("这个梗好好笑", message), self.gateway)
            text = self.handler.transform_llm_output('{"action":"reply","message":"哈哈，太可爱了！"}', rewritten["text"], platform="qqbot")
            await self.adapter.send("g", text, reply_to=message)
            await self.drain()
        self.assertEqual(len(self.adapter.texts), 2)
        self.assertEqual(len(self.adapter.images), 1)
        self.assertEqual(self.adapter.images[0][2], "one")

    async def test_ignore_failed_quiet_only_private_and_user_optout_replies_never_auto_send(self):
        add_sticker(self.store)
        for mode in ("ignore", "failed", "quiet", "only", "private", "optout"):
            self.adapter.success = mode != "failed"
            if mode == "quiet":
                self.handler.character.command("g", "a", "少说一点", "quiet-control")
            elif mode == "only":
                self.handler.character.command("g", "a", "活跃一点", "recover")
                self.handler.character.set_group_mode("g", "only")
            elif mode == "optout":
                self.store.set_member_consent("g", "a", "opted_out")
            rewritten = await self.handler(event("这个梗好好笑", mode, private=mode == "private"), self.gateway)
            envelope = '{"action":"ignore","message":null}' if mode == "ignore" else '{"action":"reply","message":"哈哈，太可爱了！"}'
            text = self.handler.transform_llm_output(envelope, rewritten["text"], platform="qqbot")
            if mode != "ignore":
                await self.adapter.send("g", text, reply_to=mode)
            await self.drain()
            self.assertEqual(self.adapter.images, [])

    async def test_authorization_denial_and_private_images_do_not_collect(self):
        self.gateway._is_user_authorized_for_source = lambda source: False
        before = list(self.store.db.iterdump())
        self.assertEqual(await self.handler(event("图片", "denied", paths=[self.image]), self.gateway), {"action": "allow"})
        self.assertEqual(list(self.store.db.iterdump()), before)
        self.gateway._is_user_authorized_for_source = lambda source: True
        await self.handler(event("图片", "private", paths=[self.image], private=True), self.gateway)
        await self.drain()
        self.ctx.llm.acomplete_structured.assert_not_awaited()
        self.assertEqual(self.store.list_group_stickers("g"), [])

    async def test_inventory_is_group_local_and_extended_emotions_are_commands(self):
        add_sticker(self.store, category="感谢")
        await self.handler(event("/表情库", "inventory"), self.gateway)
        await asyncio.sleep(0)
        self.assertIn("感谢 1", self.adapter.texts[-1][1])
        await self.handler(event("/表情库", "other", group="other"), self.gateway)
        await asyncio.sleep(0)
        self.assertIn("感谢 0", self.adapter.texts[-1][1])
        await self.handler(event("发个感谢表情包", "thanks"), self.gateway)
        await asyncio.sleep(0.02)
        self.assertEqual(len(self.adapter.images), 1)
        await self.handler(event("/表情库", "dm", private=True), self.gateway)
        await asyncio.sleep(0)
        self.assertIn("私聊不收藏", self.adapter.texts[-1][1])
        self.assertNotIn("感谢 1", self.adapter.texts[-1][1])


if __name__ == "__main__":
    unittest.main()
