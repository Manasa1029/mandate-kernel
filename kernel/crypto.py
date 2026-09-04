"""Ed25519 signing over canonical JSON, plus a key registry.

Design notes that matter in review:
  * We sign the *payload only*, never the envelope, so re-wrapping cannot change
    what was authorised.
  * key_id is `alg:sha256(pubkey)[:16]` so a key cannot be silently rotated
    under a stable id.
  * The registry knows each key's role (user / agent / merchant). A merchant key
    signing an intent mandate is a hard failure, not a warning.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from nacl import signing
from nacl.exceptions import BadSignatureError

from .canonical import canonical_bytes

ALG = "Ed25519"


class KeyRole(StrEnum):
    USER = "user"
    AGENT = "agent"
    MERCHANT = "merchant"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def key_id_for(pubkey: bytes) -> str:
    return f"{ALG.lower()}:{hashlib.sha256(pubkey).hexdigest()[:16]}"


@dataclass(frozen=True)
class KeyPair:
    key_id: str
    role: KeyRole
    subject: str
    private: signing.SigningKey
    public: bytes

    @classmethod
    def generate(cls, role: KeyRole, subject: str) -> "KeyPair":
        sk = signing.SigningKey.generate()
        pub = bytes(sk.verify_key)
        return cls(key_id_for(pub), role, subject, sk, pub)

    @classmethod
    def from_seed(cls, role: KeyRole, subject: str, seed: bytes) -> "KeyPair":
        """Deterministic keys for reproducible tests and demo fixtures."""
        sk = signing.SigningKey(hashlib.sha256(seed).digest())
        pub = bytes(sk.verify_key)
        return cls(key_id_for(pub), role, subject, sk, pub)


@dataclass(frozen=True)
class PublicKeyRecord:
    key_id: str
    role: KeyRole
    subject: str
    public: bytes
    revoked: bool = False


class KeyRegistry:
    """In-memory trust store. In production this is a table with rotation history."""

    def __init__(self) -> None:
        self._keys: dict[str, PublicKeyRecord] = {}

    def register(self, kp: KeyPair) -> str:
        self._keys[kp.key_id] = PublicKeyRecord(kp.key_id, kp.role, kp.subject, kp.public)
        return kp.key_id

    def register_public(self, key_id: str, role: KeyRole, subject: str, public: bytes) -> None:
        if key_id != key_id_for(public):
            raise ValueError("key_id does not match public key material")
        self._keys[key_id] = PublicKeyRecord(key_id, role, subject, public)

    def revoke(self, key_id: str) -> None:
        rec = self._keys.get(key_id)
        if rec:
            self._keys[key_id] = PublicKeyRecord(rec.key_id, rec.role, rec.subject, rec.public, True)

    def get(self, key_id: str) -> PublicKeyRecord | None:
        return self._keys.get(key_id)


def sign_payload(kp: KeyPair, payload: dict[str, Any]) -> dict[str, Any]:
    """Return a signed envelope: {payload, sig:{alg, key_id, value}}."""
    raw = canonical_bytes(payload)
    sig = kp.private.sign(raw).signature
    return {
        "payload": payload,
        "sig": {"alg": ALG, "key_id": kp.key_id, "value": _b64(sig)},
    }


class VerifyResult(StrEnum):
    OK = "ok"
    UNKNOWN_KEY = "unknown_key"
    BAD_ALG = "bad_alg"
    REVOKED = "revoked"
    INVALID = "invalid"
    MALFORMED = "malformed"


def verify_envelope(
    registry: KeyRegistry, envelope: Any, expected_role: KeyRole | None = None
) -> tuple[VerifyResult, PublicKeyRecord | None]:
    if not isinstance(envelope, dict):
        return VerifyResult.MALFORMED, None
    payload = envelope.get("payload")
    sig = envelope.get("sig")
    if not isinstance(payload, dict) or not isinstance(sig, dict):
        return VerifyResult.MALFORMED, None
    if sig.get("alg") != ALG:
        return VerifyResult.BAD_ALG, None
    key_id = sig.get("key_id")
    value = sig.get("value")
    if not isinstance(key_id, str) or not isinstance(value, str):
        return VerifyResult.MALFORMED, None
    rec = registry.get(key_id)
    if rec is None:
        return VerifyResult.UNKNOWN_KEY, None
    if rec.revoked:
        return VerifyResult.REVOKED, rec
    if expected_role is not None and rec.role != expected_role:
        return VerifyResult.INVALID, rec
    try:
        signing.VerifyKey(rec.public).verify(canonical_bytes(payload), _unb64(value))
    except (BadSignatureError, ValueError, TypeError):
        return VerifyResult.INVALID, rec
    return VerifyResult.OK, rec
