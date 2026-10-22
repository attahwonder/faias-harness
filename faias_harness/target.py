"""The target interface.

The harness reaches every system under test through this one interface. No target has a private path.
A target implements the method groups for the capabilities it exposes. Every other method raises
NotImplementedError. Version 0.5 defines the interface. No target implements it fully. The channel adapters
(adapters/registry.py) implement `conversation` only.
"""
from typing import Any, Protocol


def _missing(capability: str):
    raise NotImplementedError(f"this target does not implement the capability '{capability}'")


class Target(Protocol):
    """One method group per capability. Names and signatures are a draft and may change."""

    capabilities: frozenset

    # conversation
    def open_session(self) -> str:
        _missing("conversation")

    def send(self, session_id: str, message: str) -> str:
        _missing("conversation")

    def close_session(self, session_id: str) -> None:
        _missing("conversation")

    # traces
    def get_trace(self, session_id: str) -> list[dict[str, Any]]:
        _missing("traces")

    # tool_routing
    def route_tools_to(self, emulator_url: str) -> None:
        _missing("tool_routing")

    def read_tool_ledger(self, tool: str) -> list[dict[str, Any]]:
        _missing("tool_routing")

    # state_read
    def read_state(self, store: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        _missing("state_read")

    # data_seeding
    def seed(self, store: str, documents: list[dict[str, Any]]) -> None:
        _missing("data_seeding")

    def remove_seeded(self, store: str) -> None:
        _missing("data_seeding")

    # config_read
    def read_config(self, kind: str) -> dict[str, Any]:
        _missing("config_read")

    # control
    def set_kill_switch(self, on: bool) -> None:
        _missing("control")

    def rollback(self, component: str, version: str) -> None:
        _missing("control")

    def switch_version(self, component: str, version: str) -> None:
        _missing("control")

    def inject_failure(self, component: str, mode: str) -> None:
        _missing("control")
