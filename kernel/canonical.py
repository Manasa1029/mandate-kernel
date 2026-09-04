"""Canonical JSON serialisation (RFC 8785 subset) — the substrate for signatures.

Two independently-written implementations must agree byte-for-byte on what a
mandate "is", or signature verification becomes a coin flip. We therefore
deliberately restrict the accepted value space:

  * no floats (a signed price of 4000.0 vs 4000 is a whole class of bugs)
  * no NaN/Infinity
  * dict keys must be str, sorted by UTF-16 code unit
  * no trailing whitespace, no separators padding
  * cycles rejected explicitly rather than blowing the recursion limit
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


class CanonicalisationError(ValueError):
    pass


def _check(value: Any, seen: set[int], depth: int) -> Any:
    if depth > 64:
        raise CanonicalisationError("max nesting depth exceeded")
    if isinstance(value, float):
        raise CanonicalisationError("floats are not signable; use integer paise")
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, dict):
        if id(value) in seen:
            raise CanonicalisationError("cycle detected")
        seen.add(id(value))
        out = {}
        for k in value:
            if not isinstance(k, str):
                raise CanonicalisationError(f"dict key must be str, got {type(k).__name__}")
            out[k] = _check(value[k], seen, depth + 1)
        seen.discard(id(value))
        return out
    if isinstance(value, (list, tuple)):
        if id(value) in seen:
            raise CanonicalisationError("cycle detected")
        seen.add(id(value))
        out_l = [_check(v, seen, depth + 1) for v in value]
        seen.discard(id(value))
        return out_l
    raise CanonicalisationError(f"unsupported type {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    """Deterministic UTF-8 bytes for any signable structure."""
    checked = _check(value, set(), 0)
    return json.dumps(
        checked,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    """Hex SHA-256 of the canonical form. Used for cart hashes and ledger chaining."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
