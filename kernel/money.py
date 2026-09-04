"""Money is integer paise. Never float. Never Decimal in the hot path.

Every amount that crosses a boundary (mandate, cart, action, Razorpay call) is an
`int` count of paise. This module is the only place allowed to convert to/from
human strings, and it refuses lossy conversions loudly.
"""
from __future__ import annotations

import re
from typing import Final

MAX_PAISE: Final[int] = 10**13  # ₹10,000,00,00,000 — overflow guard, not a business rule
CURRENCY: Final[str] = "INR"

_RUPEE_RE = re.compile(r"^\s*(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*)(?:\.([0-9]{1,2}))?\s*$", re.I)


class MoneyError(ValueError):
    """Raised when an amount cannot be represented exactly in paise."""


def paise(value: int) -> int:
    """Validate an integer paise amount. Rejects bool, float, negative, absurd."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError(f"amount must be int paise, got {type(value).__name__}")
    if value < 0:
        raise MoneyError("amount must be >= 0")
    if value > MAX_PAISE:
        raise MoneyError("amount exceeds MAX_PAISE overflow guard")
    return value


def from_rupee_string(text: str) -> int:
    """'₹4,000.50' -> 400050. Rejects anything with sub-paise precision."""
    if not isinstance(text, str):
        raise MoneyError("expected string")
    m = _RUPEE_RE.match(text)
    if not m:
        raise MoneyError(f"unparseable amount: {text!r}")
    whole = int(m.group(1).replace(",", ""))
    frac = m.group(2) or "0"
    frac_paise = int(frac.ljust(2, "0"))
    return paise(whole * 100 + frac_paise)


def to_rupee_string(amount: int) -> str:
    """400050 -> '₹4,000.50'. Display only — never feed this back into arithmetic."""
    paise(amount)
    whole, frac = divmod(amount, 100)
    return f"₹{whole:,}.{frac:02d}"


def add(*amounts: int) -> int:
    """Checked addition — a single overflow guard beats scattered asserts."""
    total = 0
    for a in amounts:
        total += paise(a)
    return paise(total)


def mul(amount: int, qty: int) -> int:
    """Line-item extension. qty must be a positive int; no fractional quantities."""
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise MoneyError("qty must be a positive int")
    if qty > 100_000:
        raise MoneyError("qty exceeds sanity bound")
    return paise(paise(amount) * qty)
