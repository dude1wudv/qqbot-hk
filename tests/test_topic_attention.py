import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from smart_group_qq.attention import (
    AttentionManager, AttentionMode, ParticipationDecision, parse_participation_decision,
)


class ParticipationDecisionTests(unittest.TestCase):
    def parse(self, value, **kwargs):
        return parse_participation_decision(
            value, candidate_message_ids=("a1", "a2", "a3"),
            candidate_topic_ids=("topic-1", "topic-2"), **kwargs,
        )

    def test_old_classifier_result_keeps_its_admission_without_optional_metadata(self):
        decision = self.parse({"reply": True, "confidence": .8, "engaged": False})
        self.assertEqual(decision, ParticipationDecision(True, .8, False))

    def test_known_metadata_is_kept_and_foreign_ids_are_not_forwarded(self):
        decision = self.parse({
            "reply": True, "confidence": .9, "engaged": True, "topic_id": "topic-2",
            "focus_message_ids": ["a2", "outside-group", "a2", "a1"], "mode_hint": "support",
        })
        self.assertEqual(decision, ParticipationDecision(True, .9, True, "topic-2", ("a2", "a1"), "support"))

    def test_unusable_optional_metadata_degrades_without_losing_valid_reply(self):
        for topic, focus, mode in [("foreign-topic", ["foreign-message"], "unknown"), ([], "a1", {})]:
            with self.subTest(topic=topic):
                decision = self.parse({"reply": True, "confidence": .9, "engaged": False,
                                       "topic_id": topic, "focus_message_ids": focus, "mode_hint": mode})
                self.assertEqual(decision, ParticipationDecision(True, .9, False))

    def test_invalid_gate_fields_never_admit_even_when_metadata_is_valid(self):
        valid = {"reply": True, "confidence": .9, "engaged": False,
                 "topic_id": "topic-1", "focus_message_ids": ["a1"], "mode_hint": "task"}
        invalid = [{"reply": "true"}, {"engaged": 1}, {"confidence": True},
                   {"confidence": "0.9"}, {"confidence": math.nan}, {"confidence": math.inf},
                   {"confidence": -1}, {"confidence": 10 ** 500}, {"confidence": 1.1},
                   {"confidence": .69}, {"reply": False}]
        for change in invalid:
            with self.subTest(change=change):
                decision = self.parse({**valid, **change})
                self.assertFalse(decision.reply)
                self.assertFalse(decision.engaged)
                self.assertIsNone(decision.topic_id)
                self.assertEqual(decision.focus_message_ids, ())
        self.assertFalse(self.parse({"reply": True, "confidence": .9}).reply)
        self.assertFalse(self.parse(None).reply)

    def test_focus_count_and_confidence_threshold_are_bounded(self):
        decision = parse_participation_decision(
            {"reply": True, "confidence": .7, "engaged": True,
             "focus_message_ids": [f"m{i}" for i in range(10)]},
            candidate_message_ids=[f"m{i}" for i in range(10)],
        )
        self.assertTrue(decision.reply)
        self.assertEqual(len(decision.focus_message_ids), 6)
        self.assertFalse(self.parse({"reply": True, "confidence": .7, "engaged": True}, min_confidence=.8).reply)
        for threshold in (None, "0.7", math.nan, 10 ** 500):
            self.assertFalse(self.parse({"reply": True, "confidence": .9, "engaged": True}, min_confidence=threshold).reply)


class TopicAttentionTests(unittest.TestCase):
    def setUp(self):
        self.attention = AttentionManager()

    def record(self, member, question, stamp, reply, **kwargs):
        self.attention.record_success("g", member, question, direct=True, now=stamp, message_id=reply, **kwargs)
        return self.attention.get("g", now=stamp).current_topic_id

    def test_interleaved_topics_keep_continuity_for_different_members(self):
        deployment = self.record("member-a", "Windows deployment failed", 100, "reply-a")
        dinner = self.record("member-b", "Friday dinner menu", 105, "reply-b")
        self.assertNotEqual(deployment, dinner)
        self.assertEqual(self.attention.continuation_score("g", "member-c", "Windows deployment", now=106), 20)
        self.assertTrue(self.attention.is_active_member("g", "member-a", now=106))
        self.assertTrue(self.attention.is_active_member("g", "member-b", now=106))
        self.record("member-c", "Windows deployment logs", 110, "reply-c")
        context = self.attention.topic_context("g", now=111)
        self.assertEqual(len(context), 2)
        self.assertEqual(context[0]["id"], deployment)
        self.assertEqual(context[0]["member_refs"], ["member-a", "member-c"])
        self.assertEqual(self.attention.get("g", now=111).member_ref, "member-c")
        self.assertEqual(self.attention.continuation_score("other", "member-c", "Windows deployment", now=111), 0)

    def test_quote_selects_the_older_topic_without_reusing_its_mode_or_writing_state(self):
        deployment = self.record("member-a", "Windows deployment", 100, "reply-a", mode_hint="casual")
        self.record("member-b", "Friday dinner", 105, "reply-b", mode_hint="support")
        before = self.attention.topic_context("g", now=106)
        decision = self.attention.direct_decision(
            "g", "member-c", "请给详细步骤", message_id="incoming", reference_id="reply-a",
            candidate_message_ids=("incoming", "reply-a"), now=106,
        )
        self.assertEqual(decision.topic_id, deployment)
        self.assertEqual(decision.mode_hint, "auto")
        self.assertEqual(decision.focus_message_ids, ("incoming", "reply-a"))
        self.assertTrue(decision.engaged)
        self.assertEqual(before, self.attention.topic_context("g", now=106))
        self.record("member-c", "详细排障步骤", 107, "reply-c", topic_id=decision.topic_id,
                    focus_message_ids=decision.focus_message_ids, mode_hint="task")
        self.assertEqual(self.attention.topic_context("g", now=107)[0]["id"], deployment)
        self.assertEqual(self.attention.topic_context("g", now=107)[0]["mode_hint"], "task")

    def test_unmatched_direct_turn_does_not_create_topic_before_success(self):
        decision = self.attention.direct_decision("new", "member-a", "hello", message_id="incoming", now=100)
        self.assertEqual(decision, ParticipationDecision(True, 1, True, None, ("incoming",), "auto"))
        self.assertEqual(self.attention.topic_context("new", now=101), [])
        self.assertNotIn("new", self.attention._groups)

    def test_direct_focus_does_not_include_an_unsupplied_reference(self):
        deployment = self.record("member-a", "Windows deployment", 100, "reply-a")
        decision = self.attention.direct_decision(
            "g", "member-a", "继续", message_id="incoming", reference_id="reply-a", now=101,
        )
        self.assertEqual(decision.topic_id, deployment)
        self.assertEqual(decision.focus_message_ids, ("incoming",))
        foreign = self.attention.direct_decision(
            "other", "member-a", "继续", message_id="incoming", reference_id="reply-a", now=101,
        )
        self.assertIsNone(foreign.topic_id)

    def test_three_topics_evict_least_recently_used_without_losing_reply_validity(self):
        first = self.record("a", "alpha deployment", 100, "r1")
        second = self.record("b", "bravo dinner", 101, "r2")
        third = self.record("c", "charlie music", 102, "r3")
        self.record("a", "alpha deployment logs", 103, "r4", topic_id=first)
        fourth = self.record("d", "delta garden", 104, "r5")
        topics = self.attention.topic_context("g", now=105)
        self.assertEqual({item["id"] for item in topics}, {first, third, fourth})
        self.assertNotIn(second, self.attention.get("g", now=105).reply_topics.values())
        self.assertTrue(self.attention.has_reply_id("g", "r2", now=105))
        decision = self.attention.direct_decision("g", "b", "继续", message_id="new", reference_id="r2", now=105)
        self.assertIsNone(decision.topic_id)

    def test_topic_expiration_is_individual_and_context_reads_do_not_renew_it(self):
        self.record("a", "alpha deployment", 100, "r1")
        dinner = self.record("b", "bravo dinner", 150, "r2")
        self.assertEqual(len(self.attention.topic_context("g", now=219)), 2)
        self.assertEqual([item["id"] for item in self.attention.topic_context("g", now=220)], [dinner])
        self.assertFalse(self.attention.is_active_member("g", "a", now=220))
        self.assertEqual(self.attention.get("g", now=270).mode, AttentionMode.PASSIVE)
        self.assertTrue(self.attention.has_reply_id("g", "r1", now=270))
        decision = self.attention.direct_decision("g", "a", "继续", message_id="new", reference_id="r1", now=270)
        self.assertIsNone(decision.topic_id)

    def test_bubble_reply_ids_bind_to_the_selected_topic_and_expire_independently(self):
        self.attention = AttentionManager(ttl_seconds=120, max_reply_ids=2, reply_ttl_seconds=10)
        first = self.record("a", "alpha deployment", 100, "r1")
        self.attention.remember_reply("g", "r2", topic_id=first, now=101)
        self.attention.remember_reply("g", "r3", topic_id=first, now=102)
        state = self.attention.get("g", now=102)
        self.assertEqual(state.reply_topics, {"r2": first, "r3": first})
        self.assertFalse(self.attention.has_reply_id("g", "r1", now=102))
        self.assertEqual(self.attention.get("g", now=113).reply_topics, {})
        self.assertEqual(self.attention.topic_context("g", now=113)[0]["id"], first)

    def test_duplicate_reply_id_has_one_current_topic_association(self):
        self.record("a", "alpha deployment", 100, "same-id")
        second = self.record("b", "bravo dinner", 101, "same-id")
        state = self.attention.get("g", now=101)
        self.assertEqual(list(state.reply_ids), [("same-id", 101)])
        self.assertEqual(state.reply_topics, {"same-id": second})

    def test_group_budget_and_retreat_are_shared_across_topics(self):
        self.attention.record_success("g", "a", "alpha deployment", direct=False, now=100)
        self.attention.record_success("g", "b", "bravo dinner", direct=False, now=101)
        self.assertEqual(len(self.attention.topic_context("g", now=102)), 2)
        self.assertFalse(self.attention.can_interject("g", now=102))
        with patch("smart_group_qq.attention.time.monotonic", return_value=102):
            self.assertTrue(self.attention.reserve_interjection("g", engaged=True))
            self.assertTrue(self.attention.reserve_interjection("g", engaged=True))
            self.assertFalse(self.attention.reserve_interjection("g", engaged=True))

    def test_expired_groups_release_capacity_and_clear_removes_all_associations(self):
        attention = AttentionManager(max_groups=1, ttl_seconds=10)
        attention.record_success("old", "a", "alpha", direct=True, message_id="old-reply", now=100)
        self.assertFalse(attention.can_accept("new", now=109))
        self.assertTrue(attention.can_accept("new", now=110))
        attention.record_success("new", "b", "bravo", direct=True, message_id="new-reply", now=110)
        self.assertNotIn("old", attention._groups)
        attention.clear("new")
        self.assertEqual(attention.topic_context("new", now=111), [])
        self.assertFalse(attention.has_reply_id("new", "new-reply", now=111))

    def test_context_is_bounded_and_returned_lists_do_not_mutate_attention(self):
        self.attention = AttentionManager(max_topics_per_group=100)
        topic = self.record("a", "a" * 1500, 100, "r1", focus_message_ids=[f"m{i}" for i in range(10)])
        context = self.attention.topic_context("g", now=101)
        self.assertEqual(len(context[0]["summary"]), 300)
        self.assertEqual(len(context[0]["focus_message_ids"]), 6)
        context[0]["member_refs"].append("untrusted")
        self.assertNotIn("untrusted", self.attention.get("g", now=101).topics[topic].member_refs)
        self.assertEqual(self.attention.max_topics_per_group, 3)


if __name__ == "__main__":
    unittest.main()
