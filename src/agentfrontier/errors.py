"""Errors. Every SDK failure is an :class:`AgentFrontierError` with a stable :attr:`~AgentFrontierError.code`."""

from __future__ import annotations

INVALID_CONFIG = "INVALID_CONFIG"
INVALID_DEFINITION = "INVALID_DEFINITION"
UNSUPPORTED = "UNSUPPORTED"
CONNECTION_FAILED = "CONNECTION_FAILED"
QUERY_FAILED = "QUERY_FAILED"
QUERY_REJECTED = "QUERY_REJECTED"
AUTH_FAILED = "AUTH_FAILED"
SINK_FAILED = "SINK_FAILED"


class AgentFrontierError(RuntimeError):
    """Every SDK failure. ``code`` is stable across versions and languages and safe to show to users; messages never
    contain credentials."""

    INVALID_CONFIG = INVALID_CONFIG
    INVALID_DEFINITION = INVALID_DEFINITION
    UNSUPPORTED = UNSUPPORTED
    CONNECTION_FAILED = CONNECTION_FAILED
    QUERY_FAILED = QUERY_FAILED
    QUERY_REJECTED = QUERY_REJECTED
    AUTH_FAILED = AUTH_FAILED
    SINK_FAILED = SINK_FAILED

    def __init__(self, code: str, message: str, cause: BaseException | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        if cause is not None:
            self.__cause__ = cause

    def __str__(self) -> str:
        return self.message

    @staticmethod
    def invalid_config(message: str) -> "AgentFrontierError":
        return AgentFrontierError(INVALID_CONFIG, message)

    @staticmethod
    def invalid_definition(message: str) -> "AgentFrontierError":
        return AgentFrontierError(INVALID_DEFINITION, message)

    @staticmethod
    def unsupported(message: str) -> "AgentFrontierError":
        return AgentFrontierError(UNSUPPORTED, message)

    @staticmethod
    def connection(message: str, cause: BaseException | None = None) -> "AgentFrontierError":
        return AgentFrontierError(CONNECTION_FAILED, message, cause)

    @staticmethod
    def query(message: str, cause: BaseException | None = None) -> "AgentFrontierError":
        return AgentFrontierError(QUERY_FAILED, message, cause)

    @staticmethod
    def rejected(message: str) -> "AgentFrontierError":
        return AgentFrontierError(QUERY_REJECTED, message)


def root_message(exc: BaseException) -> str:
    """First line of the innermost cause's message; drivers bury the useful part there."""
    r = exc
    seen = {id(r)}
    while True:
        nxt = r.__cause__ if r.__cause__ is not None else None
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(nxt))
        r = nxt
    m = str(r)
    if not m:
        m = type(r).__name__
    lines = m.splitlines()
    return lines[0] if lines else m
