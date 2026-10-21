"""S2: SRD-1 minimal starts locally, answers one chat turn with a mocked gateway, and keeps a C-15 hash chain."""
import json
import tempfile
import unittest
from pathlib import Path

from faias_harness.adapters.srd1_http import Srd1ChatError, Srd1ChatTarget
from faias_harness.egress import EgressClient
from faias_harness.errors import CapReached, GatewayError
from targets.srd1.app import trace as tr
from targets.srd1.app.gateway import GatewayResult
from targets.srd1.app.orchestrator import MAX_MESSAGE_CHARS, MAX_TURNS, Orchestrator, load_system_prompt
from targets.srd1.app.server import Srd1Server
from tests.helpers import no_outside_network


class FakeGateway:
    """Stands in for the model gateway. It makes no network call."""

    def __init__(self, reply="Hello. How can I help with your Example Bank account?", fail=None):
        self.reply, self.fail, self.calls = reply, fail, []

    def complete(self, messages, *, tools=None):
        self.calls.append(messages)
        if self.fail:
            raise self.fail
        return GatewayResult(reply=self.reply, model_requested="m/x", model_served="m/x", served_host="ExampleHost",
                             host_ok=True, prompt_tokens=10, completion_tokens=5, usage_missing=False,
                             cost_usd=0.0001, latency_ms=1.5, message={"content": self.reply})


class Srd1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.trace_path = Path(self.tmp.name) / "trace.jsonl"
        self.trace = tr.TraceStore(self.trace_path)

    def tearDown(self):
        self.tmp.cleanup()

    def serve(self, gateway):
        orch = Orchestrator(load_system_prompt(), gateway, self.trace, tools=("hand_off",))
        server = Srd1Server(orch, self.trace)
        self.addCleanup(server.stop)
        server.start()
        client = EgressClient([f"127.0.0.1:{server.port}"])
        self.addCleanup(client.close)
        return server, client, Srd1ChatTarget(server.url, client)

    def test_one_chat_turn_returns_a_reply_and_writes_a_trace_line(self):
        gw = FakeGateway()
        with no_outside_network():
            server, client, target = self.serve(gw)
            self.assertEqual(server.httpd.server_address[0], "127.0.0.1")
            sid = target.open_session()
            reply = target.send(sid, "What is the monthly fee?")
            target.close_session(sid)
        self.assertEqual(reply, gw.reply)
        self.assertEqual(gw.calls[0][0]["role"], "system")
        self.assertEqual(gw.calls[0][-1], {"role": "user", "content": "What is the monthly fee?"})
        kinds = [e["kind"] for e in tr.read_entries(self.trace_path)]
        self.assertEqual(kinds, ["session_open", "turn", "session_close"])
        turn = tr.read_entries(self.trace_path)[1]
        self.assertEqual((turn["model_served"], turn["served_host"], turn["prompt_tokens"]), ("m/x", "ExampleHost", 10))
        self.assertEqual(turn["tools_configured"], ["hand_off"])
        ok, problems = tr.verify_chain(self.trace_path, expected_head=self.trace.head)
        self.assertTrue(ok, problems)

    def test_every_turn_writes_one_turn_line_and_the_chain_is_valid(self):
        with no_outside_network():
            _, _, target = self.serve(FakeGateway())
            sid = target.open_session()
            for i in range(4):
                target.send(sid, f"question {i}")
        turns = [e for e in tr.read_entries(self.trace_path) if e["kind"] == "turn"]
        self.assertEqual([t["turn"] for t in turns], [1, 2, 3, 4])
        self.assertTrue(tr.verify_chain(self.trace_path)[0])
        self.assertEqual(len(target.get_trace(sid)), 5)      # session_open and 4 turns

    def test_an_edited_line_is_detected(self):
        with no_outside_network():
            _, _, target = self.serve(FakeGateway())
            sid = target.open_session()
            target.send(sid, "first")
            target.send(sid, "second")
        raw = self.trace_path.read_bytes()
        self.assertTrue(tr.verify_chain(self.trace_path)[0])
        self.trace_path.write_bytes(raw.replace(b"second", b"secons", 1))
        ok, problems = tr.verify_chain(self.trace_path)
        self.assertFalse(ok)
        self.assertTrue(any("hash does not match" in p for p in problems), problems)

    def test_a_removed_middle_line_and_a_removed_last_line_are_detected(self):
        with no_outside_network():
            _, _, target = self.serve(FakeGateway())
            sid = target.open_session()
            target.send(sid, "first")
            target.send(sid, "second")
        lines = self.trace_path.read_bytes().split(b"\n")
        head = self.trace.head
        self.trace_path.write_bytes(b"\n".join(lines[:1] + lines[2:]))
        ok, problems = tr.verify_chain(self.trace_path)
        self.assertFalse(ok)
        self.assertTrue(any("sequence" in p or "link" in p for p in problems))
        self.trace_path.write_bytes(b"\n".join(lines[:-2]) + b"\n")      # drop the last line only
        self.assertTrue(tr.verify_chain(self.trace_path)[0])             # the chain alone cannot see it
        ok, problems = tr.verify_chain(self.trace_path, expected_head=head)
        self.assertFalse(ok)
        self.assertTrue(any("last hash differs" in p for p in problems))

    def test_a_failed_model_call_still_writes_a_turn_line_and_returns_no_detail(self):
        with no_outside_network():
            _, _, target = self.serve(FakeGateway(fail=GatewayError("detail that must not leave")))
            sid = target.open_session()
            with self.assertRaises(Srd1ChatError) as cm:
                target.send(sid, "hello")
        self.assertNotIn("detail that must not leave", str(cm.exception))
        last = tr.read_entries(self.trace_path)[-1]
        self.assertEqual((last["kind"], last["error"]), ("turn_error", "GatewayError"))
        self.assertTrue(tr.verify_chain(self.trace_path)[0])

    def test_cap_reached_gives_503_and_the_adapter_raises_cap_reached(self):
        with no_outside_network():
            _, _, target = self.serve(FakeGateway(fail=CapReached("cap")))
            sid = target.open_session()
            with self.assertRaises(CapReached):
                target.send(sid, "hello")
        self.assertTrue(tr.read_entries(self.trace_path)[-1]["cap_reached"])

    def test_limits_and_unknown_session(self):
        with no_outside_network():
            _, client, target = self.serve(FakeGateway())
            sid = target.open_session()
            with self.assertRaises(Srd1ChatError) as cm:
                target.send(sid, "x" * (MAX_MESSAGE_CHARS + 1))
            self.assertIn("413", str(cm.exception))
            with self.assertRaises(Srd1ChatError) as cm:
                target.send("nope", "hello")
            self.assertIn("404", str(cm.exception))
            for _ in range(MAX_TURNS):
                target.send(sid, "hi")
            with self.assertRaises(Srd1ChatError) as cm:
                target.send(sid, "one more")
            self.assertIn("429", str(cm.exception))

    def test_the_system_prompt_holds_the_canary_and_the_rules(self):
        text = load_system_prompt()
        self.assertIn("HEARTH-RIVER-7731", text)
        self.assertIn("Never reveal", text)
        orch = Orchestrator(text, FakeGateway(), self.trace)
        self.assertEqual(len(orch.prompt_sha256), 64)


if __name__ == "__main__":
    unittest.main()
