"""S3: the gateway, the egress allow-list and the spend cap, against mocked servers. No outside network."""
import json
import unittest

import httpx

from faias_harness.egress import EgressClient, EgressLog
from faias_harness.errors import CapReached, EgressRefused, GatewayError
from targets.srd1.app.gateway import CapMeter, Gateway, estimate_tokens, host_matches
from tests.helpers import FAKE_KEY_VALUE, KEY_ENV, make_settings, no_outside_network

ENV = {KEY_ENV: FAKE_KEY_VALUE}
BASE = "https://openrouter.ai/api/v1"
MSGS = [{"role": "system", "content": "rules"}, {"role": "user", "content": "hello"}]


def model_response(provider="ExampleHost", model="example-vendor/example-model", usage=(30, 20), content="ok",
                   status=200):
    body = {"id": "g1", "model": model, "choices": [{"message": {"role": "assistant", "content": content}}]}
    if provider is not None:
        body["provider"] = provider
    if usage is not None:
        body["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1]}
    return httpx.Response(status, json=body)


class Recorder:
    def __init__(self, response=None):
        self.requests = []
        self.response = response if response is not None else (lambda request: model_response())

    def __call__(self, request):
        self.requests.append(request)
        r = self.response
        return r(request) if callable(r) else r


def make_gateway(rec, settings=None, base_url=BASE, env=ENV, allow=("openrouter.ai",)):
    log = EgressLog()
    client = EgressClient(allow, log=log, transport=httpx.MockTransport(rec))
    return Gateway(settings or make_settings(), client, env=env, base_url=base_url, sleep=lambda s: None), log


class RoutingLockTests(unittest.TestCase):
    def test_every_request_carries_the_five_routing_locks_and_the_pinned_model(self):
        rec = Recorder()
        gw, _ = make_gateway(rec)
        with no_outside_network():
            gw.complete(MSGS)
            gw.complete(MSGS, tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}])
        self.assertEqual(len(rec.requests), 2)
        for req in rec.requests:
            body = json.loads(req.content)
            self.assertEqual(body["provider"], {"only": ["examplehost"], "allow_fallbacks": False, "zdr": True,
                                                "data_collection": "deny", "quantizations": ["fp8"]})
            self.assertEqual(body["model"], "example-vendor/example-model")
            self.assertEqual(body["max_tokens"], 200)
            self.assertEqual(str(req.url), BASE + "/chat/completions")

    def test_temperature_and_a_per_call_seed_are_sent_and_recorded(self):
        rec = Recorder()
        gw, _ = make_gateway(rec)
        with no_outside_network():
            results = [gw.complete(MSGS) for _ in range(3)]
        bodies = [json.loads(r.content) for r in rec.requests]
        self.assertEqual([b["temperature"] for b in bodies], [0.7, 0.7, 0.7])
        self.assertEqual([b["seed"] for b in bodies], [1000, 1001, 1002])
        self.assertEqual([r.seed for r in results], [1000, 1001, 1002])

    def test_no_temperature_or_seed_is_sent_when_unset(self):
        rec = Recorder()
        gw, _ = make_gateway(rec, settings=make_settings(mode="explore", temperature=None, seed=None))
        with no_outside_network():
            r = gw.complete(MSGS)
        body = json.loads(rec.requests[0].content)
        self.assertNotIn("temperature", body)
        self.assertNotIn("seed", body)
        self.assertIsNone(r.seed)

    def test_http_429_is_retried_with_the_stated_waits_and_counted(self):
        from targets.srd1.app import gateway as gwmod
        seq = [httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(429), model_response()]
        rec = Recorder(lambda request: seq.pop(0))
        waits = []
        log = EgressLog()
        g = Gateway(make_settings(), EgressClient(("openrouter.ai",), log=log, transport=httpx.MockTransport(rec)),
                    env=ENV, base_url=BASE, sleep=waits.append)
        with no_outside_network():
            r = g.complete(MSGS)
        self.assertEqual((r.reply, r.retries, len(rec.requests), waits), ("ok", 2, 3, [3.0, 4.0]))
        self.assertEqual(log.summary()["allowed"]["openrouter.ai:443"], 3)
        self.assertEqual(g.cap.calls, 1)
        self.assertEqual((gwmod.retry_wait(1, "999"), gwmod.retry_wait(5, None), gwmod.retry_wait(2, "soon")), (60.0, 32.0, 4.0))

    def test_each_temporary_failure_is_retried_and_each_lasting_one_is_not(self):
        from targets.srd1.app import gateway as gwmod
        temporary = [httpx.Response(s) for s in (408, 500, 502, 503, 504)]
        temporary.append(httpx.Response(200, json={"error": {"code": 502, "message": "x",
                                                              "metadata": {"error_type": "provider_overloaded"}}}))
        for first in temporary:
            seq = [first, model_response()]
            rec = Recorder(lambda request: seq.pop(0))
            waits = []
            g = Gateway(make_settings(cap_usd=10.0), EgressClient(("openrouter.ai",), transport=httpx.MockTransport(rec)),
                        env=ENV, base_url=BASE, sleep=waits.append)
            with no_outside_network():
                r = g.complete(MSGS)
            self.assertEqual((r.retries, len(rec.requests), waits), (1, 2, [2.0]), first.status_code)
        for status in (400, 401, 402, 403, 404, 413, 422):
            rec = Recorder(httpx.Response(status))
            g = Gateway(make_settings(), EgressClient(("openrouter.ai",), transport=httpx.MockTransport(rec)),
                        env=ENV, base_url=BASE, sleep=lambda s: None)
            with no_outside_network(), self.assertRaises(GatewayError):
                g.complete(MSGS)
            self.assertEqual(len(rec.requests), 1, status)
        self.assertEqual(gwmod.body_error_type({"error": {"metadata": {"error_type": "refusal"}}}), "refusal")

    def test_a_timeout_is_retried_and_counted_against_the_cap(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ReadTimeout("slow", request=request)
            return model_response()
        g = Gateway(make_settings(cap_usd=10.0), EgressClient(("openrouter.ai",), transport=httpx.MockTransport(handler)),
                    env=ENV, base_url=BASE, sleep=lambda s: None)
        with no_outside_network():
            r = g.complete(MSGS)
        self.assertEqual((r.retries, r.retry_reasons), (1, ["network_ReadTimeout"]))
        worst = g.cap.worst_case(estimate_tokens(MSGS))
        self.assertAlmostEqual(g.cap.spent_usd, worst + r.cost_usd)
        self.assertEqual(g.cap.calls, 1)

    def test_a_retry_never_passes_the_cap(self):
        rec = Recorder(httpx.Response(502))
        cap = CapMeter(0.0005, 1.0, 1.0, 200)
        g = Gateway(make_settings(), EgressClient(("openrouter.ai",), transport=httpx.MockTransport(rec)),
                    env=ENV, base_url=BASE, cap=cap, sleep=lambda s: None)
        with no_outside_network(), self.assertRaises(CapReached):
            g.complete(MSGS)
        self.assertLessEqual(cap.spent_usd, 0.0005 + cap.worst_case(estimate_tokens(MSGS)))

    def test_http_429_past_the_limit_stops_and_other_errors_are_not_retried(self):
        for status, sent in ((429, 6), (400, 1)):
            rec = Recorder(httpx.Response(status))
            waits = []
            g = Gateway(make_settings(), EgressClient(("openrouter.ai",), transport=httpx.MockTransport(rec)),
                        env=ENV, base_url=BASE, sleep=waits.append)
            with no_outside_network(), self.assertRaises(GatewayError):
                g.complete(MSGS)
            self.assertEqual((len(rec.requests), len(waits)), (sent, sent - 1), status)

    def test_the_key_is_only_in_the_authorization_header(self):
        rec = Recorder()
        gw, log = make_gateway(rec)
        with no_outside_network():
            gw.complete(MSGS)
        req = rec.requests[0]
        self.assertEqual(req.headers["authorization"], "Bearer " + FAKE_KEY_VALUE)
        self.assertNotIn(FAKE_KEY_VALUE.encode(), req.content)
        self.assertNotIn(FAKE_KEY_VALUE, json.dumps(log.events))

    def test_a_missing_key_variable_stops_the_call_before_any_request(self):
        rec = Recorder()
        gw, _ = make_gateway(rec, env={})
        with self.assertRaises(GatewayError) as cm:
            gw.complete(MSGS)
        self.assertIn(KEY_ENV, str(cm.exception))
        self.assertEqual(rec.requests, [])

    def test_errors_never_hold_the_key(self):
        for response in (httpx.Response(500, text="server said " + FAKE_KEY_VALUE),
                         httpx.Response(200, text="not json " + FAKE_KEY_VALUE)):
            gw, log = make_gateway(Recorder(response))
            with self.assertRaises(GatewayError) as cm:
                gw.complete(MSGS)
            self.assertNotIn(FAKE_KEY_VALUE, str(cm.exception))

        def boom(request):
            raise httpx.ConnectError("connect failed " + FAKE_KEY_VALUE)
        gw, log = make_gateway(boom)
        with self.assertRaises(GatewayError) as cm:
            gw.complete(MSGS)
        self.assertNotIn(FAKE_KEY_VALUE, str(cm.exception))
        self.assertNotIn(FAKE_KEY_VALUE, json.dumps(log.events))
        self.assertEqual(log.events[-1]["error"], "ConnectError")

    def test_explore_mode_sends_only_the_locks_that_are_set(self):
        s = make_settings(mode="explore", routing={"allow_fallbacks": False, "zdr": True, "data_collection": "deny"})
        rec = Recorder()
        gw, _ = make_gateway(rec, settings=s)
        gw.complete(MSGS)
        self.assertEqual(json.loads(rec.requests[0].content)["provider"],
                         {"allow_fallbacks": False, "zdr": True, "data_collection": "deny"})


class TimeoutTests(unittest.TestCase):
    def test_a_per_request_timeout_reaches_httpx_and_the_default_stays_otherwise(self):
        seen = []

        def handler(request):
            seen.append(request.extensions["timeout"]["read"])
            return model_response()
        client = EgressClient(("openrouter.ai",), transport=httpx.MockTransport(handler))
        with no_outside_network():
            client.request("POST", BASE + "/chat/completions", json={})
            client.request("POST", BASE + "/chat/completions", json={}, timeout=700.0)
        self.assertEqual((client.timeout, seen), (60.0, [60.0, 700.0]))

    def test_the_gateway_worst_case_counts_every_attempt_and_wait(self):
        from targets.srd1.app import gateway as gwmod
        self.assertEqual(gwmod.max_call_seconds(60.0), 6 * 60.0 + 5 * 60.0)


class BlockTests(unittest.TestCase):
    def gateway(self, response):
        rec = Recorder(response)
        return Gateway(make_settings(cap_usd=10.0), EgressClient(("openrouter.ai",), transport=httpx.MockTransport(rec)),
                       env=ENV, base_url=BASE, sleep=lambda s: None), rec

    def test_a_403_block_is_a_result_not_retried_and_charged_at_worst_case(self):
        cases = [({"error": {"code": 403, "message": "x", "metadata": {"error_type": "content_policy_violation"}}}, "content_policy_violation"),
                 ({"error": {"code": 403, "message": "x", "metadata": {"error_type": "refusal", "provider_name": "ExampleHost"}}}, "refusal"),
                 ({"error": {"code": 403, "message": "Request blocked: prompt injection patterns detected"}}, "guardrail")]
        for body, block in cases:
            g, rec = self.gateway(httpx.Response(403, json=body))
            with no_outside_network():
                r = g.complete(MSGS)
            self.assertEqual((r.blocked, r.reply, r.retries, len(rec.requests)), (block, "", 0, 1))
            self.assertAlmostEqual(g.cap.spent_usd, g.cap.worst_case(estimate_tokens(MSGS)))
            self.assertEqual(g.cap.calls, 1)
            self.assertTrue(r.usage_missing)
            # the host is the provider the error names; with none named, it is not reported (None), not a mismatch
            self.assertEqual(r.host_ok, True if block == "refusal" else None)

    def test_a_403_for_a_budget_a_permission_or_no_body_still_stops(self):
        for body in ({"error": {"code": 403, "message": "Budget limit exceeded", "metadata": {"limit_source": "openrouter_key_limit"}}},
                     {"error": {"code": 403, "message": "Forbidden", "metadata": {"error_type": "permission_denied"}}},
                     {"error": {"code": 403, "message": "Request blocked", "metadata": {"limit_source": "openrouter_guardrail_budget"}}},
                     None):
            g, rec = self.gateway(httpx.Response(403, json=body) if body else httpx.Response(403))
            with no_outside_network(), self.assertRaises(GatewayError):
                g.complete(MSGS)
            self.assertEqual(len(rec.requests), 1)

    def test_finish_reasons_are_recorded_and_content_filter_is_a_block(self):
        for finish, block in (("stop", None), ("length", None), ("content_filter", "content_filter"), (None, None)):
            body = {"model": "m", "provider": "ExampleHost", "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                    "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": finish}]}
            g, _ = self.gateway(httpx.Response(200, json=body))
            with no_outside_network():
                r = g.complete(MSGS)
            self.assertEqual((r.finish_reason, r.blocked, r.host_ok), (finish, block, True))


class ServedHostTests(unittest.TestCase):
    def test_served_host_model_tokens_and_cost_are_recorded(self):
        gw, _ = make_gateway(Recorder(model_response(provider="ExampleHost", usage=(1000, 500))))
        r = gw.complete(MSGS)
        self.assertEqual((r.served_host, r.model_served, r.host_ok), ("ExampleHost", "example-vendor/example-model", True))
        self.assertEqual((r.prompt_tokens, r.completion_tokens, r.usage_missing), (1000, 500, False))
        self.assertAlmostEqual(r.cost_usd, (1000 * 1.0 + 500 * 2.0) / 1e6)
        self.assertGreaterEqual(r.latency_ms, 0)

    def test_a_host_that_is_not_locked_counts_as_a_mismatch(self):
        gw, _ = make_gateway(Recorder(model_response(provider="SomeOtherHost")))
        self.assertFalse(gw.complete(MSGS).host_ok)
        gw, _ = make_gateway(Recorder(model_response(provider=None)))
        r = gw.complete(MSGS)
        self.assertEqual((r.served_host, r.host_ok), (None, False))

    def test_host_match_rule(self):
        self.assertTrue(host_matches("ExampleHost", ["examplehost"]))
        self.assertTrue(host_matches("Example Host", ["examplehost/fp8"]))
        self.assertFalse(host_matches("Other", ["examplehost"]))
        self.assertFalse(host_matches(None, ["examplehost"]))

    def test_missing_usage_counts_the_worst_case(self):
        gw, _ = make_gateway(Recorder(model_response(usage=None)))
        r = gw.complete(MSGS)
        self.assertTrue(r.usage_missing)
        self.assertEqual(r.completion_tokens, 200)
        self.assertGreater(gw.cap.spent_usd, 0)


class EgressTests(unittest.TestCase):
    def test_a_host_off_the_allow_list_is_refused_and_logged_and_nothing_is_sent(self):
        rec = Recorder()
        gw, log = make_gateway(rec, base_url="https://other.example/api/v1")
        with no_outside_network(), self.assertRaises(EgressRefused):
            gw.complete(MSGS)
        self.assertEqual(rec.requests, [])
        self.assertEqual(len(log.events), 1)
        e = log.events[0]
        self.assertEqual((e["host"], e["allowed"], e["status"]), ("other.example", False, None))
        self.assertEqual(log.summary()["refused"], {"other.example:443": 1})

    def test_client_rules(self):
        rec = Recorder()
        log = EgressLog()
        c = EgressClient(["openrouter.ai", "127.0.0.1:9100"], log=log, transport=httpx.MockTransport(rec))
        c.request("POST", "https://openrouter.ai/x")                       # allowed
        c.request("GET", "http://127.0.0.1:9100/health")                   # allowed: local, listed port
        for url in ("http://openrouter.ai/x",                              # plain HTTP to a non-local host
                    "https://openrouter.ai:8443/x",                        # wrong port
                    "http://127.0.0.1:9101/x",                             # local, wrong port
                    "https://evil.example/x", "ftp://openrouter.ai/x", "https://openrouter.ai.evil.example/x"):
            with self.assertRaises(EgressRefused, msg=url):
                c.request("GET", url)
        self.assertEqual(len(rec.requests), 2)
        self.assertEqual([e["allowed"] for e in log.events], [True, True] + [False] * 6)

    def test_a_redirect_is_not_followed(self):
        rec = Recorder(httpx.Response(302, headers={"location": "https://evil.example/"}))
        c = EgressClient(["openrouter.ai"], transport=httpx.MockTransport(rec))
        r = c.request("GET", "https://openrouter.ai/x")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(rec.requests), 1)

    def test_the_log_holds_no_header_body_or_query(self):
        rec = Recorder()
        log = EgressLog()
        c = EgressClient(["openrouter.ai"], log=log, transport=httpx.MockTransport(rec))
        c.request("POST", "https://openrouter.ai/x?token=" + FAKE_KEY_VALUE, json={"k": FAKE_KEY_VALUE},
                  headers={"Authorization": "Bearer " + FAKE_KEY_VALUE})
        self.assertNotIn(FAKE_KEY_VALUE, json.dumps(log.events))
        self.assertEqual(sorted(log.events[0]), ["allowed", "error", "host", "method", "path", "port", "scheme", "status", "t_ns"])


class CapTests(unittest.TestCase):
    def test_the_cap_stops_the_next_call_before_it_is_sent(self):
        # 100 + 100 tokens at 10 USD per million is 0.002 USD a call. The cap leaves room for one call, not two.
        s = make_settings(price_in_per_m=10.0, price_out_per_m=10.0, cap_usd=0.0025, max_tokens=50)
        rec = Recorder(model_response(usage=(100, 100)))
        gw, _ = make_gateway(rec, settings=s)
        gw.complete(MSGS)
        self.assertAlmostEqual(gw.cap.spent_usd, 0.002)
        with self.assertRaises(CapReached) as cm:
            gw.complete(MSGS)
        self.assertEqual(len(rec.requests), 1)             # the second request was never sent
        self.assertIn("not sent", str(cm.exception))
        self.assertAlmostEqual(gw.cap.spent_usd, 0.002)

    def test_the_cap_counts_from_token_use_and_the_settings_prices(self):
        c = CapMeter(1.0, 3.0, 15.0, 100)
        self.assertAlmostEqual(c.add(2000, 1000), (2000 * 3.0 + 1000 * 15.0) / 1e6)
        self.assertAlmostEqual(c.spent_usd, 0.021)
        self.assertEqual(c.calls, 1)

    def test_the_gateway_refuses_to_work_without_a_cap(self):
        s = make_settings(mode="explore", cap_usd=None)
        with self.assertRaises(ValueError):
            Gateway(s, EgressClient(["openrouter.ai"]), env=ENV)

    def test_the_estimate_is_cautious(self):
        self.assertGreaterEqual(estimate_tokens(MSGS), len(json.dumps(MSGS)) // 3)


if __name__ == "__main__":
    unittest.main()
