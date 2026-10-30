"""The channel adapters against local stand-ins: Direct Line, Responses API, A2A and chat completions.

No test contacts a host outside the local machine (`no_outside_network`). These tests show what each adapter does
against a stand-in. They do not show that an adapter works with a platform.
"""
import contextlib
import io
import unittest
from unittest import mock

from faias_harness.adapters import registry
from faias_harness.adapters.a2a import A2ATarget, norm_state, split_result
from faias_harness.adapters.base import CallBudget, ChannelError, host_entry
from faias_harness.adapters.directline import DirectLineTarget, activity_text
from faias_harness.adapters.openai_chat import OpenAIChatTarget
from faias_harness.adapters.openai_responses import ResponsesTarget
from faias_harness.egress import EgressClient
from faias_harness.errors import CapReached, EgressRefused
from tests.helpers import FAKE_KEY_VALUE, StandInModelHost, no_outside_network
from tests.standins import StandInA2A, StandInDirectLine, StandInJsonApp, StandInResponses

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

    def test_auth_scheme_defaults_to_bearer_for_authorization_and_none_for_another_header(self):
        url = "http://127.0.0.1:9"
        for header, expected in ((None, {"Authorization": f"Bearer {FAKE_KEY_VALUE}"}),
                                 ("X-Api-Key", {"X-Api-Key": FAKE_KEY_VALUE})):
            with self.subTest(header=header):
                options = {"version": "0.3"} if header is None else {"version": "0.3", "auth_header": header}
                t = A2ATarget(url, egress_for(url), options=options, api_key_env=KEY_ENV, env=ENV)
                self.assertEqual(t._headers(), expected)

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


class JsonHttpTests(unittest.TestCase):
    OPTS = {"request": {"conversationId": "{session_id}", "message": "{message}", "channel": "web",
                        "note": "from {session_id}"},
            "reply_path": "data.reply.text", "session_path": "data.conversationId",
            "blocked_path": "data.safety.blocked", "block_type_path": "data.safety.category",
            "model_path": "data.meta.model", "headers": {"X-Channel": "web"},
            "auth_header": "X-Api-Key"}

    def target(self, app, **changes):
        from faias_harness.adapters.json_http import JsonHttpTarget
        return JsonHttpTarget(app.base_url + "/api/chat", egress_for(app.base_url), options={**self.OPTS, **changes},
                              api_key_env=KEY_ENV, env=ENV, reply_timeout_s=5)

    def test_template_session_paths_and_custom_key_header(self):
        tricky = 'Say "hi"\nand {session_id} and {"x": 1}'
        with no_outside_network(), StandInJsonApp(lambda t, n: "Hello.", key_header="X-Api-Key", key=FAKE_KEY_VALUE) as app:
            t = self.target(app)
            sid = t.open_session()
            self.assertEqual(t.send(sid, tricky), "Hello.")
            first_conv = t.last_exchange["ids"]["session"]
            t.send(sid, "second")
        b1, b2 = app.requests[0]["body"], app.requests[1]["body"]
        self.assertEqual(b1["message"], tricky)            # the message is sent as it is: never filled again
        self.assertIsNone(b1["conversationId"])            # not yet known: null
        self.assertEqual(b1["note"], "from ")
        self.assertEqual(b2["conversationId"], first_conv)  # the server's session ID on the next turn
        self.assertEqual(b2["note"], f"from {first_conv}")
        self.assertEqual(app.auth_ok, [True, True])
        self.assertEqual(t.last_exchange["served_model"], "example-app-model")
        self.assertIsNone(t.last_exchange["blocked"])

    def test_blocked_missing_path_and_errors(self):
        def respond(text, nth):
            return {"blocked": "self_harm"} if text == "a" else {"status": 500} if text == "c" else "fine"
        with no_outside_network(), StandInJsonApp(respond, key_header="X-Api-Key", key=FAKE_KEY_VALUE) as app:
            t = self.target(app)
            sid = t.open_session()
            t.send(sid, "a")
            self.assertEqual((t.last_exchange["blocked"], t.last_exchange["block_type"]), (True, "self_harm"))
            t2 = self.target(app, reply_path="data.answer")
            with self.assertRaises(ChannelError) as cm:
                t2.send(t2.open_session(), "b")
            self.assertEqual(cm.exception.kind, "channel_error")
            with self.assertRaises(ChannelError) as cm:
                t.send(sid, "c")
            self.assertEqual(cm.exception.kind, "agent_error")
        with no_outside_network(), StandInJsonApp(key_header="X-Api-Key", key="another-one") as app:
            t = self.target(app)
            with self.assertRaises(ChannelError) as cm:
                t.send(t.open_session(), "x")
            self.assertEqual(cm.exception.kind, "auth")

    def test_harness_session_id_without_session_path(self):
        opts = {k: v for k, v in self.OPTS.items() if k != "session_path"}
        with no_outside_network(), StandInJsonApp(key_header="X-Api-Key", key=FAKE_KEY_VALUE) as app:
            from faias_harness.adapters.json_http import JsonHttpTarget
            t = JsonHttpTarget(app.base_url + "/api/chat", egress_for(app.base_url), options=opts,
                               api_key_env=KEY_ENV, env=ENV)
            sid = t.open_session()
            t.send(sid, "1")
            t.send(sid, "2")
        self.assertEqual(app.requests[0]["body"]["conversationId"], sid)
        self.assertEqual(app.requests[1]["body"]["conversationId"], sid)

    def test_option_rules(self):
        from faias_harness.adapters.json_http import get_path, validate_options
        self.assertEqual(validate_options(self.OPTS, {}), [])
        bad = [
            ({**self.OPTS, "request": {"text": "fixed"}}, "{message}"),
            ({**self.OPTS, "reply_path": "data..x"}, "reply_path"),
            ({**self.OPTS, "headers": {"X-Api-Key": "v"}}, "key header"),
            ({**self.OPTS, "method": "GET"}, "method"),
            ({**self.OPTS, "auth_header": "bad header"}, "auth_header"),
        ]
        for opts, needle in bad:
            with self.subTest(needle=needle):
                self.assertTrue(any(needle in p for p in validate_options(opts, {})))
        self.assertEqual(get_path({"a": [{"b": "x"}]}, "a.0.b"), "x")


class ReviewFixAdapterTests(unittest.TestCase):
    """Fixes from the review of 22 October 2026."""

    def test_list_body_negative_retry_after_and_cancelled(self):
        from faias_harness.adapters.a2a import norm_state
        self.assertEqual(norm_state("TASK_STATE_CANCELLED"), "canceled")
        calls = {"n": 0}

        def respond(text, nth):
            calls["n"] += 1
            return {"status": 429, "body": {}, "headers": {"Retry-After": "-5"}} if calls["n"] == 1 else \
                {"status": 200, "body": ["a", "list"]}
        sleeps = Sleeps()
        with no_outside_network(), StandInResponses(respond, key=FAKE_KEY_VALUE) as s:
            t = ResponsesTarget(s.base_url, egress_for(s.base_url), options={"body": {"model": "m"}},
                                api_key_env=KEY_ENV, env=ENV, sleep=sleeps)
            self.assertEqual(t.send(t.open_session(), "x"), "")     # a list body is not a response: no text, no crash
        self.assertEqual(sleeps.waits, [0.0])

    def test_directline_read_is_retried_without_charge(self):
        budget = CallBudget(1.0, 0.1)
        with no_outside_network(), StandInDirectLine(lambda t, n: "Answer.", secret=FAKE_KEY_VALUE) as dl:
            real = dl._get
            state = {"n": 0}

            def flaky(conv, query):
                state["n"] += 1
                return (503, {"error": {"code": "ServiceUnavailable"}}, {"Retry-After": "0"}) if state["n"] == 1 else real(conv, query)
            dl._get = flaky
            t = DirectLineTarget(dl.base_url, egress_for(dl.base_url), options={"auth": "secret", **FAST_DL},
                                 api_key_env=KEY_ENV, env=ENV, reply_timeout_s=5, before_send=budget.charge,
                                 sleep=Sleeps())
            self.assertEqual(t.send(t.open_session(), "q"), "Answer.")
        self.assertEqual(budget.calls, 1)
        self.assertIn("read_http_503", t.last_exchange["retry_reasons"])

    def test_a_reply_activity_with_no_reply_to_id_is_kept_and_recorded_apart(self):
        """A bot may not set replyToId, so such an activity is kept in the reply. It may also be a banner, so the
        exchange names it apart: its ID and the count."""
        class Banner(StandInDirectLine):
            def _post(self, cid, conv, body):
                status, out, headers = super()._post(cid, conv, body)
                if body.get("type") == "message":
                    conv["pending"][-1]["acts"].append({"type": "message", "from": {"id": "examplebot"},
                                                        "text": "Our branches close early on Friday."})
                return status, out, headers
        with no_outside_network(), Banner(lambda t, n: "Answer.", secret=FAKE_KEY_VALUE) as dl:
            t = DirectLineTarget(dl.base_url, egress_for(dl.base_url), options={"auth": "secret", **FAST_DL},
                                 api_key_env=KEY_ENV, env=ENV, reply_timeout_s=5)
            reply = t.send(t.open_session(), "q")
        ex = t.last_exchange
        self.assertEqual(reply, "Answer.\nOur branches close early on Friday.")
        self.assertEqual((len(ex["ids"]["replies"]), ex["replies_without_reply_to"]), (2, 1))
        self.assertEqual(ex["ids"]["replies_without_reply_to"], ex["ids"]["replies"][1:])
        with no_outside_network(), StandInDirectLine(lambda t, n: "Answer.", secret=FAKE_KEY_VALUE) as dl:
            t = DirectLineTarget(dl.base_url, egress_for(dl.base_url), options={"auth": "secret", **FAST_DL},
                                 api_key_env=KEY_ENV, env=ENV, reply_timeout_s=5)
            t.send(t.open_session(), "q")
        self.assertEqual((t.last_exchange["replies_without_reply_to"],
                          t.last_exchange["ids"]["replies_without_reply_to"]), (0, []))

    def test_directline_token_and_conversation_calls_retry_429_and_503_without_charge(self):
        """As for a send: at most two retries, with the Retry-After wait. These calls go to the Direct Line service,
        so they are not charged."""
        class Busy(StandInDirectLine):
            def __init__(self, busy, **kw):
                super().__init__(lambda t, n: "Answer.", secret=FAKE_KEY_VALUE, **kw)
                self.busy = busy

            def route(self, method, path, query, headers, body):
                if self.busy.get(path):
                    return self.busy[path].pop(0), {"error": {"code": "ServiceUnavailable"}}, {"Retry-After": "0"}
                return super().route(method, path, query, headers, body)
        conv = "/v3/directline/conversations"
        for auth, token_path, status in (("secret", "/v3/directline/tokens/generate", 503),
                                         ("token_endpoint", "/token", 429)):
            with self.subTest(auth=auth):
                budget, sleeps = CallBudget(1.0, 0.1), Sleeps()
                with no_outside_network(), Busy({token_path: [status], conv: [429, 503]}) as dl:
                    opts = {"auth": auth, **FAST_DL}
                    if auth == "token_endpoint":
                        opts["token_endpoint"] = dl.base_url + "/token"
                    t = DirectLineTarget(dl.base_url, egress_for(dl.base_url), options=opts,
                                         api_key_env=KEY_ENV if auth == "secret" else None, env=ENV, reply_timeout_s=5,
                                         before_send=budget.charge, sleep=sleeps)
                    sid = t.open_session()
                    self.assertEqual(t.last_exchange["retry_reasons"],
                                     [f"token_http_{status}", "conversation_http_429", "conversation_http_503"])
                    self.assertEqual((budget.calls, t.last_exchange["retries"], sleeps.waits), (0, 0, [0.0, 0.0, 0.0]))
                    self.assertEqual(t.send(sid, "q"), "Answer.")
                self.assertEqual(budget.calls, 1)
        with no_outside_network(), Busy({conv: [503, 503, 503]}) as dl:
            t = DirectLineTarget(dl.base_url, egress_for(dl.base_url), options={"auth": "secret", **FAST_DL},
                                 api_key_env=KEY_ENV, env=ENV, reply_timeout_s=5, sleep=Sleeps())
            with self.assertRaises(ChannelError) as cm:
                t.open_session()
        self.assertEqual((cm.exception.status, t.last_exchange["statuses"][-3:]), (503, [503, 503, 503]))

    def test_json_http_counts_only_a_non_empty_string_or_an_integer_as_a_session_id(self):
        import json
        import httpx
        from faias_harness.adapters.json_http import JsonHttpTarget
        for value, found in (("", False), (None, False), (False, False), (True, False), ({}, False), ([], False),
                             ({"id": "x"}, False), (0, True), (7, True), ("s-1", True)):
            with self.subTest(value=value):
                sent = []

                def handler(request, value=value):
                    sent.append(json.loads(request.content)["session_id"])
                    return httpx.Response(200, json={"reply": "OK.", "sid": value})
                egress = EgressClient(["127.0.0.1:9"], transport=httpx.MockTransport(handler))
                _CLIENTS.append(egress)
                t = JsonHttpTarget("http://127.0.0.1:9/chat", egress, options={
                    "request": {"message": "{message}", "session_id": "{session_id}"}, "reply_path": "reply",
                    "session_path": "sid"})
                sid = t.open_session()
                t.send(sid, "one")
                self.assertEqual(t.last_exchange["session_path_found"], found)
                t.send(sid, "two")
                self.assertEqual(sent, [None, value if found else None])
                self.assertEqual(t.server_ids(sid), {"session": value if found else None})
        t = JsonHttpTarget("http://127.0.0.1:9/chat", egress, options={"request": {"message": "{message}"},
                                                                        "reply_path": "reply"})
        self.assertEqual(t.server_ids(t.open_session()), {})        # the harness names the ID: no server ID

    def test_greeting_wait_is_bounded(self):
        from faias_harness.adapters import directline
        with no_outside_network(), StandInDirectLine(lambda t, n: "Answer.", greeting=None) as dl, \
                mock.patch.object(directline, "GREETING_WAIT_S", 0.2):
            t = DirectLineTarget(dl.base_url, egress_for(dl.base_url),
                                 options={"auth": "token_endpoint", "token_endpoint": dl.base_url + "/token",
                                          "start_event": True, **FAST_DL}, reply_timeout_s=30)
            import time as _t
            t0 = _t.monotonic()
            sid = t.open_session()
            self.assertLess(_t.monotonic() - t0, 5)
            self.assertEqual(t.send(sid, "q"), "Answer.")


class CookieApp(StandInJsonApp):
    """A json-http endpoint that keys its session by a cookie: it sets one when a request carries none, and keeps the
    Cookie header of each request it gets."""

    def __init__(self):
        super().__init__(lambda t, n: "Fine.")
        self.cookies, self._n = [], 0

    def route(self, method, path, query, headers, body):
        self.cookies.append(headers.get("Cookie"))
        status, out, extra = super().route(method, path, query, headers, body)
        if headers.get("Cookie") is None:
            self._n += 1
            extra = {**(extra or {}), "Set-Cookie": f"sess=s{self._n}; Path=/"}
        return status, out, extra


class CookieTests(unittest.TestCase):
    """One egress client serves a whole run. Each external adapter clears its cookies at open_session."""

    def test_a_cookie_set_in_session_1_goes_with_turn_2_and_not_with_session_2(self):
        from faias_harness.adapters.json_http import JsonHttpTarget
        with no_outside_network(), CookieApp() as app:
            t = JsonHttpTarget(app.base_url + "/api/chat", egress_for(app.base_url),
                               options={"request": {"message": "{message}"}, "reply_path": "data.reply.text"})
            s1 = t.open_session()
            t.send(s1, "one")
            t.send(s1, "two")
            t.close_session(s1)
            s2 = t.open_session()
            t.send(s2, "three")
        self.assertEqual(app.cookies, [None, "sess=s1", None])

    def test_every_external_adapter_clears_the_cookies_at_open_session(self):
        """Each adapter of the registry clears before any request of the session. The SRD-1 adapter does not."""
        from faias_harness.adapters import srd1_http
        opts = {"directline": {"auth": "token_endpoint", "token_endpoint": "http://127.0.0.1:9/token"},
                "openai-responses": {"body": {"model": "m"}}, "a2a": {}, "generic-http": {"model": "m"},
                "json-http": {"request": {"message": "{message}"}, "reply_path": "r"}}
        self.assertEqual(set(opts), set(registry.ADAPTERS))
        for adapter, o in opts.items():
            with self.subTest(adapter=adapter):
                egress = EgressClient(["127.0.0.1:9"])
                _CLIENTS.append(egress)
                order = []

                def stop(*a, **k):
                    order.append("request")
                    raise ChannelError("transport", "the test stops at the first request")
                with mock.patch.object(egress, "clear_cookies", side_effect=lambda: order.append("clear")), \
                        mock.patch.object(egress, "request", side_effect=stop):
                    t = registry.ADAPTERS[adapter].cls("http://127.0.0.1:9", egress, options=o)
                    try:
                        t.open_session()
                    except ChannelError:
                        pass
                self.assertEqual(order[:1], ["clear"], order)
        egress = EgressClient(["127.0.0.1:9"])
        _CLIENTS.append(egress)
        with mock.patch.object(egress, "clear_cookies") as clear, \
                mock.patch.object(egress, "request", return_value=mock.Mock(status_code=200,
                                                                            json=lambda: {"session_id": "x"})):
            self.assertEqual(srd1_http.Srd1ChatTarget("http://127.0.0.1:9", egress).open_session(), "x")
        clear.assert_not_called()

    def test_the_srd1_client_keeps_its_cookies(self):
        """The SRD-1 engines are not changed: their client keeps a cookie across sessions, as before."""
        import httpx
        from faias_harness.adapters import srd1_http
        seen = []

        def handler(request):
            seen.append(request.headers.get("Cookie"))
            return httpx.Response(200, json={"session_id": f"s{len(seen)}"}, headers={"Set-Cookie": "srd=1; Path=/"})
        egress = EgressClient(["127.0.0.1:9"], transport=httpx.MockTransport(handler))
        _CLIENTS.append(egress)
        t = srd1_http.Srd1ChatTarget("http://127.0.0.1:9", egress)
        t.open_session()
        t.open_session()
        self.assertEqual(seen, [None, "srd=1"])
