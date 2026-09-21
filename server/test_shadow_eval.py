import unittest

import shadow_eval as shadow


class UsageAggregation(unittest.TestCase):
    def test_sums_usage_and_cache_ratio_across_retries(self):
        attempts = [
            {"usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10}},
            {"usage": {"input_tokens": 50, "cached_input_tokens": 20, "output_tokens": 5}},
            {"usage": None},
        ]
        usage, known, unknown = shadow.aggregate_usage(attempts)
        self.assertEqual((known, unknown), (2, 1))
        self.assertEqual(usage["input_tokens"], 150)
        self.assertEqual(usage["cached_input_tokens"], 100)
        metrics = shadow.cache_metrics(usage)
        self.assertAlmostEqual(metrics["cache_hit_ratio"], 100 / 150)


class Outcomes(unittest.TestCase):
    def test_completed_is_success(self):
        outcome, success = shadow.classify_outcome(
            200, [{"terminal_type": "response.completed"}]
        )
        self.assertEqual(outcome, "completed")
        self.assertTrue(success)

    def test_incomplete_is_not_success(self):
        outcome, success = shadow.classify_outcome(
            200, [{"terminal_type": "response.incomplete"}]
        )
        self.assertEqual(outcome, "incomplete")
        self.assertFalse(success)

    def test_http_error_is_not_success(self):
        outcome, success = shadow.classify_outcome(500, [])
        self.assertEqual(outcome, "http_error")
        self.assertFalse(success)


class EventShape(unittest.TestCase):
    def test_turn_event_separates_jev_smart_and_served_routes(self):
        event = shadow.build_turn_event(
            at="2026-09-21T12:00:00",
            turn_id="abc",
            session="hashed",
            policy_version="p1",
            jev_model="gpt-5.6-terra",
            jev_effort="high",
            smart_model="gpt-5.6-sol",
            smart_effort="medium",
            smart_gate="failure_floor_2",
            served_model="gpt-5.6-sol",
            served_effort="medium",
            status=200,
            attempts=[{
                "model": "gpt-5.6-sol",
                "effort": "medium",
                "status": 200,
                "terminal_type": "response.completed",
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 900,
                    "output_tokens": 100,
                },
            }],
            total_ms=1200,
            jev_ms=100,
            step_type="user_turn",
            route_source="jev",
            route_reason="new_user_turn",
            jev_cache="miss",
        )
        self.assertTrue(event["route_changed"])
        self.assertEqual(event["jev_route"]["effort"], "high")
        self.assertEqual(event["smart_route"]["effort"], "medium")
        self.assertEqual(event["attempt_count"], 1)
        self.assertEqual(event["retry_count"], 0)
        self.assertEqual(event["route_source"], "jev")
        self.assertEqual(event["route_reason"], "new_user_turn")
        self.assertEqual(event["jev_cache"], "miss")
        self.assertTrue(event["success"])

    def test_feedback_carries_no_prompt_data(self):
        event = shadow.build_tool_feedback(
            at="2026-09-21T12:01:00",
            turn_id="abc",
            session="hashed",
            errored=True,
        )
        self.assertEqual(event["event"], "tool_feedback")
        self.assertTrue(event["tool_error"])
        self.assertNotIn("task", event)


if __name__ == "__main__":
    unittest.main()