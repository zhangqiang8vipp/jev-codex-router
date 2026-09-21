import threading
import time
import unittest

import route_lease as lease
from routing_policy import ASTRA, POLICY_VERSION, SOL, TERRA


def user(text, item_id=None):
    item = {"role": "user", "content": [{"type": "input_text", "text": text}]}
    if item_id:
        item["id"] = item_id
    return item


def assistant(text, item_id=None):
    item = {"role": "assistant", "content": [{"type": "output_text", "text": text}]}
    if item_id:
        item["id"] = item_id
    return item


class TurnIdentity(unittest.TestCase):
    def test_user_item_id_is_stable_across_tool_continuations(self):
        base = [assistant("ready", "a1"), user("fix the bug", "u1")]
        tool = {
            "type": "function_call_output",
            "call_id": "c1",
            "output": "done",
        }
        a = lease.human_turn_key({"input": base}, task="fix the bug", session_key="s")
        b = lease.human_turn_key({"input": base + [tool]}, task="fix the bug", session_key="s")
        self.assertEqual(a, b)

    def test_same_text_after_different_assistant_state_is_a_new_turn_without_ids(self):
        first = lease.human_turn_key(
            {"input": [assistant("state one"), user("continue")]},
            task="continue",
            session_key="s",
        )
        second = lease.human_turn_key(
            {"input": [assistant("state two"), user("continue")]},
            task="continue",
            session_key="s",
        )
        self.assertNotEqual(first, second)

    def test_turn_key_never_contains_prompt_text(self):
        key = lease.human_turn_key(
            {"input": [user("secret-ish project wording")]},
            task="secret-ish project wording",
            session_key="session",
        )
        self.assertIsInstance(key, str)
        self.assertNotIn("secret", key)

    def test_image_only_user_turn_without_id_still_has_a_replay_key(self):
        payload = {
            "input": [{
                "role": "user",
                "content": [{"type": "input_image", "image_url": "data:image/png;base64,abc"}],
            }]
        }
        first = lease.human_turn_key(payload, task="", session_key="session")
        second = lease.human_turn_key(payload, task="", session_key="session")
        self.assertIsInstance(first, str)
        self.assertEqual(first, second)
        self.assertNotIn("data:image", first)



    def test_tool_step_replay_key_is_stable_and_private(self):
        payload = {
            "input": [{
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "sensitive failing output",
            }]
        }
        first = lease.tool_step_key(payload, session_key="session")
        second = lease.tool_step_key(payload, session_key="session")
        self.assertEqual(first, second)
        self.assertNotIn("sensitive", first)

    def test_changed_tool_result_gets_a_new_event_key(self):
        a = lease.tool_step_key({
            "input": [{"type": "function_call_output", "call_id": "c", "output": "first"}]
        }, session_key="s")
        b = lease.tool_step_key({
            "input": [{"type": "function_call_output", "call_id": "c", "output": "second"}]
        }, session_key="s")
        self.assertNotEqual(a, b)


class FailureReplayState(unittest.TestCase):
    def test_failed_tool_replay_does_not_double_count_failure(self):
        first_key = "tool-hash"
        streak, replay, fields = lease.failure_state(
            {"failure_streak": 1},
            step_type="tool_step",
            errored=True,
            tool_key=first_key,
        )
        self.assertEqual(streak, 2)
        self.assertFalse(replay)
        self.assertEqual(fields["last_tool_step_key"], first_key)

        streak2, replay2, fields2 = lease.failure_state(
            {**fields},
            step_type="tool_step",
            errored=True,
            tool_key=first_key,
        )
        self.assertEqual(streak2, 2)
        self.assertTrue(replay2)
        self.assertEqual(fields2["failure_streak"], 2)

    def test_new_user_turn_resets_failure_and_replay_key(self):
        streak, replay, fields = lease.failure_state(
            {"failure_streak": 4, "last_tool_step_key": "old"},
            step_type="user_turn",
            errored=False,
            tool_key=None,
        )
        self.assertEqual(streak, 0)
        self.assertFalse(replay)
        self.assertIsNone(fields["last_tool_step_key"])


class LeasePolicy(unittest.TestCase):
    def make_lease(self, turn_key="t"):
        state = lease.lease_fields(
            TERRA,
            "medium",
            turn_key=turn_key,
            source="jev",
            policy_version=POLICY_VERSION,
        )
        return lease.read_lease(state, POLICY_VERSION)

    def test_new_user_turn_replans(self):
        action, reason = lease.route_action(
            step_type="user_turn",
            meaningful_user_turn=True,
            turn_key="new",
            lease=self.make_lease("old"),
        )
        self.assertEqual((action, reason), ("REPLAN", "new_user_turn"))

    def test_duplicate_user_turn_reuses_semantic_decision(self):
        action, reason = lease.route_action(
            step_type="user_turn",
            meaningful_user_turn=True,
            turn_key="same",
            lease=self.make_lease("same"),
        )
        self.assertEqual((action, reason), ("KEEP", "same_user_turn_replay"))

    def test_tool_continuation_keeps_route(self):
        action, reason = lease.route_action(
            step_type="tool_step",
            meaningful_user_turn=False,
            turn_key="same",
            lease=self.make_lease(),
        )
        self.assertEqual((action, reason), ("KEEP", "tool_continuation"))

    def test_compaction_keeps_route(self):
        action, reason = lease.route_action(
            step_type="other",
            meaningful_user_turn=False,
            turn_key=None,
            lease=self.make_lease(),
            compacted=True,
        )
        self.assertEqual((action, reason), ("KEEP", "compaction_continuity"))

    def test_missing_or_old_policy_lease_replans(self):
        fields = lease.lease_fields(
            TERRA,
            "medium",
            turn_key="t",
            source="jev",
            policy_version="old-policy",
        )
        self.assertIsNone(lease.read_lease(fields, POLICY_VERSION))
        action, reason = lease.route_action(
            step_type="tool_step",
            meaningful_user_turn=False,
            turn_key="t",
            lease=None,
        )
        self.assertEqual((action, reason), ("REPLAN", "no_valid_lease"))

    def test_route_lease_v1_means_one_replan_then_tool_keeps(self):
        current = None
        actions = []
        action, _ = lease.route_action(
            step_type="user_turn",
            meaningful_user_turn=True,
            turn_key="turn-1",
            lease=current,
        )
        actions.append(action)
        current = lease.RouteLease(TERRA, "high", "turn-1", "jev", POLICY_VERSION)
        for _ in range(6):
            action, _ = lease.route_action(
                step_type="tool_step",
                meaningful_user_turn=False,
                turn_key="turn-1",
                lease=current,
            )
            actions.append(action)
        self.assertEqual(actions.count("REPLAN"), 1)
        self.assertEqual(actions.count("KEEP"), 6)



    def test_served_higher_tier_becomes_continuity_route_for_same_turn(self):
        current = self.make_lease("turn")
        fields = lease.served_continuity_fields(
            current,
            served_model=SOL,
            served_effort="high",
            turn_key="turn",
            policy_version=POLICY_VERSION,
        )
        self.assertIsNotNone(fields)
        updated = lease.read_lease(fields, POLICY_VERSION)
        self.assertEqual((updated.model, updated.effort), (SOL, "high"))
        self.assertEqual(updated.source, "served_continuity")

    def test_slow_old_turn_cannot_overwrite_newer_lease(self):
        current = self.make_lease("new-turn")
        fields = lease.served_continuity_fields(
            current,
            served_model=SOL,
            served_effort="high",
            turn_key="old-turn",
            policy_version=POLICY_VERSION,
        )
        self.assertIsNone(fields)


class LocalEscalation(unittest.TestCase):
    def test_first_failure_does_not_replan_or_raise(self):
        self.assertEqual(
            lease.apply_failure_escalation(TERRA, "medium", 1),
            (TERRA, "medium", None),
        )

    def test_second_failure_raises_locally_to_sol_high(self):
        self.assertEqual(
            lease.apply_failure_escalation(TERRA, "medium", 2),
            (SOL, "high", "failure_floor_2"),
        )

    def test_third_failure_raises_locally_to_astra_xhigh(self):
        self.assertEqual(
            lease.apply_failure_escalation(SOL, "high", 3),
            (ASTRA, "xhigh", "failure_floor_3"),
        )

    def test_escalation_never_downgrades_existing_stronger_lease(self):
        self.assertEqual(
            lease.apply_failure_escalation(ASTRA, "max", 3),
            (ASTRA, "max", None),
        )


class SemanticSingleFlight(unittest.TestCase):
    def test_same_session_decision_lock_serializes_callers(self):
        locks = lease.RouteLeaseLocks()
        guard = threading.Lock()
        active = 0
        peak = 0
        order = []

        def worker(name):
            nonlocal active, peak
            with locks.hold("session"):
                with guard:
                    active += 1
                    peak = max(peak, active)
                    order.append(f"{name}:in")
                time.sleep(0.03)
                with guard:
                    order.append(f"{name}:out")
                    active -= 1

        a = threading.Thread(target=worker, args=("a",))
        b = threading.Thread(target=worker, args=("b",))
        a.start()
        b.start()
        a.join(1)
        b.join(1)

        self.assertEqual(peak, 1)
        self.assertEqual(len(order), 4)


class CompactionSignal(unittest.TestCase):
    def test_detects_opaque_compaction_item(self):
        self.assertTrue(lease.contains_compaction({
            "input": [
                {"type": "compaction", "encrypted_content": "opaque"},
                user("continue"),
            ]
        }))
        self.assertFalse(lease.contains_compaction({"input": [user("continue")]}))


if __name__ == "__main__":
    unittest.main()