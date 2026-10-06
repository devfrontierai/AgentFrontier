"""Secret references of the form ``${scheme:reference}`` in connection properties and auth values.

Built in: ``env`` (environment variable) and ``file`` (file content, trailing newline removed). Register more (a
vault, a cloud secret manager) with ``AgentFrontier(secret_resolvers=[...])``. Error messages name the reference,
never a value.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import json_util as J
from .errors import AgentFrontierError

_REF = re.compile(r"\$\{([a-zA-Z][a-zA-Z0-9_-]*):([^}]+)}")


class SecretResolver:
    """Resolves one scheme. Subclass it, or use ``SecretResolver("vault", lambda ref: ...)``."""

    def __init__(self, scheme: str | None = None, fn: Callable[[str], str] | None = None):
        if scheme is not None:
            self.scheme = scheme
        self._fn = fn

    scheme: str = ""

    def resolve(self, reference: str) -> str:
        """The secret value; raise ``AgentFrontierError`` with code INVALID_CONFIG when it does not exist."""
        if self._fn is None:
            raise NotImplementedError
        return self._fn(reference)


class Secrets:
    def __init__(self, extra: Iterable[SecretResolver] = (), env: Callable[[str], str | None] | None = None):
        env_fn = env if env is not None else os.environ.get

        def from_env(reference: str) -> str:
            v = env_fn(reference)
            if v is None:
                raise AgentFrontierError.invalid_config("environment variable " + reference + " is not set")
            return v

        def from_file(reference: str) -> str:
            try:
                s = Path(J.jtrim(reference)).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                raise AgentFrontierError.invalid_config("cannot read secret file " + reference) from None
            return J.strip_trailing(s) if s.endswith("\n") else s

        self._resolvers: dict[str, SecretResolver] = {"env": SecretResolver("env", from_env),
                                                      "file": SecretResolver("file", from_file)}
        for r in extra:
            self._resolvers[r.scheme] = r

    def resolve(self, value: str | None) -> str | None:
        if value is None or "${" not in value:
            return value

        def sub(m: re.Match) -> str:
            r = self._resolvers.get(m.group(1))
            if r is None:
                return m.group()  # not a secret reference we know (it may be literal text); leave it alone
            return r.resolve(J.jtrim(m.group(2)))

        return _REF.sub(sub, value)

    def resolve_all(self, values: Mapping[str, str]) -> dict[str, str]:
        return {k: self.resolve(v) for k, v in values.items()}

    def resolve_deep(self, v: Any) -> Any:
        if isinstance(v, str):
            return self.resolve(v)
        if isinstance(v, dict):
            return {k: self.resolve_deep(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self.resolve_deep(x) for x in v]
        return v
