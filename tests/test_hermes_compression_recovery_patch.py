import asyncio
from dataclasses import dataclass
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "patch-hermes-compression-recovery.py"
SPEC = importlib.util.spec_from_file_location("patch_hermes_compression_recovery", SCRIPT)
PATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PATCH)


def fixture_source() -> str:
    return (
        "from __future__ import annotations\n"
        "import dataclasses\n"
        "\n"
        "class Logger:\n"
        "    def __init__(self):\n"
        "        self.warning_calls = []\n"
        "    def warning(self, *args):\n"
        "        self.warning_calls.append(args)\n"
        "\n"
        "logger = Logger()\n"
        "\n"
        "class Gateway:\n"
        "    async def _hm_offer_pairing_code(self, source: SessionSource) -> None:\n"
        "        return None\n"
        "\n"
        "    async def _handle_inbound(self, event, source):\n"
        "        if not getattr(event, \"_bot_loop_admitted\", False) and not self._admit_bot_message_for_source(source):\n"
        "            return None\n"
        "        return event, source, False\n"
    )


@dataclass
class FakeEvent:
    text: str
    message_id: str = "message"

    def get_command(self):
        return self.text.lstrip("/").split(maxsplit=1)[0] if self.text.startswith("/") else None


class FakeStore:
    async def get_or_create_session(self, source):
        return SimpleNamespace(session_id="session")


class FakeDB:
    def __init__(self, ineffective=0, fallback=0, error=None):
        self.ineffective = ineffective
        self.fallback = fallback
        self.error = error

    async def get_compression_ineffective_count(self, session_id):
        if self.error:
            raise self.error
        return self.ineffective

    async def get_compression_fallback_streak(self, session_id):
        if self.error:
            raise self.error
        return self.fallback


class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class CompressionRecoveryPatchTests(unittest.TestCase):
    def test_patch_is_fail_closed_exact_and_idempotent(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        patched = PATCH.patch_source(source, digest)

        self.assertEqual(patched.count(PATCH.PATCH_MARKER), 1)
        self.assertEqual(patched.count("async def _hm_auto_new_on_ineffective_compression("), 1)
        self.assertNotIn(PATCH.CALL_ANCHOR, patched)
        self.assertIn(PATCH.CALL_REPLACEMENT, patched)
        self.assertLess(patched.index(PATCH.HELPER), patched.index(PATCH.HELPER_ANCHOR))
        compile(patched, "run_inbound.py", "exec")
        self.assertEqual(PATCH.patch_source(patched, "wrong-on-purpose"), patched)

        with self.assertRaisesRegex(PATCH.PatchError, "SHA mismatch"):
            PATCH.patch_source(source, PATCH.EXPECTED_ORIGINAL_SHA256)

    def test_patch_rejects_missing_or_ambiguous_anchors(self):
        source = fixture_source()
        missing_helper = source.replace(PATCH.HELPER_ANCHOR, "", 1)
        missing_helper_digest = hashlib.sha256(missing_helper.encode("utf-8")).hexdigest()

        with self.assertRaisesRegex(PATCH.PatchError, "helper insertion anchor"):
            PATCH.patch_source(missing_helper, missing_helper_digest)
        ambiguous_call = source + PATCH.CALL_ANCHOR
        ambiguous_call_digest = hashlib.sha256(ambiguous_call.encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(PATCH.PatchError, "inbound call anchor"):
            PATCH.patch_source(ambiguous_call, ambiguous_call_digest)

    def test_patch_file_is_idempotent(self):
        import tempfile

        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run_inbound.py"
            path.write_text(source, encoding="utf-8", newline="\n")
            self.assertTrue(PATCH.patch_file(path, digest))
            first = path.read_bytes()
            self.assertFalse(PATCH.patch_file(path, digest))
            self.assertEqual(first, path.read_bytes())

    def make_gateway(self, gateway_class, *, ineffective=0, fallback=0, enabled=True, error=None):
        gateway = gateway_class()
        gateway.config = SimpleNamespace(platforms={
            "qqbot": SimpleNamespace(extra={
                "auto_new_on_compression_ineffective": enabled,
            }),
        })
        gateway.async_session_store = FakeStore()
        gateway._session_db = FakeDB(ineffective, fallback, error)
        gateway.adapter = FakeAdapter()
        gateway.resets = []
        gateway._admit_bot_message_for_source = lambda source: True

        async def reset(event):
            gateway.resets.append(event)

        gateway._handle_reset_command = reset
        gateway._adapter_for_source = lambda source: gateway.adapter
        return gateway

    def test_breaker_thresholds_rotate_with_new_and_continue(self):
        namespace = {}
        exec(PATCH.patch_source(fixture_source(), PATCH.sha256_text(fixture_source())), namespace)
        source = SimpleNamespace(platform="qqbot", chat_id="chat")

        for ineffective, fallback in ((2, 0), (3, 1), (0, 2), (1, 3)):
            with self.subTest(ineffective=ineffective, fallback=fallback):
                gateway = self.make_gateway(
                    namespace["Gateway"], ineffective=ineffective, fallback=fallback,
                )
                event = FakeEvent("ordinary message")
                result = asyncio.run(gateway._handle_inbound(event, source))
                self.assertEqual(result, (event, source, False))
                self.assertEqual([item.text for item in gateway.resets], ["/new"])
                self.assertEqual(len(gateway.adapter.sent), 1)

    def test_disabled_healthy_and_manual_commands_do_not_rotate(self):
        namespace = {}
        exec(PATCH.patch_source(fixture_source(), PATCH.sha256_text(fixture_source())), namespace)
        source = SimpleNamespace(platform="qqbot", chat_id="chat")

        cases = ((1, 1, True, "ordinary message"), (2, 2, False, "ordinary message"),
                 (2, 2, True, "/compress"), (2, 2, True, "/new"))
        for ineffective, fallback, enabled, text in cases:
            with self.subTest(ineffective=ineffective, fallback=fallback, enabled=enabled, text=text):
                gateway = self.make_gateway(
                    namespace["Gateway"], ineffective=ineffective, fallback=fallback, enabled=enabled,
                )
                event = FakeEvent(text)
                result = asyncio.run(gateway._handle_inbound(event, source))
                self.assertEqual(result, (event, source, False))
                self.assertEqual(gateway.resets, [])
                self.assertEqual(gateway.adapter.sent, [])

    def test_recovery_exception_fails_open(self):
        namespace = {}
        exec(PATCH.patch_source(fixture_source(), PATCH.sha256_text(fixture_source())), namespace)
        source = SimpleNamespace(platform="qqbot", chat_id="chat")
        gateway = self.make_gateway(namespace["Gateway"], error=RuntimeError("state unavailable"))
        event = FakeEvent("ordinary message")

        result = asyncio.run(gateway._handle_inbound(event, source))

        self.assertEqual(result, (event, source, False))
        self.assertEqual(gateway.resets, [])
        self.assertEqual(gateway.adapter.sent, [])


if __name__ == "__main__":
    unittest.main()
