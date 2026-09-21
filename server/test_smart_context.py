import json
import os
import tempfile
import unittest
from unittest import mock

import smart_context as smart


class SessionIdentity(unittest.TestCase):
    def user(self, text, item_id=None):
        item = {"role": "user", "content": [{"type": "input_text", "text": text}]}
        if item_id:
            item["id"] = item_id
        return item

    def test_explicit_thread_id_is_stable(self):
        a = smart.session_key({"thread_id": "abc", "input": [self.user("one")]}, "/repo")
        b = smart.session_key({"thread_id": "abc", "input": [self.user("two")]}, "/repo")
        self.assertEqual(a, b)
        self.assertNotIn("abc", a)

    def test_first_item_id_is_stable_across_replayed_history(self):
        first = self.user("first", "msg_1")
        a = smart.session_key({"input": [first, self.user("second")]}, "/repo")
        b = smart.session_key({"input": [first, self.user("third")]}, "/repo")
        self.assertEqual(a, b)

    def test_environment_cwd_is_read_locally(self):
        payload = {"input": [self.user("<environment_context><cwd>/tmp/project</cwd></environment_context>")]}
        self.assertEqual(smart.extract_cwd(payload), "/tmp/project")


class StateEnrichment(unittest.TestCase):
    def test_no_absolute_cwd_or_file_names_enter_jev_state(self):
        repo = smart.RepoSnapshot(project="billing", dirty_files=4, diff_lines=23, available=True)
        state = smart.enrich_jev_state(
            {"task": "continue", "signals": {}, "step": {"type": "user_turn"}},
            "continue",
            {"last_model": smart.TERRA, "last_effort": "high"},
            repo,
            0,
        )
        encoded = json.dumps(state)
        self.assertIn('"project": "billing"', encoded)
        self.assertNotIn("/Users/", encoded)
        self.assertNotIn(".py", encoded)
        self.assertEqual(state["session"]["previous_model"], smart.TERRA)
        self.assertEqual(state["session"]["previous_effort"], "high")
        self.assertTrue(state["signals"]["short_followup"])

    def test_unknown_effort_is_not_persisted_into_jev_state(self):
        state = smart.enrich_jev_state(
            {"task": "x", "signals": {}, "step": {"type": "user_turn"}},
            "x", {"last_model": smart.SOL, "last_effort": "ultra"}, smart.RepoSnapshot(), 0,
        )
        self.assertNotIn("previous_effort", state["session"])


class Guardrails(unittest.TestCase):
    def step(self, kind="user_turn", errored=False):
        return {"step_type": kind, "errored": errored}

    def test_tier_order_includes_terra(self):
        self.assertEqual(smart.TIERS, (smart.LUNA, smart.TERRA, smart.SOL, smart.ASTRA))

    def test_effort_order_matches_codex_five_rungs(self):
        self.assertEqual(smart.EFFORTS, ("low", "medium", "high", "xhigh", "max"))

    def test_successful_tool_step_can_still_drop_to_luna_low(self):
        model, effort, gate = smart.apply_guardrails(
            smart.LUNA, "low", "", self.step("tool_step", False),
            {"last_model": smart.ASTRA, "last_effort": "max"}, 0,
        )
        self.assertEqual((model, effort, gate), (smart.LUNA, "low", "apply"))

    def test_first_tool_failure_holds_both_model_and_effort(self):
        model, effort, gate = smart.apply_guardrails(
            smart.LUNA, "low", "", self.step("tool_step", True),
            {"last_model": smart.TERRA, "last_effort": "high"}, 1,
        )
        self.assertEqual((model, effort), (smart.TERRA, "high"))
        self.assertIn("failure_hold_model", gate)
        self.assertIn("failure_hold_effort", gate)

    def test_two_failures_floor_at_sol_high(self):
        model, effort, gate = smart.apply_guardrails(
            smart.LUNA, "low", "", self.step("tool_step", True), {}, 2,
        )
        self.assertEqual((model, effort), (smart.SOL, "high"))
        self.assertIn("failure_floor_2", gate)

    def test_three_failures_floor_at_astra_xhigh_not_max(self):
        model, effort, gate = smart.apply_guardrails(
            smart.LUNA, "low", "", self.step("tool_step", True), {}, 3,
        )
        self.assertEqual((model, effort), (smart.ASTRA, "xhigh"))
        self.assertIn("failure_floor_3", gate)

    def test_short_continuation_can_only_drop_one_model_and_effort_rung(self):
        model, effort, gate = smart.apply_guardrails(
            smart.LUNA, "low", "继续", self.step("user_turn"),
            {"last_model": smart.ASTRA, "last_effort": "max"}, 0,
        )
        self.assertEqual((model, effort), (smart.SOL, "xhigh"))
        self.assertIn("continuation_model_hysteresis", gate)
        self.assertIn("continuation_effort_hysteresis", gate)

    def test_one_rung_drop_is_allowed(self):
        model, effort, gate = smart.apply_guardrails(
            smart.TERRA, "high", "继续", self.step("user_turn"),
            {"last_model": smart.SOL, "last_effort": "xhigh"}, 0,
        )
        self.assertEqual((model, effort, gate), (smart.TERRA, "high", "apply"))

    def test_real_new_task_can_drop_freely(self):
        task = "Rename the typo in README and update the matching snapshot test."
        model, effort, gate = smart.apply_guardrails(
            smart.LUNA, "low", task, self.step("user_turn"),
            {"last_model": smart.ASTRA, "last_effort": "max"}, 0,
        )
        self.assertEqual((model, effort, gate), (smart.LUNA, "low", "apply"))


class FailureStreak(unittest.TestCase):
    def test_streak_tracks_only_consecutive_failed_tool_steps(self):
        prev = {"failure_streak": 1}
        self.assertEqual(smart.next_failure_streak(prev, {"step_type": "tool_step", "errored": True}), 2)
        self.assertEqual(smart.next_failure_streak(prev, {"step_type": "tool_step", "errored": False}), 0)
        self.assertEqual(smart.next_failure_streak(prev, {"step_type": "user_turn", "errored": False}), 0)


class Store(unittest.TestCase):
    def test_store_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.json")
            store = smart.SessionStore(path)
            store.put("abc", last_model=smart.TERRA, last_effort="xhigh", failure_streak=1)
            value = store.get("abc")
            self.assertEqual(value["last_model"], smart.TERRA)
            self.assertEqual(value["last_effort"], "xhigh")
            self.assertEqual(value["failure_streak"], 1)


class RepoProfiler(unittest.TestCase):
    def test_shortstat_parser(self):
        self.assertEqual(smart.RepoProfiler._shortstat("3 files changed, 12 insertions(+), 4 deletions(-)"), (3, 16))
        self.assertEqual(smart.RepoProfiler._shortstat("1 file changed, 2 deletions(-)"), (1, 2))
        self.assertEqual(smart.RepoProfiler._shortstat(""), (0, 0))

    @mock.patch("smart_context.subprocess.run")
    def test_git_commands_are_read_only_and_never_use_a_shell(self, run):
        run.side_effect = [
            mock.Mock(returncode=0, stdout="/tmp/repo\n"),
            mock.Mock(returncode=0, stdout="2 files changed, 3 insertions(+), 1 deletion(-)\n"),
        ]
        profiler = smart.RepoProfiler(cache_s=0)
        with mock.patch("smart_context.os.path.isdir", return_value=True):
            snap = profiler.snapshot("/tmp/repo")
        self.assertTrue(snap.available)
        self.assertEqual((snap.dirty_files, snap.diff_lines), (2, 4))
        for call in run.call_args_list:
            self.assertNotIn("shell", call.kwargs)


if __name__ == "__main__":
    unittest.main()
