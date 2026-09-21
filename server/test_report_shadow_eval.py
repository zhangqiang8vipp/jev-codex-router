import unittest

import report_shadow_eval as report


class ShadowReport(unittest.TestCase):
    def turn(self, turn_id, smart_model, smart_effort, success=True, credits_usage=None):
        usage = credits_usage or {
            "input_tokens": 1000,
            "cached_input_tokens": 900,
            "output_tokens": 100,
        }
        return {
            "_at": report.datetime.datetime(2026, 9, 21, 12, 0, 0),
            "event": "turn",
            "turn_id": turn_id,
            "jev_route": {"model": smart_model, "effort": smart_effort},
            "smart_route": {"model": smart_model, "effort": smart_effort},
            "served_route": {"model": smart_model, "effort": smart_effort},
            "route_changed": False,
            "success": success,
            "retry_count": 0,
            "total_ms": 1000,
            "usage": usage,
            "attempts": [{
                "model": smart_model,
                "effort": smart_effort,
                "status": 200 if success else 500,
                "terminal_type": "response.completed" if success else "response.failed",
                "usage": usage,
            }],
        }

    def test_tool_error_penalizes_only_observed_smart_quality(self):
        turn = self.turn("a", "gpt-5.6-terra", "high")
        feedback = {"a": {"tool_error": True, "tool_success": False}}
        result = report.summarize([turn], feedback, {}, 7, 1)
        self.assertEqual(result["pairs"][0]["observed_success_rate"], 0.0)
        self.assertEqual(result["tool_error_rate"], 1.0)

    def test_pareto_uses_observed_smart_pairs(self):
        cheap = self.turn("a", "gpt-5.6-luna", "low")
        strong = self.turn("b", "gpt-5.6-sol", "medium")
        strong["_at"] = report.datetime.datetime(2026, 9, 21, 12, 1, 0)
        result = report.summarize([cheap, strong], {}, {}, 7, 1)
        self.assertIn("gpt-5.6-luna:low", result["pareto_frontier"])
        self.assertNotIn("gpt-5.6-sol:medium", result["pareto_frontier"])

    def test_partially_unpriced_retry_is_not_treated_as_full_actual_cost(self):
        turn = self.turn("a", "gpt-5.6-sol", "medium")
        turn["attempts"].append({
            "model": "external/unpriced",
            "status": 200,
            "terminal_type": "response.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 0,
                "output_tokens": 10,
            },
        })
        turn["retry_count"] = 1
        result = report.summarize([turn], {}, {}, 7, 1)
        row = result["pairs"][0]
        self.assertEqual(row["priced_turns"], 0)
        self.assertIsNone(row["avg_actual_credits"])

    def test_transition_cost_does_not_claim_counterfactual_quality(self):
        turn = self.turn("a", "gpt-5.6-sol", "medium")
        turn["jev_route"] = {"model": "gpt-5.6-terra", "effort": "high"}
        turn["route_changed"] = True
        result = report.summarize([turn], {}, {}, 7, 1)
        transition = result["transitions"][0]
        self.assertEqual(transition["from"], "gpt-5.6-terra:high")
        self.assertEqual(transition["to"], "gpt-5.6-sol:medium")
        self.assertIn("raw_estimated_credits", transition)
        self.assertNotIn("raw_quality", transition)


if __name__ == "__main__":
    unittest.main()
