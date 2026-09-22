import json
import os
import tempfile
import unittest

import session_report


def _rec(session, step, source=None, action=None, status=200,
         model="gpt-5.6-luna", effort="low", at="2026-09-22T10:00:00"):
    return {
        "at": at,
        "session": session,
        "step": step,
        "route_source": source,
        "lease_action": action,
        "lease_reason": None,
        "model": model,
        "effort": effort,
        "status": status,
        "jev_usage": {"input_tokens": 10, "output_tokens": 5},
    }


class SessionReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "live.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, records):
        with open(self.log, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def test_aggregates_keep_and_replan_per_session(self):
        self._write([
            _rec("s1", "user_turn", source="jev", action="REPLAN"),
            _rec("s1", "tool_step", source="lease", action="KEEP"),
            _rec("s1", "tool_step", source="lease", action="KEEP"),
        ])
        sessions = session_report.aggregate(self.log)
        stats = sessions["s1"]
        self.assertEqual(stats["calls"], 3)
        self.assertEqual(stats["user_turns"], 1)
        self.assertEqual(stats["tool_steps"], 2)
        self.assertEqual(stats["jev_decisions"], 1)
        self.assertEqual(stats["lease_keeps"], 2)
        self.assertEqual(stats["replans"], 1)
        self.assertEqual(stats["input_tokens"], 30)
        self.assertEqual(stats["output_tokens"], 15)

    def test_local_escalation_is_counted_not_as_jev(self):
        self._write([
            _rec("s2", "tool_step", source="lease_escalation", action="KEEP"),
        ])
        stats = session_report.aggregate(self.log)["s2"]
        self.assertEqual(stats["local_escalations"], 1)
        self.assertEqual(stats["jev_decisions"], 0)

    def test_jev_error_fallback_is_separate(self):
        self._write([
            _rec("s3", "user_turn", source="jev_error_fallback", action="REPLAN"),
        ])
        stats = session_report.aggregate(self.log)["s3"]
        self.assertEqual(stats["jev_error_fallbacks"], 1)

    def test_corrupt_lines_are_skipped(self):
        with open(self.log, "w", encoding="utf-8") as handle:
            handle.write("not json\n")
            handle.write(json.dumps(_rec("s4", "user_turn", source="jev",
                                         action="REPLAN")) + "\n")
            handle.write("{partial\n")
        sessions = session_report.aggregate(self.log)
        self.assertIn("s4", sessions)
        self.assertEqual(sessions["s4"]["jev_decisions"], 1)

    def test_render_text_includes_session_and_totals(self):
        self._write([
            _rec("s1", "user_turn", source="jev", action="REPLAN"),
            _rec("s1", "tool_step", source="lease", action="KEEP"),
        ])
        text = session_report.render_text(session_report.aggregate(self.log))
        self.assertIn("s1", text)
        self.assertIn("TOTAL", text)

    def test_empty_log_renders_hint(self):
        self._write([])
        text = session_report.render_text(session_report.aggregate(self.log))
        self.assertIn("No session records", text)

    def test_main_writes_reports(self):
        self._write([
            _rec("s1", "user_turn", source="jev", action="REPLAN"),
        ])
        json_out = os.path.join(self.tmp.name, "out.json")
        text_out = os.path.join(self.tmp.name, "out.txt")
        code = session_report.main([
            "--log", self.log, "--json-out", json_out, "--text-out", text_out])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(json_out))
        self.assertTrue(os.path.exists(text_out))
        with open(json_out, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertIn("s1", data)


if __name__ == "__main__":
    unittest.main()
