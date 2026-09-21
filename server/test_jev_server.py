"""Unit tests for the decisions jev_server makes on every call.

The server itself is a long-lived process on loopback, so these tests hold the
parts that do not need it: key loading, installation readiness, response-stream
continuity, routing-policy decisions, and bounded Jev task construction.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import jev_server as jev


class KeyLoading(unittest.TestCase):
    def test_explicit_env_file_wins_without_exposing_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jev.env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("TYPESAFE_API_KEY=file-secret\n")
            with mock.patch.dict(os.environ, {
                "JEV_ENV_FILE": path,
                "TYPESAFE_API_KEY": "process-secret",
            }, clear=False):
                self.assertEqual(jev.key_paths()[0], os.path.realpath(path))
                self.assertEqual(jev.load_key(), "file-secret")

    def test_process_env_is_last_resort(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing.env")
            with mock.patch.object(jev, "ENV_PATH", missing), \
                 mock.patch.object(jev, "LEGACY_ENV_PATH", missing + ".legacy"), \
                 mock.patch.dict(os.environ, {
                     "JEV_ENV_FILE": missing + ".override",
                     "TYPESAFE_API_KEY": "process-secret",
                 }, clear=False):
                self.assertEqual(jev.load_key(), "process-secret")


class InstallationCheck(unittest.TestCase):
    class FakeResponse:
        status = 200

        def __init__(self, body):
            self.body = body

        def getheader(self, name):
            if name.lower() == "content-length":
                return str(len(self.body))
            return None

        def read(self, size=None):
            return self.body if size is None else self.body[:size]

    class FakeConnection:
        def __init__(self, *_args, **_kwargs):
            self.path = ""

        def request(self, _method, path, **_kwargs):
            self.path = path

        def getresponse(self):
            if "/_codex-router/" in self.path and self.path.endswith("/v1/models"):
                body = json.dumps({"data": [{"id": jev.VIRTUAL_MODEL_SLUG}]}).encode()
            else:
                body = b'{"ok":true}'
            return InstallationCheck.FakeResponse(body)

        def close(self):
            pass

    def dependencies(self):
        return (
            mock.patch.object(jev, "load_key", return_value="typesafe-secret"),
            mock.patch.object(
                jev,
                "call_jev_routed",
                return_value={"answers": {"ready": {"probability": 1.0}}},
            ),
            mock.patch.object(jev, "caller_secret", return_value="caller-secret"),
            mock.patch.object(jev.http.client, "HTTPConnection", self.FakeConnection),
        )

    def test_check_verifies_dependencies_and_loaded_model_without_exposing_secrets(self):
        patches = self.dependencies()
        with patches[0], patches[1], patches[2], patches[3]:
            result = jev.installation_check(require_model=True)
        self.assertTrue(result["ok"])
        encoded = json.dumps(result)
        self.assertNotIn("typesafe-secret", encoded)
        self.assertNotIn("caller-secret", encoded)
        self.assertEqual(
            [item["name"] for item in result["checks"]],
            ["typesafe_key", "typesafe_api", "caller_secret", "codex_router", "jev_model"],
        )

    def test_core_check_does_not_require_curated_model(self):
        patches = self.dependencies()
        with patches[0], patches[1], patches[2], patches[3]:
            result = jev.installation_check(require_model=False)
        self.assertTrue(result["ok"])
        self.assertNotIn("jev_model", [item["name"] for item in result["checks"]])

    def test_readiness_accepts_a_catalog_larger_than_the_old_512k_cap(self):
        filler = "x" * (600 * 1024)
        body = json.dumps({
            "data": [
                {"id": "native/filler", "description": filler},
                {"id": jev.VIRTUAL_MODEL_SLUG},
            ]
        }).encode()

        class LargeCatalogConnection(self.FakeConnection):
            def getresponse(self_inner):
                if "/_codex-router/" in self_inner.path and self_inner.path.endswith("/v1/models"):
                    return InstallationCheck.FakeResponse(body)
                return InstallationCheck.FakeResponse(b'{"ok":true}')

        patches = self.dependencies()
        with patches[0], patches[1], patches[2], mock.patch.object(
            jev.http.client, "HTTPConnection", LargeCatalogConnection
        ):
            result = jev.installation_check(require_model=True)

        self.assertTrue(result["ok"])
        model_check = next(item for item in result["checks"] if item["name"] == "jev_model")
        self.assertEqual(model_check["detail"], "jev/auto loaded")

    def test_bounded_reader_rejects_an_oversized_catalog_explicitly(self):
        body = b"x" * (jev.MODEL_CATALOG_MAX_BYTES + 1)
        response = self.FakeResponse(body)
        with self.assertRaisesRegex(ValueError, "too large"):
            jev.read_bounded_response(response, jev.MODEL_CATALOG_MAX_BYTES)


class RouteDecisionCache(unittest.TestCase):
    def setUp(self):
        with jev._route_cache_lock:
            jev._route_cache.clear()
            jev._route_flights.clear()

    def tearDown(self):
        with jev._route_cache_lock:
            jev._route_cache.clear()
            jev._route_flights.clear()

    def test_identical_retry_reuses_one_paid_judgement(self):
        answer = {"answers": {"route": {"choice": "gpt-5.6-sol:high"}}}
        with mock.patch.object(jev, "call_jev_routed", return_value=answer) as call:
            first, first_mode = jev.call_jev_for_route("key", {"task": "x"}, b"same-request")
            second, second_mode = jev.call_jev_for_route("key", {"task": "changed-session"}, b"same-request")
        self.assertIs(first, answer)
        self.assertIs(second, answer)
        self.assertEqual((first_mode, second_mode), ("miss", "hit"))
        self.assertEqual(call.call_count, 1)

    def test_failed_judgement_is_not_cached_for_a_later_retry(self):
        answer = {"answers": {"route": {"choice": "gpt-5.6-sol:high"}}}
        with mock.patch.object(
            jev, "call_jev_routed", side_effect=[RuntimeError("temporary"), answer]
        ) as call:
            with self.assertRaisesRegex(RuntimeError, "temporary"):
                jev.call_jev_for_route("key", {"task": "x"}, b"same-request")
            result, mode = jev.call_jev_for_route("key", {"task": "x"}, b"same-request")
        self.assertIs(result, answer)
        self.assertEqual(mode, "miss")
        self.assertEqual(call.call_count, 2)

    def test_concurrent_identical_retries_are_single_flighted(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []
        answer = {"answers": {"route": {"choice": "gpt-5.6-terra:medium"}}}

        def paid_call(*_args, **_kwargs):
            calls.append(1)
            entered.set()
            release.wait(2)
            return answer

        results = []
        errors = []

        def worker():
            try:
                results.append(
                    jev.call_jev_for_route("key", {"task": "x"}, b"same-concurrent-request")
                )
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(jev, "call_jev_routed", side_effect=paid_call):
            first = threading.Thread(target=worker)
            second = threading.Thread(target=worker)
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            time.sleep(0.05)
            release.set()
            first.join(2)
            second.join(2)

        self.assertFalse(errors)
        self.assertEqual(len(calls), 1)
        self.assertEqual({mode for _result, mode in results}, {"miss", "coalesced"})


class TerminalQuotaTranslation(unittest.TestCase):
    def test_native_usage_limit_becomes_a_terminal_sse_quota_code(self):
        body = json.dumps({
            "error": {
                "type": "usage_limit_reached",
                "message": "The usage limit has been reached",
                "plan_type": "pro",
            }
        }).encode()
        error = jev.terminal_quota_error(429, {}, body)
        self.assertEqual(error["code"], "insufficient_quota")
        self.assertEqual(error["message"], "The usage limit has been reached")

        wire = jev.quota_failure_sse(error).decode("utf-8")
        payload = json.loads(next(
            line[6:] for line in wire.splitlines() if line.startswith("data: ")
        ))
        self.assertEqual(payload["type"], "response.failed")
        self.assertEqual(payload["response"]["error"]["code"], "insufficient_quota")

    def test_transient_rate_limit_stays_retryable_http_429(self):
        body = json.dumps({
            "error": {
                "type": "rate_limit_error",
                "code": "rate_limit_exceeded",
                "message": "Please try again in 2s",
            }
        }).encode()
        self.assertIsNone(jev.terminal_quota_error(429, {}, body))

    def test_codex_hard_stop_header_is_terminal_even_with_sparse_body(self):
        headers = {"x-codex-rate-limit-reached-type": "workspace_owner_credits_depleted"}
        error = jev.terminal_quota_error(429, headers, b'{"error":{"message":"limit"}}')
        self.assertEqual(error["code"], "insufficient_quota")


class ResponseIdContinuity(unittest.TestCase):
    """One response id per relayed stream, however many gateways touched it.

    A relayed turn crosses the local caller edge, which can re-encode response
    ids, so the terminal event of the stream we receive may repeat the id under a
    fresh encoding. The Responses transform in front of the router requires the
    completion to retain the id announced by response.created.
    """

    CREATED = b'data: {"type":"response.created","response":{"id":"resp_created"}}\n\n'
    DONE = b"data: [DONE]\n\n"

    def relay(self, *frames):
        markerer = jev.SummaryMarker(" \u00b7 \U0001f9e0sol:low \u00b7 ")
        return "".join(markerer.feed(frame) for frame in frames) + markerer.flush()

    def response_ids(self, stream):
        ids = []
        for line in stream.splitlines():
            if not line.startswith("data: ") or line[6:].strip() == "[DONE]":
                continue
            event = json.loads(line[6:])
            response = event.get("response")
            if isinstance(response, dict) and "id" in response:
                ids.append(response["id"])
        return ids

    def test_a_re_encoded_completion_keeps_the_announced_id(self):
        completed = (
            b'data: {"type":"response.completed","response":{"id":"resp_re-encoded","output":[]}}\n\n'
        )
        stream = self.relay(self.CREATED, completed, self.DONE)
        self.assertEqual(self.response_ids(stream), ["resp_created", "resp_created"])
        self.assertIn("data: [DONE]", stream)

    def test_every_terminal_event_is_rewritten_onto_the_announced_id(self):
        for terminal in ("response.completed", "response.incomplete", "response.failed"):
            frame = (
                'data: {"type":"%s","response":{"id":"resp_other","output":[]}}\n\n' % terminal
            ).encode()
            stream = self.relay(self.CREATED, frame)
            self.assertEqual(self.response_ids(stream), ["resp_created", "resp_created"], terminal)

    def test_a_stream_without_a_created_event_is_left_to_its_own_id(self):
        completed = (
            b'data: {"type":"response.completed","response":{"id":"resp_alone","output":[]}}\n\n'
        )
        stream = self.relay(completed)
        self.assertEqual(self.response_ids(stream), ["resp_alone"])


class Policy(unittest.TestCase):
    def test_a_low_confidence_user_turn_keeps_the_jev_choice(self):
        model, effort, speed, gate = jev.route(jev.LUNA, "low", 0.1, {"step_type": "user_turn"})
        self.assertEqual((model, effort, speed, gate), (jev.LUNA, "low", "default", "apply"))

    def test_a_clean_mechanical_step_keeps_luna_and_its_decided_depth(self):
        model, effort, speed, gate = jev.route(jev.LUNA, "low", 0.1, {"step_type": "tool_step"})
        self.assertEqual((model, effort, speed, gate), (jev.LUNA, "low", "default", "apply"))

    def test_a_confident_verdict_is_applied_as_given(self):
        model, effort, speed, gate = jev.route(jev.ASTRA, "max", 0.9, {"step_type": "user_turn"})
        self.assertEqual((model, effort, speed, gate), (jev.ASTRA, "max", "default", "apply"))

    def test_an_invalid_depth_does_not_silently_change_the_jev_choice(self):
        with self.assertRaises(ValueError):
            jev.route(jev.SOL, "nonsense", 0.9, {"step_type": "user_turn"})


class JevTaskInput(unittest.TestCase):
    """Jev judges the current ask: not the thread, and not Codex's own blocks.

    A turn carries machine-generated envelopes (goal context, plugin catalog,
    environment) that are far longer than the 500 chars Jev was calibrated on,
    and Codex appends some of them *after* the user's text. Clipping the raw head
    sent Jev nothing but the envelope on 1 291 of 4 516 live calls, so the task
    is unwrapped and clipped head+tail instead.
    """

    ASK = "Corrige le parseur de drift.test.ts, puis relance le backtest complet."
    GOAL = ('<codex_internal_context source="goal">\n'
            "Continue working toward the active thread goal.\n\n"
            "The objective below is a short navigation aid. "
            + ("navigation aid. " * 300) + "\n</codex_internal_context>")
    ENV = "<environment_context>\n<cwd>/Users/x/project</cwd>\n</environment_context>"
    PLUGINS = ("<recommended_plugins>\nHere is a list of plugins that are available "
               "but not installed.\n" + ("- Some Plugin (some-plugin@openai-curated-remote)\n" * 40)
               + "</recommended_plugins>")

    def user_item(self, text):
        return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}

    def task(self, text):
        return jev.extract({"input": [self.user_item(text)]})[0]

    def state(self, thread):
        payload = {"input": thread}
        task, prev_assistant, signals = jev.extract(payload)
        return jev.jev_state(task, prev_assistant, signals, jev.classify(payload))

    def test_a_goal_block_does_not_hide_the_ask(self):
        task = self.task(self.GOAL + "\n\n" + self.ASK)
        self.assertIn("Corrige le parseur", task)
        self.assertNotIn("codex_internal_context", task)

    def test_an_appended_environment_block_is_dropped(self):
        task = self.task(self.ASK + "\n" + self.ENV)
        self.assertIn("relance le backtest", task)
        self.assertNotIn("environment_context", task)

    def test_a_goal_only_turn_keeps_the_objective_and_loses_the_tags(self):
        task = self.task(self.GOAL)
        self.assertIn("Continue working toward the active thread goal.", task)
        self.assertNotIn("codex_internal_context", task)

    def test_an_envelope_without_a_request_gives_jev_nothing(self):
        """A catalog-only turn has no ask in it: the caller fails open."""
        self.assertEqual(self.task(self.PLUGINS), "")
        self.assertEqual(self.task(self.ENV), "")

    def test_the_tail_of_a_long_prompt_survives_the_clip(self):
        task = self.task("Contexte. " * 900 + self.ASK)
        self.assertIn("relance le backtest complet.", task)
        self.assertLessEqual(len(task), jev.TASK_CHARS)
        self.assertIn(jev.TASK_CLIP_MARK.strip(), task)

    def test_a_short_prompt_is_sent_untouched(self):
        self.assertEqual(self.task(self.ASK), self.ASK)

    def test_the_thread_length_never_reaches_jev(self):
        history = [self.user_item("Question %d" % i) for i in range(300)]
        assistant = {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "Reponse"}]}
        short = self.state([assistant, self.user_item(self.ASK)])
        long_thread = self.state(history + [assistant, self.user_item(self.ASK)])
        self.assertEqual(short, long_thread)

    def test_only_the_last_tool_output_travels_and_only_as_a_digest(self):
        output = {"type": "function_call_output", "output": "ok\n" + ("ligne\n" * 5_000)}
        state = self.state([self.user_item(self.ASK), output])
        tail = state["step"]["last_tool_output_tail"]
        self.assertEqual(state["step"]["type"], "tool_step")
        self.assertEqual(len(tail), jev.DIGEST_CHARS)
        self.assertNotIn("contains_error", state["step"])

    def test_the_tool_name_is_linked_by_call_id_without_sending_arguments(self):
        state = self.state([
            self.user_item(self.ASK),
            {"type": "function_call", "call_id": "call_1",
             "name": "exec_command", "arguments": "private arguments"},
            {"type": "function_call_output", "call_id": "call_1", "output": "done"},
        ])
        self.assertEqual(state["step"]["tool_call"], {"name": "exec_command"})
        self.assertNotIn("private arguments", json.dumps(state))

    def test_the_assistant_side_is_bounded(self):
        assistant = {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "a" * 4_000}]}
        state = self.state([self.user_item(self.ASK), assistant])
        self.assertEqual(len(state["previous_assistant"]), 240)


class RuntimeSafety(unittest.TestCase):
    def setUp(self):
        with jev._BREAKER_LOCK:
            jev._BREAKER.clear()
        jev._NATIVE_REDIRECT_DEPTH = 0

    def tearDown(self):
        with jev._BREAKER_LOCK:
            jev._BREAKER.clear()
        jev._NATIVE_REDIRECT_DEPTH = 0

    def test_breaker_never_wraps_from_astra_to_a_lower_tier(self):
        with mock.patch.object(jev.time, "time", return_value=100.0):
            jev.breaker_record(jev.ASTRA, False)
            jev.breaker_record(jev.ASTRA, False)
            model, rerouted = jev.breaker_pick(jev.ASTRA)
        self.assertIsNone(model)
        self.assertTrue(rerouted)

    def test_only_one_half_open_probe_is_claimed(self):
        with mock.patch.object(jev.time, "time", return_value=100.0):
            jev.breaker_record(jev.LUNA, False)
            jev.breaker_record(jev.LUNA, False)
        with mock.patch.object(jev.time, "time", return_value=161.0):
            self.assertTrue(jev.breaker_available(jev.LUNA))
            self.assertFalse(jev.breaker_available(jev.LUNA))
            jev.breaker_release(jev.LUNA)
            self.assertTrue(jev.breaker_available(jev.LUNA))

    def test_failed_half_open_probe_reopens_for_a_full_cooldown(self):
        with mock.patch.object(jev.time, "time", return_value=100.0):
            jev.breaker_record(jev.LUNA, False)
            jev.breaker_record(jev.LUNA, False)
        with mock.patch.object(jev.time, "time", return_value=161.0):
            self.assertTrue(jev.breaker_available(jev.LUNA))
            jev.breaker_record(jev.LUNA, False)
        with mock.patch.object(jev.time, "time", return_value=200.0):
            self.assertFalse(jev.breaker_available(jev.LUNA))
        with mock.patch.object(jev.time, "time", return_value=222.0):
            self.assertTrue(jev.breaker_available(jev.LUNA))

    def test_native_redirect_recovers_after_interrupted_suppression(self):
        with tempfile.TemporaryDirectory() as state, mock.patch.object(jev, "STATE", state):
            path, held = jev._native_redirect_paths()
            with open(held, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "model": "jev/auto"}, fh)
            self.assertTrue(jev.recover_native_redirect())
            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(held))

    def test_native_redirect_does_not_overwrite_a_newer_operator_choice(self):
        with tempfile.TemporaryDirectory() as state, mock.patch.object(jev, "STATE", state):
            path, held = jev._native_redirect_paths()
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "model": "jev/auto"}, fh)
            with jev.native_redirect_suppressed():
                self.assertFalse(os.path.exists(path))
                self.assertTrue(os.path.exists(held))
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"version": 1, "model": "native/operator-choice"}, fh)
            with open(path, encoding="utf-8") as fh:
                restored = json.load(fh)
            self.assertEqual(restored["model"], "native/operator-choice")
            self.assertFalse(os.path.exists(held))

    def test_deadline_timeout_is_capped_by_remaining_budget(self):
        with mock.patch.object(jev.time, "monotonic", return_value=10.0):
            self.assertEqual(jev._bounded_timeout(12.5, 30.0), 2.5)
            self.assertEqual(jev._bounded_timeout(100.0, 30.0), 30.0)
            with self.assertRaises(TimeoutError):
                jev._bounded_timeout(9.0, 30.0)


    def test_request_specific_4xx_releases_half_open_probe_without_reopening(self):
        with mock.patch.object(jev.time, "time", return_value=100.0):
            jev.breaker_record(jev.LUNA, False)
            jev.breaker_record(jev.LUNA, False)
        with mock.patch.object(jev.time, "time", return_value=161.0):
            self.assertTrue(jev.breaker_available(jev.LUNA))
            jev.breaker_finish(jev.LUNA, 400)
            self.assertTrue(jev.breaker_available(jev.LUNA))

    def test_retryable_429_reopens_half_open_probe(self):
        with mock.patch.object(jev.time, "time", return_value=100.0):
            jev.breaker_record(jev.LUNA, False)
            jev.breaker_record(jev.LUNA, False)
        with mock.patch.object(jev.time, "time", return_value=161.0):
            self.assertTrue(jev.breaker_available(jev.LUNA))
            jev.breaker_finish(jev.LUNA, 429)
        with mock.patch.object(jev.time, "time", return_value=200.0):
            self.assertFalse(jev.breaker_available(jev.LUNA))

    def test_terminal_quota_releases_probe_without_marking_model_healthy(self):
        with mock.patch.object(jev.time, "time", return_value=100.0):
            jev.breaker_record(jev.LUNA, False)
            jev.breaker_record(jev.LUNA, False)
        with mock.patch.object(jev.time, "time", return_value=161.0):
            self.assertTrue(jev.breaker_available(jev.LUNA))
            jev.breaker_finish(jev.LUNA, 200, quota_hit=True)
            self.assertTrue(jev.breaker_available(jev.LUNA))


if __name__ == "__main__":
    unittest.main()
