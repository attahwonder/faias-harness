"""The channel adapters against local stand-ins: Direct Line, Responses API, A2A and chat completions.

No test contacts a host outside the local machine (`no_outside_network`). These tests show what each adapter does
against a stand-in. They do not show that an adapter works with a platform.
"""
import contextlib
import io
import unittest

from faias_harness.adapters import registry
from faias_harness.adapters.a2a import A2ATarget, norm_state, split_result
from faias_harness.adapters.base import CallBudget, ChannelError, host_entry
from faias_harness.adapters.directline import DirectLineTarget, activity_text
from faias_harness.adapters.openai_chat import OpenAIChatTarget
from faias_harness.adapters.openai_responses import ResponsesTarget
from faias_harness.egress import EgressClient
from faias_harness.errors import CapReached, EgressRefused
from tests.helpers import FAKE_KEY_VALUE, StandInModelHost, no_outside_network
from tests.standins import StandInA2A, StandInDirectLine, StandInResponses

KEY_ENV = "EXAMPLE_CHANNEL_KEY"
ENV = {KEY_ENV: FAKE_KEY_VALUE}
OTHER_VALUE = "not-the-expected-value"   # a value the stand-in expects, so the adapter's value is refused
FAST_DL = {"poll_ms": 50, "settle_ms": 0}


_CLIENTS = []


def egress_for(url):
    c = EgressClient([host_entry(url)])
    _CLIENTS.append(c)
    return c


def tearDownModule():
    for c in _CLIENTS:
        c.close()


class Sleeps:
    def __init__(self):
        self.waits = []

    def __call__(self, s):
        self.waits.append(s)


class DirectLineTests(unittest.TestCase):
    def target(self, dl, **opts):
        o = {"auth": "secret", **FAST_DL, **opts}
        return DirectLineTarget(dl.base_url, egress_for(dl.base_url), options=o,
                                api_key_env=KEY_ENV if o["auth"] == "secret" else None, env=ENV, reply_timeout_s=5)

    def test_secret_conversation_reply_and_exchange(self):
        with no_outside_network(), StandInDirectLine(lambda t, n: ["Part one.", "Part two."],
                                                     secret=FAKE_KEY_VALUE) as dl:
            t = self.target(dl)
            sid = t.open_session()
            reply = t.send(sid, "What is my balance?")
            t.close_session(sid)
        self.assertEqual(reply, "Part one.\nPart two.")
        ex = t.last_exchange
        self.assertEqual(ex["channel_host"], host_entry(dl.base_url))
        self.assertEqual(len(ex["ids"]["replies"]), 2)
        self.assertEqual(ex["activity_types"].get("typing"), 1)
        self.assertIsNone(ex["blocked"])            # Direct Line names no block
        self.assertEqual(dl.auth_ok, [True])
        self.assertNotIn(FAKE_KEY_VALUE, repr(ex))

    def test_token_endpoint_needs_no_key_and_drops_greeting(self):
        with no_outside_network(), StandInDirectLine(lambda t, n: "Answer.") as dl:
            t = self.target(dl, auth="token_endpoint", token_endpoint=dl.base_url + "/token", start_event=True)
            sid = t.open_session()
            reply = t.send(sid, "Hi")
        self.assertEqual(reply, "Answer.")           # the greeting was read and dropped

    def test_wrong_secret_is_an_auth_error(self):
        with no_outside_network(), StandInDirectLine(secret=OTHER_VALUE) as dl:
            with self.assertRaises(ChannelError) as cm:
                self.target(dl).open_session()
        self.assertEqual(cm.exception.kind, "auth")
        self.assertNotIn(FAKE_KEY_VALUE, str(cm.exception))

    def test_missing_key_variable(self):
        with no_outside_network(), StandInDirectLine(secret=FAKE_KEY_VALUE) as dl:
            t = DirectLineTarget(dl.base_url, egress_for(dl.base_url), options={"auth": "secret", **FAST_DL},
                                 api_key_env=KEY_ENV, env={}, reply_timeout_s=5)
            with self.assertRaises(ChannelError) as cm:
                t.open_session()
        self.assertEqual(cm.exception.kind, "key_missing")
        self.assertIn(KEY_ENV, str(cm.exception))

    def test_no_reply_is_a_timeout_with_empty_text(self):
        with no_outside_network(), StandInDirectLine(lambda t, n: None, secret=FAKE_KEY_VALUE) as dl:
            t = DirectLineTarget(dl.base_url, egress_for(dl.base_url), options={"auth": "secret", **FAST_DL},
                                 api_key_env=KEY_ENV, env=ENV, reply_timeout_s=0.3)
            reply = t.send(t.open_session(), "Hello?")
        self.assertEqual(reply, "")
        self.assertEqual(t.last_exchange["finish_reason"], "timeout")

    def test_bot_error_and_card_text_and_slow_reply(self):
        def respond(text, nth):
            return {"status": 502} if text == "break" else {"card": "Your fee is 5.00 GBP."}
        with no_outside_network(), StandInDirectLine(respond, secret=FAKE_KEY_VALUE, reply_after_polls=2) as dl:
            t = self.target(dl)
            sid = t.open_session()
            with self.assertRaises(ChannelError) as cm:
                t.send(sid, "break")
            reply = t.send(sid, "fee?")
        self.assertEqual(cm.exception.kind, "agent_error")
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual(reply, "Your fee is 5.00 GBP.")
        self.assertGreaterEqual(t.last_exchange["polls"], 3)

    def test_activity_text_reads_cards(self):
        a = {"text": "Top", "attachments": [{"content": {"type": "AdaptiveCard", "body": [
            {"type": "TextBlock", "text": "A"}, {"type": "FactSet", "facts": [{"title": "Fee", "value": "0"}]}]}}]}
        self.assertEqual(activity_text(a), "Top\nA\nFee\n0")

    def test_egress_refuses_a_host_off_the_list(self):
        with no_outside_network(), StandInDirectLine() as dl:
            t = DirectLineTarget(dl.base_url, egress_for("http://127.0.0.2:1"),
                                 options={"auth": "token_endpoint", "token_endpoint": dl.base_url + "/token", **FAST_DL},
                                 reply_timeout_s=5)
            with self.assertRaises(EgressRefused):
                t.open_session()


class ResponsesTests(unittest.TestCase):
    def target(self, s, **opts):
        o = {"body": {"agent": {"name": "example", "type": "agent_reference"}}, **opts}
        return ResponsesTarget(s.base_url + "/openai/v1", egress_for(s.base_url), options=o, api_key_env=KEY_ENV,
                               env=ENV, reply_timeout_s=5, sleep=Sleeps())

    def test_reply_model_usage_and_chaining(self):
        with no_outside_network(), StandInResponses(lambda t, n: "Hello there.", key=FAKE_KEY_VALUE) as s:
            t = self.target(s, query={"api-version": "v1"}, store=True)
            sid = t.open_session()
            r1 = t.send(sid, "one")
            r2 = t.send(sid, "two")
        self.assertEqual((r1, r2), ("Hello there.", "Hello there."))
        self.assertEqual(t.last_exchange["served_model"], "example-model-2026-01-01")
        self.assertEqual(t.last_exchange["usage"], {"input_tokens": 40, "output_tokens": 12})
        bodies = [r["body"] for r in s.requests]
        self.assertNotIn("previous_response_id", bodies[0])
        self.assertEqual(bodies[1]["previous_response_id"], "resp_1")
        self.assertEqual(bodies[0]["agent"], {"name": "example", "type": "agent_reference"})
        self.assertEqual(s.requests[0]["query"], "api-version=v1")
        self.assertEqual(s.auth_ok, [True, True])

    def test_refusal_content_filter_and_length(self):
        def respond(text, nth):
            return {"refusal": "I can't help with that."} if text == "a" else \
                {"status": 400, "body": {"error": {"code": "content_filter"}}} if text == "b" else \
                {"incomplete": "max_output_tokens", "text": "cut"}
        with no_outside_network(), StandInResponses(respond, key=FAKE_KEY_VALUE) as s:
            t = self.target(s)
            sid = t.open_session()
            out = {}
            for x in "abc":
                out[x] = (t.send(sid, x), dict(t.last_exchange))
        self.assertEqual((out["a"][1]["blocked"], out["a"][1]["block_type"]), (True, "refusal"))
        self.assertEqual((out["b"][0], out["b"][1]["blocked"], out["b"][1]["block_type"]), ("", True, "content_filter"))
        self.assertEqual((out["c"][0], out["c"][1]["finish_reason"]), ("cut", "length"))

    def test_429_is_retried_and_each_attempt_is_charged(self):
        calls = {"n": 0}

        def respond(text, nth):
            calls["n"] += 1
            return {"status": 429, "body": {}, "headers": {"Retry-After": "3"}} if calls["n"] == 1 else "ok"
        budget = CallBudget(1.0, 0.1)
        with no_outside_network(), StandInResponses(respond, key=FAKE_KEY_VALUE) as s:
            sleeps = Sleeps()
            t = ResponsesTarget(s.base_url, egress_for(s.base_url), options={"body": {"model": "m"}},
                                api_key_env=KEY_ENV, env=ENV, sleep=sleeps, before_send=budget.charge)
            self.assertEqual(t.send(t.open_session(), "x"), "ok")
        self.assertEqual((t.last_exchange["retries"], t.last_exchange["retry_reasons"]), (1, ["http_429"]))
        self.assertEqual(sleeps.waits, [3.0])
        self.assertEqual(budget.calls, 2)

    def test_budget_stops_before_the_call(self):
        budget = CallBudget(0.25, 0.1)              # floor(0.25 / 0.1) = 2 calls
        with no_outside_network(), StandInResponses(key=FAKE_KEY_VALUE) as s:
            t = ResponsesTarget(s.base_url, egress_for(s.base_url), options={"body": {"model": "m"}},
                                api_key_env=KEY_ENV, env=ENV, before_send=budget.charge)
            sid = t.open_session()
            t.send(sid, "1")
            t.send(sid, "2")
            with self.assertRaises(CapReached):
                t.send(sid, "3")
        self.assertEqual(len(s.requests), 2)

    def test_401_is_an_auth_error(self):
        with no_outside_network(), StandInResponses(key="another-value") as s:
            t = self.target(s)
            with self.assertRaises(ChannelError) as cm:
                t.send(t.open_session(), "x")
        self.assertEqual(cm.exception.kind, "auth")


class A2ATests(unittest.TestCase):
    def target(self, s, version):
        return A2ATarget(s.base_url, egress_for(s.base_url), options={"version": version, "poll_ms": 100},
                         api_key_env=KEY_ENV, env=ENV, reply_timeout_s=5, sleep=Sleeps())

    def test_both_versions_task_and_message(self):
        for version in ("1.0", "0.3"):
            for mode in ("task", "message", "working"):
                with self.subTest(version=version, mode=mode), no_outside_network(), \
                        StandInA2A(lambda t, n: "Reply text.", mode=mode, key=FAKE_KEY_VALUE) as s:
                    t = self.target(s, version)
                    sid = t.open_session()
                    self.assertEqual(t.send(sid, "first"), "Reply text.")
                    ctx = t.last_exchange["ids"]["context"]
                    t.send(sid, "second")
                    sends = [r for r in s.requests if r["body"]["method"] in ("SendMessage", "message/send")]
                    self.assertEqual(sends[-1]["body"]["params"]["message"]["contextId"], ctx)
                    self.assertEqual(s.versions, [version, version])
                    method = s.requests[0]["body"]["method"]
                    self.assertEqual(method, "SendMessage" if version == "1.0" else "message/send")
                    if mode == "working":
                        self.assertEqual(t.last_exchange["task_reads"], 1)
                    part = s.requests[0]["body"]["params"]["message"]["parts"][0]
                    self.assertEqual(part, {"text": "first"} if version == "1.0" else {"kind": "text", "text": "first"})

    def test_rejected_is_blocked(self):
        with no_outside_network(), StandInA2A(lambda t, n: "I will not do that.", mode="rejected", key=FAKE_KEY_VALUE) as s:
            t = self.target(s, "1.0")
            reply = t.send(t.open_session(), "x")
        self.assertEqual(reply, "I will not do that.")
        self.assertEqual((t.last_exchange["blocked"], t.last_exchange["block_type"]), (True, "a2a_rejected"))

    def test_helpers(self):
        self.assertEqual(norm_state("TASK_STATE_INPUT_REQUIRED"), "input-required")
        self.assertEqual(norm_state("completed"), "completed")
        self.assertEqual(split_result({"task": {"id": "1"}})[0], "task")
        self.assertEqual(split_result({"kind": "message", "parts": []})[0], "message")
        self.assertEqual(split_result({"id": "1", "status": {}})[0], "task")


class ChatCompletionsTests(unittest.TestCase):
    def test_generic_http_through_egress(self):
        with no_outside_network(), StandInModelHost(lambda t, n: "Hello.") as host:
            t = OpenAIChatTarget(host.base_url, egress_for(host.base_url), model_id="example-model",
                                 api_key_env=KEY_ENV, env=ENV)
            sid = t.open_session()
            self.assertEqual(t.send(sid, "hi"), "Hello.")
            t.send(sid, "again")
        self.assertEqual(len(host.requests[1]["body"]["messages"]), 3)      # history kept within the session
        self.assertTrue(all(r["has_auth"] for r in host.requests))
        self.assertEqual(t.last_exchange["served_model"], "example-model")


class RegistryTests(unittest.TestCase):
    def test_option_problems(self):
        from faias_harness.adapters import directline, openai_responses
        self.assertTrue(directline.validate_options({"auth": "secret"}, {}))       # no key variable named
        self.assertFalse(directline.validate_options({"auth": "secret"}, {"api_key_env": "X"}))
        self.assertTrue(directline.validate_options({"auth": "token_endpoint"}, {}))
        self.assertTrue(openai_responses.validate_options({"body": {"model": "m", "input": "x"}}, {}))
        self.assertTrue(openai_responses.validate_options({"body": {}}, {}))
        self.assertIn("srd1-http", registry.problems(type("P", (), {"adapter": "srd1-http"})())[0])


if __name__ == "__main__":
    unittest.main()
