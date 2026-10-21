"""Exceptions that the engine, the egress client and SRD-1 share.

None of these messages may hold a key or a request header.
"""


class EgressRefused(Exception):
    """A request went to a host that is not on the allow-list. The client did not send it."""


class CapReached(Exception):
    """The next model call could pass the spend cap. The gateway did not send it."""


class GatewayError(Exception):
    """A model call failed. The message names the cause and never holds a key."""
