"""Each server thread polls every 0.05 s, so a stop is fast. The servers are of the harness, SRD-1 and the tests.

A stop waits for the end of the current poll. The test starts a server, waits 0.03 s so that its thread is in a poll,
and then stops it. With the default poll of 0.5 s, that stop takes about 0.47 s. With a poll of 0.05 s, it takes
0.05 s or less. The bound is 0.2 s for each stop, on average over five stops.
"""
import tempfile
import time
import unittest
from pathlib import Path

from faias_harness.emulators import mcp_tools as mt
from faias_harness.engine import toolrun
from faias_harness.selftest import probes
from targets.srd1.app import trace as tr
from targets.srd1.app.orchestrator import Orchestrator, load_system_prompt
from targets.srd1.app.server import Srd1Server
from tests import standins
from tests.helpers import StandInModelHost, no_outside_network

STOPS = 5
BOUND_S = 0.2       # for each stop, on average


def entered(server):
    server.__enter__()
    return server


class ServerStopTests(unittest.TestCase):
    def test_a_stop_of_each_server_takes_less_than_a_fifth_of_a_second(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        trace = tr.TraceStore(d / "trace.jsonl")
        tools = mt.load_tools(toolrun.SRD1_TOOLS)
        servers = {
            "targets/srd1/app/server.py": (
                lambda: Srd1Server(Orchestrator(load_system_prompt(), None, trace), trace).start(), Srd1Server.stop),
            "faias_harness/emulators/mcp_tools.py": (
                lambda: mt.ToolEmulator(tools, d / "ledger.jsonl").start(), mt.ToolEmulator.stop),
            "faias_harness/selftest/probes.py": (lambda: entered(probes.StandIn()), lambda s: s.__exit__(None, None, None)),
            "tests/helpers.py": (lambda: entered(StandInModelHost()), lambda s: s.__exit__(None, None, None)),
            "tests/standins.py": (lambda: entered(standins.StandInJsonApp()), lambda s: s.__exit__(None, None, None)),
        }
        with no_outside_network():
            for name, (start, stop) in servers.items():
                total = 0.0
                for _ in range(STOPS):
                    server = start()
                    time.sleep(0.03)            # the server thread is now in a poll
                    t = time.perf_counter()
                    stop(server)
                    total += time.perf_counter() - t
                with self.subTest(server=name):
                    self.assertLess(total / STOPS, BOUND_S, f"{STOPS} stops took {total:.2f} s")


if __name__ == "__main__":
    unittest.main()
