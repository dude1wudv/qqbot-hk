import hashlib
import importlib.util
from pathlib import Path
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "patch-hermes-aux-budget.py"
SPEC = importlib.util.spec_from_file_location("patch_hermes_aux_budget", SCRIPT)
PATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PATCH)


def fixture_source() -> str:
    return (
        "from typing import Any\n\n"
        "def _is_anthropic_compat_endpoint(provider, effective_base):\n"
        "    return str(provider or '').lower() == 'anthropic'\n\n"
        "def _nous_on_messages_wire(provider_norm, model):\n"
        "    return False\n\n"
        "_NVIDIA_PROVIDER_NAMES = {'nvidia'}\n\n"
        "def base_url_host_matches(base, host):\n"
        "    return host in str(base or '')\n\n"
        "def _is_gemini_native_route(provider_norm, effective_base):\n"
        "    return False\n\n"
        "def _is_managed_local_endpoint(effective_base):\n"
        "    return False\n\n"
        "def _forwards_max_tokens(provider, provider_norm, model, effective_base, task):\n"
        "    return (\n"
        "        _is_anthropic_compat_endpoint(provider, effective_base)\n"
        "        or _nous_on_messages_wire(provider_norm, model)\n"
        "        or provider_norm in _NVIDIA_PROVIDER_NAMES\n"
        "        or base_url_host_matches(effective_base, 'integrate.api.nvidia.com')\n"
        "        or str(task) == 'moa_reference'\n"
        "        or _is_gemini_native_route(provider_norm, effective_base)\n"
        "        or provider_norm == 'openrouter'\n"
        "        or base_url_host_matches(effective_base, 'openrouter.ai')\n"
        "        or _is_managed_local_endpoint(effective_base)\n"
        "    )\n\n"
        "def auxiliary_max_tokens_param(max_tokens, model=None):\n"
        "    return {'max_tokens': int(max_tokens)}\n\n"
        "def build_kwargs(provider, provider_norm, model, effective_base, task, max_tokens):\n"
        "    kwargs = {'model': model}\n"
        "    if max_tokens is not None and _forwards_max_tokens(provider, provider_norm, model, effective_base, task):\n"
        "        kwargs.update(auxiliary_max_tokens_param(max_tokens, model=model))\n"
        "    return kwargs\n\n"
        "def clamp_reasoning_config(config):\n"
        "    return config\n\n"
        "def known_reasoning_floor(config, provider, base, model, task):\n"
        "    return config or {'enabled': True, 'effort': 'medium'}\n\n"
        "def merge_session_affinity_headers(kwargs, *args):\n"
        "    return kwargs\n\n"
        "def _runtime_main_value(key):\n"
        "    return None\n\n"
        "def _build_call_kwargs(provider, model, messages, reasoning_config=None, extra_body=None, base_url=None, task='compression', max_tokens=None, timeout=300):\n"
        "    effective_base, provider_norm = base_url, provider.lower()\n"
        "    kwargs = build_kwargs(provider, provider_norm, model, effective_base, task, max_tokens)\n"
        "    kwargs.update(messages=messages, timeout=timeout)\n"
        "    extra_body = dict(extra_body or {})\n"
        "    task_reasoning = extra_body.pop('reasoning', None)\n"
        "    if reasoning_config is None and isinstance(task_reasoning, dict):\n"
        "        reasoning_config = task_reasoning\n"
        "    reasoning_config = clamp_reasoning_config(\n"
        "        known_reasoning_floor(reasoning_config, provider_norm, effective_base, model, task))\n"
        "    kwargs['reasoning_effort'] = 'medium'\n"
        "    extra_body['reasoning'] = reasoning_config\n"
        "    kwargs['extra_body'] = extra_body\n"
        "    return merge_session_affinity_headers(kwargs, provider, base_url, _runtime_main_value(\"session_id\") or None)\n\n"
        "def _without_max_tokens(kwargs):\n"
        "    return {k: v for k, v in kwargs.items() if k not in ('max_tokens', 'max_completion_tokens')}\n\n"
        "def _is_unsupported_parameter_error(exc, name):\n"
        "    return False\n\n"
        "def _is_max_tokens_rejection(exc: Exception, client: Any) -> bool:\n"
        "    err_str = str(exc)\n"
        "    return ('max_tokens' in err_str or 'unsupported_parameter' in err_str\n"
        "            or _is_unsupported_parameter_error(exc, 'max_tokens'))\n\n"
        "def select_budget_retry(exc, client, max_tokens, kwargs):\n"
        "    if max_tokens is not None and _is_max_tokens_rejection(exc, client):\n"
        "        return _without_max_tokens(kwargs)\n"
        "    return None\n\n"
    )


class HermesAuxBudgetPatchTests(unittest.TestCase):
    def setUp(self):
        effort = ModuleType("agent.reasoning_effort")
        effort.DEEPSEEK_V4_EFFORTS = ("low", "high", "max")
        effort.DEEPSEEK_V4_OVERRIDES = {"xhigh": "max"}
        effort.clamp_effort = lambda value, levels, aliases: aliases.get(value, value)
        self.modules = patch.dict("sys.modules", {"agent": ModuleType("agent"), "agent.reasoning_effort": effort})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def _load_patched(self):
        source = fixture_source()
        patched = PATCH.patch_source(source, hashlib.sha256(source.encode("utf-8")).hexdigest())
        namespace = {}
        exec(compile(patched, "fixture_auxiliary_client.py", "exec"), namespace)
        return namespace

    def test_patch_is_idempotent_and_forwards_explicit_cap_only_on_pinned_route(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        patched = PATCH.patch_source(source, digest)
        self.assertEqual(1, patched.count(PATCH.MARKER))
        self.assertEqual(patched, PATCH.patch_source(patched, "wrong-on-purpose"))

        namespace = self._load_patched()
        for cap in (180, 1400, 8192):
            with self.subTest(cap=cap):
                self.assertEqual(
                    {"model": "task-model", "max_tokens": cap},
                    namespace["build_kwargs"](
                        "openai", "openai", "task-model", "http://sub2api:8080/v1", "summary", cap
                    ),
                )

    def test_route_matching_is_exact_and_existing_route_rules_are_unchanged(self):
        namespace = self._load_patched()
        forwards = namespace["_forwards_max_tokens"]
        self.assertTrue(forwards("openai", "openai", "m", " http://sub2api:8080/v1/ ", "summary"))
        for base in (
            "http://sub2api:8080/v1.evil",
            "http://sub2api:8080/v1/other",
            "https://sub2api:8080/v1",
            "http://other:8080/v1",
        ):
            with self.subTest(base=base):
                self.assertFalse(forwards("openai", "openai", "m", base, "summary"))

        # The patch adds the pinned route while retaining the existing Anthropic policy.
        self.assertTrue(forwards("anthropic", "anthropic", "m", "https://api.example/v1", "summary"))
        self.assertFalse(forwards("openai", "openai", "m", "https://api.example/v1", "summary"))

    def test_sub2api_max_tokens_rejection_does_not_select_budget_stripping_retry(self):
        namespace = self._load_patched()
        error = RuntimeError("max_tokens exceeds provider limit")
        sub2api_client = SimpleNamespace(base_url="http://sub2api:8080/v1")
        other_client = SimpleNamespace(base_url="https://api.example/v1")
        original = {"max_tokens": 1400, "messages": ["kept"]}

        self.assertFalse(namespace["_is_max_tokens_rejection"](error, sub2api_client))
        self.assertIsNone(namespace["select_budget_retry"](error, sub2api_client, 1400, original))
        self.assertTrue(namespace["_is_max_tokens_rejection"](error, other_client))
        self.assertEqual(
            {"messages": ["kept"]},
            namespace["select_budget_retry"](error, other_client, 1400, original),
        )

    def test_wrong_sha_fails_closed(self):
        with self.assertRaisesRegex(PATCH.PatchError, "SHA mismatch"):
            PATCH.patch_source(fixture_source(), "0" * 64)

    def test_explicit_low_high_off_and_default_low_on_both_deepseek_models(self):
        build = self._load_patched()["_build_call_kwargs"]
        for model in ("deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash-fast"):
            for config, expected in ((None, "low"), ({"enabled": True, "effort": "low"}, "low"),
                                     ({"enabled": True, "effort": "high"}, "high"), ({"enabled": False}, None)):
                for cap in (None, 180, 5000, 8192):
                    with self.subTest(model=model, config=config, cap=cap):
                        extra = {"reasoning": {"enabled": True, "effort": "low"}, "keep": {"value": 1}}
                        kwargs = build("sub2api_deepseek", model, ["keep message"], reasoning_config=config,
                                       extra_body=extra, base_url="http://sub2api:8080/v1/", max_tokens=cap)
                        self.assertEqual(kwargs.get("reasoning_effort"), expected)
                        if expected is None:
                            self.assertNotIn("reasoning_effort", kwargs)
                        self.assertEqual(kwargs["extra_body"], {"keep": {"value": 1}})
                        self.assertEqual(kwargs.get("max_tokens"), cap)
                        self.assertEqual(kwargs["messages"], ["keep message"])
                        self.assertEqual(kwargs["timeout"], 300)
                        self.assertIn("reasoning", extra, "caller-owned body was mutated")

    def test_absent_reasoning_defaults_low_and_task_high_is_preserved(self):
        build = self._load_patched()["_build_call_kwargs"]
        for body, expected in ((None, "low"), ({"reasoning": {"enabled": True, "effort": "high"}}, "high"),
                               ({"reasoning": {"enabled": False}}, None)):
            result = build("sub2api_deepseek", "deepseek/deepseek-v4.1-flash", [],
                           base_url="http://sub2api:8080/v1", extra_body=body)
            self.assertEqual(result.get("reasoning_effort"), expected)
            self.assertNotIn("extra_body", result)
            self.assertNotIn("max_tokens", result)

    def test_non_target_models_and_urls_keep_original_reasoning_projection(self):
        build = self._load_patched()["_build_call_kwargs"]
        for model, base in (("other-model", "http://sub2api:8080/v1"),
                            ("deepseek/deepseek-v4.1-flash-new", "http://sub2api:8080/v1"),
                            ("deepseek/deepseek-v4.1-flash", "https://other.invalid/v1"),
                            ("deepseek/deepseek-v4.1-flash", "http://sub2api:8080/v1.evil"),
                            ("deepseek/deepseek-v4.1-flash-fast", "https://sub2api:8080/v1")):
            with self.subTest(model=model, base=base):
                config = {"enabled": True, "effort": "high"}
                result = build("custom", model, [], reasoning_config=config, base_url=base)
                self.assertEqual(result["reasoning_effort"], "medium")
                self.assertEqual(result["extra_body"], {"reasoning": config})

    def test_v1_upgrade_restores_original_hash_then_applies_v2(self):
        source = fixture_source()
        digest = hashlib.sha256(source.encode()).hexdigest()
        legacy = source
        for old, new in PATCH.BUDGET_PATCHES:
            legacy = legacy.replace(old, new, 1)
        legacy += "\n" + PATCH.LEGACY_MARKER + "\n"
        upgraded = PATCH.patch_source(legacy, digest)
        self.assertEqual(upgraded, PATCH.patch_source(source, digest))
        self.assertNotIn(PATCH.LEGACY_MARKER, upgraded)
        self.assertIn('QQBOT_HK_AUX_BUDGET_PATCH = "v2"', upgraded)
        with self.assertRaisesRegex(PATCH.PatchError, "SHA mismatch"):
            PATCH.patch_source(legacy + "# unrelated drift\n", digest)

    def test_corrupted_already_patched_source_is_rejected(self):
        source = fixture_source()
        patched = PATCH.patch_source(source, hashlib.sha256(source.encode("utf-8")).hexdigest())
        corrupted = patched.replace(
            "return str(value or \"\").strip().rstrip(\"/\") == \"http://sub2api:8080/v1\"",
            "return True",
            1,
        )
        with self.assertRaisesRegex(PATCH.PatchError, "sentinel mismatch"):
            PATCH.patch_source(corrupted, "wrong-on-purpose")


if __name__ == "__main__":
    unittest.main()
