import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
import sys
from types import ModuleType
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "patch-hermes-chat-reasoning.py"
SPEC = importlib.util.spec_from_file_location("patch_hermes_chat_reasoning", SCRIPT)
PATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PATCH)


def fixture_source() -> str:
    return (
        "from agent.reasoning_effort import (\n"
        "    KIMI_K3_EFFORTS, KIMI_K3_OVERRIDES, OPENAI_COMPAT_WIRE_EFFORTS, TOKENHUB_EFFORTS, clamp_effort,\n"
        ")\n"
        "def build(reasoning_config, params, api_kwargs, model, supports_reasoning, is_lmstudio):\n"
        "        thinking_off = isinstance(reasoning_config, dict) and reasoning_config.get(\"enabled\") is False\n"
        "        _e = requested_effort(reasoning_config)\n"
        "        if supports_reasoning and not is_lmstudio:\n"
        "            api_kwargs['generic_branch'] = True\n"
    )


class HermesChatReasoningPatchTests(unittest.TestCase):
    def test_patch_is_exact_and_idempotent(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        patched = PATCH.patch_source(source, digest)
        self.assertIn(PATCH.PATCH_MARKER, patched)
        self.assertEqual(PATCH.PATCH_MARKER, '# QQBOT_HK_CHAT_REASONING_PATCH = "v3"')
        self.assertIn('api_kwargs["reasoning_effort"] = clamp_effort(', patched)
        self.assertEqual(patched.count("is_sub2api_deepseek ="), 1)
        self.assertNotIn("is_sub2api_native_effort", patched)
        self.assertEqual(patched.count("if supports_reasoning and not is_lmstudio and not is_sub2api_deepseek:"), 1)
        self.assertEqual(patched, PATCH.patch_source(patched, "wrong-on-purpose"))

    def test_executed_patch_preserves_deepseek_effort_and_route_boundaries(self):
        effort = ModuleType("agent.reasoning_effort")
        for name in ("KIMI_K3_EFFORTS", "OPENAI_COMPAT_WIRE_EFFORTS", "TOKENHUB_EFFORTS"):
            setattr(effort, name, ("low", "medium", "high"))
        effort.DEEPSEEK_V4_EFFORTS = ("low", "medium", "high", "max")
        effort.DEEPSEEK_V4_OVERRIDES = {"xhigh": "max"}
        effort.KIMI_K3_OVERRIDES = {}
        effort.clamp_effort = Mock(side_effect=lambda value, allowed, overrides: overrides.get(value, value))
        requested = Mock(side_effect=lambda config: config.get("effort"))
        namespace = {"requested_effort": requested}
        source = fixture_source()
        patched = PATCH.patch_source(source, hashlib.sha256(source.encode()).hexdigest())
        with patch.dict(sys.modules, {"agent": ModuleType("agent"), "agent.reasoning_effort": effort}):
            exec(compile(patched, "fixture_chat.py", "exec"), namespace)
        for model in ("deepseek/deepseek-v4.1-flash", "DEEPSEEK/DEEPSEEK-V4.1-FLASH"):
            for enabled in (True, False):
                for supports in (True, False):
                    with self.subTest(model=model, enabled=enabled, supports=supports):
                        effort.clamp_effort.reset_mock()
                        kwargs = {"untouched": "Value"}
                        config = {"enabled": enabled, "effort": "xhigh"}
                        namespace["build"](config, {"base_url": PATCH.SUB2API_BASE_URL + "/"}, kwargs, model, supports, False)
                        requested.assert_called_with(config)
                        expected = {"untouched": "Value"}
                        if enabled:
                            expected["reasoning_effort"] = "max"
                        self.assertEqual(kwargs, expected)
                        if enabled:
                            effort.clamp_effort.assert_called_once_with("xhigh", effort.DEEPSEEK_V4_EFFORTS, effort.DEEPSEEK_V4_OVERRIDES)
                        else:
                            effort.clamp_effort.assert_not_called()
        for host, model in (
            ("https://other.example/v1", "deepseek/deepseek-v4.1-flash"),
            ("http://sub2api:8080/v1.evil", "deepseek/deepseek-v4.1-flash"),
            (PATCH.SUB2API_BASE_URL, "other/model"),
        ):
            for supports, studio in ((True, False), (False, False), (True, True)):
                with self.subTest(host=host, model=model, supports=supports, studio=studio):
                    effort.clamp_effort.reset_mock()
                    kwargs = {"reasoning_effort": "existing"}
                    namespace["build"]({"effort": "xhigh"}, {"base_url": host}, kwargs, model, supports, studio)
                    expected = {"reasoning_effort": "existing"}
                    if supports and not studio:
                        expected["generic_branch"] = True
                    self.assertEqual(kwargs, expected)
                    effort.clamp_effort.assert_not_called()

    def test_wrong_sha_fails_closed(self):
        with self.assertRaisesRegex(PATCH.PatchError, "SHA mismatch"):
            PATCH.patch_source(fixture_source(), "0" * 64)

    def test_missing_sentinel_fails_closed(self):
        source = fixture_source().replace(PATCH.PATCHES[1][0], "", 1)
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(PATCH.PatchError, "Sub2API top-level reasoning effort"):
            PATCH.patch_source(source, digest)

    def test_patch_file_preserves_an_already_patched_file(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat_completions.py"
            path.write_text(source, encoding="utf-8", newline="\n")
            self.assertTrue(PATCH.patch_file(path, digest))
            first = path.read_bytes()
            self.assertFalse(PATCH.patch_file(path, digest))
            self.assertEqual(first, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
