import hashlib
import importlib.util
from pathlib import Path
import unittest
from types import SimpleNamespace


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
