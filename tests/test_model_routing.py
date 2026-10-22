import asyncio
from copy import deepcopy
import enum
import hashlib
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
import qqbot_model_routing as routing

SPEC = importlib.util.spec_from_file_location("patch_qq_model_routing", ROOT / "scripts/patch-hermes-model-routing.py")
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)


class Reason(enum.Enum):
    timeout = "timeout"
    server_error = "server_error"


class Runner:
    def __init__(self, override=None, reasoning=None):
        self.conversation = SimpleNamespace(model_override=deepcopy(override),
                                            reasoning_override=reasoning,
                                            one_turn_restore={"restore": "old model"},
                                            last_resolved_model=routing.FLASH_MODEL)
        self.state = SimpleNamespace(conversation=self.conversation,
                                     turn=SimpleNamespace(agent=None, started_ts=0))
        self.history = [{"role": "user", "content": "keep this"}, {"role": "assistant", "content": "and this"}]
        self.session_id = "unchanged-session"
        self.persisted = {"qq:one": deepcopy(override), "qq:other": {"model": "other"}}
        self.events = []
        self.session_store = SimpleNamespace(get_model_override=lambda key: self.persisted.get(key),
                                             set_model_override=self.set_model_override)
        self.async_session_store = SimpleNamespace(set_model_override=Mock(side_effect=AssertionError("must not yield")))

    def _session_key_for_source(self, source):
        return "qq:one"

    def _session_model_override(self, key):
        return self.conversation.model_override

    def _peek_session_state(self, key):
        return self.state

    def _resolve_session_reasoning_config(self, **kwargs):
        return self.conversation.reasoning_override or {"enabled": True, "effort": "high"}

    def set_model_override(self, key, value):
        self.events.append("persist")
        self.persisted[key] = value

    def _restore_session_model_override(self, key, snapshot):
        assert snapshot == {"had_override": False, "override": None}
        self.events.append("evict")
        self.conversation.model_override = None


SOURCE = SimpleNamespace(platform="qqbot")


def agent(**kwargs):
    values = dict(platform="qqbot", model=routing.FAST_MODEL, provider=routing.PROVIDER,
                  base_url=routing.BASE_URL, api_mode="chat_completions",
                  reasoning_config={"enabled": True, "effort": "high"},
                  _primary_runtime={"reasoning_config": {"enabled": True, "effort": "low"}},
                  _interrupt_requested=False, _qq_deadline_exceeded=False)
    values.update(kwargs)
    return SimpleNamespace(**values)


def configure(value=None, runner=None):
    value, runner = value or agent(), runner or Runner()
    routing.configure_turn(value, runner, source=SOURCE, session_key="qq:one")
    return value


class ModelRoutingTests(unittest.TestCase):
    def test_actual_route_log_is_once_and_contains_no_model_content(self):
        value = configure()
        with patch.object(routing.logger, "info") as info:
            routing.note_stream_chunk(value, {"choices": [{"delta": {"reasoning_content": "private synthetic reasoning"}}]})
            routing.note_progress(value, "tools")
        info.assert_called_once_with("QQ_MODEL_ROUTE actual=%s effort=%s progress=%s",
                                     routing.FAST_MODEL, "high", "stream")

    def test_auto_falls_back_once_and_preserves_this_turn_reasoning(self):
        for effort in ({"enabled": True, "effort": "high"}, {"enabled": True, "effort": "low"}, {"enabled": False}):
            with self.subTest(effort=effort):
                value = configure(agent(reasoning_config=effort))
                original = deepcopy(effort)

                def swap(a, reason, reset_at=None):
                    self.assertEqual(a._fallback_chain, [{"provider": routing.PROVIDER, "model": routing.FLASH_MODEL,
                                                         "api_mode": "chat_completions"}])
                    a.model = routing.FLASH_MODEL
                    a.reasoning_config = {"enabled": True, "effort": "max"}
                    return True

                native = Mock(side_effect=swap)
                self.assertTrue(routing.guarded_fallback(value, Reason.server_error, None, native))
                self.assertEqual(value.reasoning_config, original)
                self.assertEqual(value._primary_runtime["reasoning_config"], original)
                self.assertFalse(routing.guarded_fallback(value, Reason.timeout, None, native))
                self.assertEqual(native.call_count, 1)

    def test_only_known_retryable_reasons_are_admitted_before_output(self):
        for reason in routing.RETRYABLE_REASONS:
            native = Mock(return_value=True)
            self.assertTrue(routing.guarded_fallback(configure(), reason, None, native))
            native.assert_called_once()
        for reason in (None, "auth", "auth_permanent", "billing", "unknown", "model_not_found", "context_overflow",
                       "content_policy_blocked", "ssl_cert_verification", "upstream_blocked", [], {}):
            with self.subTest(reason=reason):
                native = Mock(return_value=True)
                self.assertFalse(routing.guarded_fallback(configure(), reason, None, native))
                native.assert_not_called()

    def test_manual_pins_including_fast_disable_all_model_fallback(self):
        for model in (routing.FLASH_MODEL, routing.FAST_MODEL, "other"):
            value = configure(agent(model=model), Runner({"model": model}))
            native = Mock(return_value=True)
            self.assertFalse(routing.guarded_fallback(value, Reason.timeout, None, native))
            self.assertEqual(value._fallback_chain, [])
            native.assert_not_called()

    def test_every_model_output_shape_blocks_fallback_even_hidden_reasoning(self):
        for field, payload in (("content", "hello"), ("reasoning", "thinking"), ("reasoning_content", "thinking"),
                               ("reasoning_details", [{"text": "thinking"}]), ("tool_calls", [{"id": "call-1"}]),
                               ("refusal", "refused")):
            for as_extra in (False, True):
                with self.subTest(field=field, as_extra=as_extra):
                    value = configure()
                    delta = {"model_extra": {field: payload}} if as_extra else {field: payload}
                    routing.note_stream_chunk(value, {"choices": [{"delta": delta}]})
                    native = Mock(return_value=True)
                    self.assertFalse(routing.guarded_fallback(value, Reason.timeout, None, native))
                    self.assertEqual(value._fallback_index, len(value._fallback_chain))
                    native.assert_not_called()

    def test_role_only_chunk_does_not_claim_model_content(self):
        value = configure()
        routing.note_stream_chunk(value, SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(role="assistant"))]))
        self.assertTrue(routing.guarded_fallback(value, Reason.timeout, None, Mock(return_value=True)))

    def test_success_response_and_tool_execution_block_restart(self):
        for success in (True, False):
            value = configure()
            tool = Mock(return_value="already changed state")
            if success:
                response = object()
                self.assertIs(routing.observe_response(value, response), response)
            else:
                routing.note_progress(value, "tools")
                tool()
            native = Mock(side_effect=lambda *_args, **_kw: tool())
            self.assertFalse(routing.guarded_fallback(value, Reason.timeout, None, native))
            native.assert_not_called()
            self.assertEqual(tool.call_count, 0 if success else 1)

    def test_configure_replaces_state_without_callback_wrappers_and_rejects_late_workers(self):
        value = agent(stream_delta_callback=Mock(), tool_start_callback=Mock())
        callbacks = (value.stream_delta_callback, value.tool_start_callback)
        configure(value)
        first = routing.capture_route_state(value)
        routing.note_progress(value, "first turn")
        configure(value)
        current = routing.capture_route_state(value)
        self.assertIsNot(first, current)
        routing.observe_response(value, object(), state=first)
        routing.observe_response(value, object(), state=None)
        self.assertFalse(current.progressed)
        self.assertEqual(callbacks, (value.stream_delta_callback, value.tool_start_callback))
        self.assertTrue(routing.guarded_fallback(value, Reason.timeout, None, Mock(return_value=True)))

    def test_failed_activation_cannot_start_another_switch(self):
        value = configure()
        native = Mock(return_value=False)
        self.assertFalse(routing.guarded_fallback(value, Reason.timeout, None, native))
        self.assertFalse(routing.guarded_fallback(value, Reason.timeout, None, native))
        native.assert_called_once()

    def test_interrupt_deadline_and_other_route_cannot_fallback(self):
        for options in ({"_interrupt_requested": True}, {"_qq_deadline_exceeded": True},
                        {"base_url": "https://other.invalid"}, {"provider": "other"}, {"api_mode": "responses"}):
            value = configure(agent(**options))
            native = Mock(return_value=True)
            self.assertFalse(routing.guarded_fallback(value, Reason.timeout, None, native))
            native.assert_not_called()
        self.assertFalse(routing.guarded_fallback(agent(), Reason.timeout, None, Mock(return_value=True)))

    def test_non_qq_retains_native_behavior_and_no_route_state(self):
        value = agent(platform="telegram")
        routing.configure_turn(value, Runner(), source=SimpleNamespace(platform="telegram"), session_key="telegram:one")
        self.assertIsNone(routing.capture_route_state(value))
        native = Mock(return_value="native")
        self.assertEqual(routing.guarded_fallback(value, "auth", 100, native), "native")
        native.assert_called_once_with(value, "auth", reset_at=100)
        self.assertFalse(routing.preserve_reasoning(value))

    def test_native_reasoning_reparse_keeps_manual_effort(self):
        value = configure(agent(reasoning_config={"enabled": False}))
        value.reasoning_config = {"enabled": True, "effort": "high"}
        self.assertTrue(routing.preserve_reasoning(value))
        self.assertEqual(value.reasoning_config, {"enabled": False})


class ModelControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_clears_only_model_and_one_turn_restore_preserving_history_reasoning_and_other_groups(self):
        runner = Runner({"model": routing.FLASH_MODEL}, {"enabled": True, "effort": "low"})
        old_history, old_id = deepcopy(runner.history), runner.session_id
        before = routing.session_route_status(runner, SOURCE)
        self.assertEqual((before["mode"], before["model"], before["reasoning"], before["reasoning_manual"]),
                         ("manual", routing.FLASH_MODEL, "low", True))
        await routing.clear_manual_model(runner, SOURCE)
        self.assertEqual(runner.events, ["persist", "evict"])
        self.assertEqual((runner.history, runner.session_id), (old_history, old_id))
        self.assertEqual(runner.persisted["qq:other"], {"model": "other"})
        self.assertIsNone(runner.conversation.one_turn_restore)
        status = routing.session_route_status(runner, SOURCE)
        self.assertEqual(status, {"mode": "auto", "model": routing.FAST_MODEL, "reasoning": "low",
                                  "reasoning_manual": True, "background_model": routing.FLASH_MODEL,
                                  "background_reasoning": "low"})
        runner.async_session_store.set_model_override.assert_not_called()

    async def test_busy_turn_and_failed_persistence_leave_overrides_intact(self):
        for busy in ("agent", "started_ts"):
            runner = Runner({"model": routing.FLASH_MODEL})
            setattr(runner.state.turn, busy, object() if busy == "agent" else 1)
            with self.assertRaises(routing.ModelRoutingBusy):
                await routing.clear_manual_model(runner, SOURCE)
            self.assertEqual(runner.events, [])
            self.assertEqual(runner.conversation.model_override, {"model": routing.FLASH_MODEL})
        runner = Runner({"model": routing.FLASH_MODEL})
        runner.session_store.set_model_override = Mock(side_effect=OSError("synthetic durable write failure"))
        with self.assertRaises(OSError):
            await routing.clear_manual_model(runner, SOURCE)
        self.assertEqual(runner.events, [])
        self.assertEqual(runner.conversation.model_override, {"model": routing.FLASH_MODEL})
        self.assertIsNotNone(runner.conversation.one_turn_restore)

    async def test_persisted_pin_is_visible_before_native_rehydration_and_off_is_separate(self):
        runner = Runner({"model": routing.FLASH_MODEL, "api_key": "never return this"}, {"enabled": False})
        runner.conversation.model_override = None
        status = routing.session_route_status(runner, SOURCE)
        self.assertEqual((status["mode"], status["reasoning"]), ("manual", "off"))
        self.assertNotIn("never return this", repr(status))
        value = configure(runner=runner)
        self.assertFalse(routing.capture_route_state(value).enabled)
        await routing.clear_manual_model(runner, SOURCE)
        self.assertEqual(routing.session_route_status(runner, SOURCE)["reasoning"], "off")

    async def test_default_high_and_non_qq_control_rejected(self):
        self.assertEqual(routing.session_route_status(Runner(), SOURCE)["reasoning"], "high")
        self.assertFalse(routing.session_route_status(Runner(), SOURCE)["reasoning_manual"])
        runner = Runner()
        with self.assertRaises(ValueError):
            await routing.clear_manual_model(runner, SimpleNamespace(platform="telegram"))
        self.assertEqual(runner.events, [])


class RoutingPatchTests(unittest.TestCase):
    def test_pinned_patch_is_idempotent_and_fails_closed_on_changed_input(self):
        source = "def run():\n    return 1\n"
        digest = hashlib.sha256(source.encode()).hexdigest()
        anchors = [("    return 1\n", "    return 2\n")]
        result = PATCH.patch_source(source, digest, anchors)
        self.assertEqual(result, PATCH.patch_source(result, digest, anchors))
        with self.assertRaisesRegex(ValueError, "SHA"):
            PATCH.patch_source(source + "# upstream changed\n", digest, anchors)
        with self.assertRaisesRegex(ValueError, "anchor"):
            PATCH.patch_source(source, digest, [("missing", "changed")])
        with self.assertRaisesRegex(ValueError, "sentinel"):
            PATCH.patch_source(result.replace("return 2", "return 3"), digest, anchors)

    def test_real_patched_fallback_wrapper_delegates_once_and_blocks_after_tool(self):
        old, new = PATCH.PATCHES["agent/chat_completion_helpers.py"][1][0]
        source = old + "    agent.native_calls += 1\n    agent.model = 'deepseek/deepseek-v4.1-flash'\n    return True\n"
        namespace = {}
        exec(compile(source.replace(old, new, 1), "native_fallback_fixture.py", "exec"), namespace)
        value = configure(agent(native_calls=0))
        fallback = namespace["try_activate_fallback"]
        self.assertTrue(fallback(value, Reason.timeout))
        self.assertFalse(fallback(value, Reason.timeout))
        self.assertEqual(value.native_calls, 1)
        value = configure(agent(native_calls=0))
        routing.note_progress(value, "tools")
        self.assertFalse(fallback(value, Reason.timeout))
        self.assertEqual(value.native_calls, 0)


if __name__ == "__main__":
    unittest.main()
