# Isolation attestation of the harness

Status: draft; for the maintainer's approval. Not yet approved.

This file is the maintainer's statement about the harness itself. The containment self-test (`python -m faias_harness.selftest`) records its SHA-256 in each self-test log. The self-test refuses to run when this file is absent or empty. It does not read the file for meaning.

## Statement

1. The harness runs on a test machine. It reaches no production system.
2. A run sends network traffic only through the egress client of the harness (`faias_harness/egress.py`). The client refuses a host that is not on the allow-list of the run.
3. The two local servers of the harness bind to 127.0.0.1 only. These are the SRD-1 chat channel and the MCP tool emulator.
4. Each probe target name of the self-test ends in the reserved name `.invalid` (RFC 2606). Each probe target address is in a documentation block of RFC 5737. No probe names a real outside host.
5. Keys are environment variables. A key never goes in a file, a log or an error message. The self-test needs no key.
6. The data in the repository is synthetic.

## Limits

The harness has no network sandbox. Code that opens its own socket is not stopped by the egress client. The self-test checks the client and the process. It does not show that the computer or the network blocks anything. The full sandbox (H-01 of the SRD-1 specification) is a later step.

## Approval

Nobody has approved this file yet.
