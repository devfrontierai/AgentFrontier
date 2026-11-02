"""RS256 JSON Web Tokens for the ``gcp_jwt`` provider (spec docs/auth.md), with the standard library only: PEM and
DER parsing of RSA private keys (PKCS#8 and PKCS#1) and RSASSA-PKCS1-v1_5 signatures with SHA-256."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass

from .errors import AgentFrontierError

# DER of the DigestInfo prefix for SHA-256 (RFC 8017 section 9.2, note 1)
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_PEM = re.compile(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", re.S)


@dataclass(frozen=True)
class RsaKey:
    n: int
    d: int

    @property
    def size(self) -> int:
        return (self.n.bit_length() + 7) // 8


def _der(data: bytes, pos: int) -> tuple[int, int, int]:
    """(tag, start of content, end of content) of the DER element at ``pos``."""
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    return tag, pos, pos + length


def _children(data: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    out = []
    while start < end:
        t, s, e = _der(data, start)
        out.append((t, s, e))
        start = e
    return out


def _rsa_private(der: bytes) -> RsaKey:
    """RSAPrivateKey: SEQUENCE {version, n, e, d, p, q, dp, dq, qinv}."""
    _, s, e = _der(der, 0)
    ints = _children(der, s, e)
    if len(ints) < 4 or any(t != 0x02 for t, _, _ in ints[:4]):
        raise ValueError("not an RSA private key")
    n = int.from_bytes(der[ints[1][1]:ints[1][2]], "big")
    d = int.from_bytes(der[ints[3][1]:ints[3][2]], "big")
    return RsaKey(n, d)


def load_private_key(pem: str) -> RsaKey:
    """An RSA key from PEM: PKCS#8 ``PRIVATE KEY`` or PKCS#1 ``RSA PRIVATE KEY`` (unencrypted)."""
    m = _PEM.search(pem.replace("\\n", "\n"))
    if m is None:
        raise AgentFrontierError.invalid_config("the private key is not PEM")
    kind = m.group(1)
    try:
        der = base64.b64decode("".join(m.group(2).split()))
        if kind == "RSA PRIVATE KEY":
            return _rsa_private(der)
        if kind == "PRIVATE KEY":
            _, s, e = _der(der, 0)
            parts = _children(der, s, e)
            # PrivateKeyInfo: version, AlgorithmIdentifier, OCTET STRING holding the RSAPrivateKey
            _, os_start, os_end = parts[2]
            return _rsa_private(der[os_start:os_end])
    except (ValueError, IndexError) as e:
        raise AgentFrontierError.invalid_config("cannot read the private key: " + str(e)) from None
    raise AgentFrontierError.invalid_config("unsupported private key type " + kind
                                            + " (an encrypted key must be decrypted first)")


def sign_rs256(key: RsaKey, message: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 section 8.2.1)."""
    t = _SHA256_PREFIX + hashlib.sha256(message).digest()
    k = key.size
    if k < len(t) + 11:
        raise AgentFrontierError.invalid_config("the RSA key is too short")
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), key.d, key.n).to_bytes(k, "big")


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _compact(doc: dict) -> bytes:
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def service_account_jwt(key: RsaKey, key_id: str | None, client_email: str, scope: str, audience: str,
                        issued_at: int) -> str:
    """The JWT a Google service account exchanges for an access token, keys in the order docs/auth.md fixes."""
    header: dict = {"alg": "RS256", "typ": "JWT"}
    if key_id:
        header["kid"] = key_id
    claims = {"iss": client_email, "scope": scope, "aud": audience, "iat": issued_at, "exp": issued_at + 3600}
    signing_input = b64url(_compact(header)) + "." + b64url(_compact(claims))
    return signing_input + "." + b64url(sign_rs256(key, signing_input.encode("ascii")))


__all__ = ["RsaKey", "load_private_key", "sign_rs256", "service_account_jwt", "b64url"]
