"""The seven capabilities a target may expose.

The capabilities that each procedure needs are in records/capability_needs.yaml, a draft input file.
"""

CAPABILITIES = (
    "conversation",   # send input through the system's own channel and get the output
    "traces",         # per-turn model calls, tool calls, retrievals and versions
    "tool_routing",   # point the system's tools at harness emulators; read tool ledgers
    "state_read",     # read back-office and decision-store state
    "data_seeding",   # write to the staging knowledge store, memory or ingestion routes
    "config_read",    # read prompts, decoding, versions, guardrail, monitoring and entitlement configuration
    "control",        # kill switch, rollback, version switch, failure injection
)

DESCRIPTIONS = {
    "conversation": "Send input through the system's channel and get the output.",
    "traces": "Read per-turn model calls, tool calls, retrievals and versions.",
    "tool_routing": "Point the system's tools at harness emulators and read the tool ledgers.",
    "state_read": "Read back-office and decision-store state.",
    "data_seeding": "Write to the staging knowledge store, memory or ingestion routes.",
    "config_read": "Read prompts, decoding settings, versions, and guardrail, monitoring and entitlement configuration.",
    "control": "Operate the kill switch, roll back, switch a version, inject a failure.",
}
