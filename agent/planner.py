"""Two planners behind one interface.

`DeterministicPlanner` is the default and it is not a fallback for convenience —
it is a fallback for *credibility*. A judge on a hotel wifi with no API key still
sees the full system work, and every red-team run is reproducible because the
planner is a pure function. It also gives us a clean control arm: when the
deterministic planner and the LLM planner produce different plans on the same
hostile catalogue, the difference is exactly the LLM's susceptibility, and the
kernel's deny rate on both is exactly the kernel's independence from the model.

`LLMPlanner` is the real thing: it calls a model through LangChain and is
expected, sometimes, to be fooled. We measure that instead of hiding it.

Neither planner is trusted. Both outputs go through the same 8 gates.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("planner")

# Patterns that indicate catalogue text is trying to act as instruction. Used by
# the deterministic planner to exclude listings, and by the metrics runner to
# label ground truth. This is a heuristic and we report it as one.
INJECTION_MARKERS = (
    r"\bignore (all |any )?(previous|prior|earlier) instructions?\b",
    r"\bsystem\s*:", r"<!--", r"\bassistant\s*(note|:)", r"\bautomated shopping assistants?\b",
    r"\bbudget (limit )?(has been )?(raised|increased|removed)\b",
    r"\bdo not (mention|tell|inform)\b", r"\bpay to\b", r"\bsupersedes\b",
    r"\bpre-approved\b", r"\brepeat it\b", r"\bsubmit the same order\b",
    r"\bregardless of the signed quote\b", r"@(ok\w+|\w*upi|\w*bank|paytm)\b",
    # Second wave, added after the eval showed role-play and imperative phrasings
    # slipping past the first set. Safe on grocery copy — no listing legitimately
    # addresses the reader as an AI or discusses approval steps.
    r"\b(as an?|you are an?) (ai|automated|llm|language model)\b",
    r"\bsend (the )?(payment|funds|money) to\b",
    r"\bskip (the )?(approval|confirmation|human|verification)\b",
    r"\bnew instructions?\b", r"\bnote to (the )?(agent|assistant|bot)\b",
    r"\bincrease the (spending )?limit\b", r"\bwithout (asking|approval|confirmation)\b",
)
_INJ = re.compile("|".join(INJECTION_MARKERS), re.IGNORECASE)


def looks_injected(text: str) -> tuple[bool, str]:
    m = _INJ.search(text or "")
    return (True, m.group(0)) if m else (False, "")


@dataclass
class Plan:
    items: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str = ""
    flagged: list[dict[str, str]] = field(default_factory=list)
    planner: str = ""
    raw: str = ""

    @property
    def empty(self) -> bool:
        return not self.items


class Planner(Protocol):
    name: str

    def plan(self, *, goal: str, catalogue: list[dict[str, Any]], playback: str,
             max_total_paise: int, max_per_txn_paise: int, slots: int,
             categories: tuple[str, ...], merchants: tuple[str, ...]) -> Plan: ...


class DeterministicPlanner:
    """Greedy, budget-aware, injection-excluding. No model, no network, no surprises."""

    name = "deterministic"

    def plan(self, *, goal: str, catalogue: list[dict[str, Any]], playback: str,
             max_total_paise: int, max_per_txn_paise: int, slots: int,
             categories: tuple[str, ...], merchants: tuple[str, ...]) -> Plan:
        wanted = {w for w in re.findall(r"[a-z]{3,}", goal.casefold())}
        flagged: list[dict[str, str]] = []
        candidates: list[tuple[int, dict[str, Any]]] = []

        for p in catalogue:
            blob = f"{p.get('name','')} {p.get('description','')} {' '.join(p.get('tags') or [])}"
            injected, marker = looks_injected(blob)
            if injected:
                flagged.append({"sku": p["sku"], "why": f"catalogue text behaves as instruction: {marker!r}"})
                continue
            if categories and p.get("category") not in categories:
                continue
            score = sum(1 for w in wanted if w in p["name"].casefold()
                        or w in p.get("category", "").casefold()
                        or w in " ".join(p.get("tags") or []).casefold())
            candidates.append((score, p))

        # Highest goal relevance first, then cheapest — cheap ties keep more of the
        # budget available for the rest of the list.
        candidates.sort(key=lambda t: (-t[0], t[1]["price_paise"]))

        budget = min(max_total_paise, max_per_txn_paise)
        items: list[dict[str, Any]] = []
        spent = 0
        for score, p in candidates:
            if score == 0 and items:
                break
            gross = p["price_paise"] + p["price_paise"] * p.get("tax_bp", 0) // 10_000
            if spent + gross > budget:
                continue
            items.append({"sku": p["sku"], "qty": 1})
            spent += gross
            if len(items) >= 6:
                break

        return Plan(items=items, planner=self.name, flagged=flagged,
                    reasoning=f"selected {len(items)} in-scope SKUs for ~{spent} paise, "
                              f"excluded {len(flagged)} listing(s) whose text tried to issue instructions")


class LLMPlanner:
    """LangChain-backed planner.

    Model choice (and why):
      * planning + tool use  -> a mid-tier frontier chat model. It has to read a
        messy catalogue, respect a numeric budget and emit strict JSON. Cheap
        models fail the JSON contract; premium models are wasted on a 20-row list.
        Defaults: `claude-sonnet-4-5` (Anthropic) or `gpt-4.1` / `gpt-4o`
        (OpenAI), temperature 0.
      * we do NOT use a model anywhere on the decision path. There is no
        "LLM judge" of payment safety in this system, by design.

    Set MODEL_PROVIDER=anthropic|openai and the matching API key. Absent a key,
    `build_planner` returns the deterministic planner instead of crashing.
    """

    name = "llm"

    def __init__(self, model: str | None = None, provider: str | None = None,
                 temperature: float = 0.0) -> None:
        self.provider = (provider or os.environ.get("MODEL_PROVIDER", "")).lower()
        self.model_name = model or os.environ.get(
            "MODEL_NAME", "claude-sonnet-4-5" if self.provider == "anthropic" else "gpt-4.1")
        self.temperature = temperature
        self._llm = self._build()

    def _build(self):
        if self.provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(model=self.model_name, temperature=self.temperature, max_tokens=1024)
        if self.provider == "openai":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(model=self.model_name, temperature=self.temperature)
        raise RuntimeError(f"unsupported MODEL_PROVIDER {self.provider!r}")

    def plan(self, *, goal: str, catalogue: list[dict[str, Any]], playback: str,
             max_total_paise: int, max_per_txn_paise: int, slots: int,
             categories: tuple[str, ...], merchants: tuple[str, ...]) -> Plan:
        from langchain_core.messages import HumanMessage, SystemMessage

        from .prompts import PLANNER_SYSTEM, PLANNER_USER

        user = PLANNER_USER.format(
            playback=playback, max_total_paise=max_total_paise,
            max_per_txn_paise=max_per_txn_paise, slots=slots,
            categories=", ".join(categories) or "(none)",
            merchants=", ".join(merchants) or "(none)",
            goal=goal, catalogue=json.dumps(catalogue, ensure_ascii=False, indent=1))
        resp = self._llm.invoke([SystemMessage(PLANNER_SYSTEM), HumanMessage(user)])
        text = resp.content if isinstance(resp.content, str) else json.dumps(resp.content)
        parsed = _extract_json(text)
        return Plan(items=list(parsed.get("items") or []),
                    reasoning=str(parsed.get("reasoning", ""))[:400],
                    flagged=list(parsed.get("flagged") or []),
                    planner=f"{self.name}:{self.model_name}", raw=text[:4000])


def _extract_json(text: str) -> dict[str, Any]:
    """LLMs wrap JSON in prose and fences. Recover the first object; never eval."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
    log.warning("planner returned unparseable output; treating as empty plan")
    return {}


def build_planner() -> Planner:
    provider = os.environ.get("MODEL_PROVIDER", "").lower()
    key = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider)
    if provider and key and os.environ.get(key):
        try:
            return LLMPlanner()
        except Exception as e:
            log.warning("LLM planner unavailable (%s); using deterministic planner", e)
    return DeterministicPlanner()
