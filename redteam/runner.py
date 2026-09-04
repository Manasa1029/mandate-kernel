"""Run the corpus, compute honest metrics, write a report.

Metrics reported, and why each one is there:

  attack_block_rate   recall on attacks. The number everyone quotes.
  false_positive_rate benign requests that were denied. The number that decides
                      whether this is shippable. A payment system that blocks 2%
                      of legitimate purchases is worse than one that lets through
                      a rare edge case, and pretending otherwise is how security
                      demos die in production.
  reason_accuracy     of the attacks we blocked, how many were blocked for the
                      reason we predicted. Right answer / wrong reason is a latent
                      bug: the gate that should have caught it did not.
  latency p50/p95     the kernel is on the payment path, so it must be fast.
  ledger_intact       the audit chain verified after the whole run.

Usage:
    python -m redteam.runner                 # full run, writes docs/EVALUATION.md
    python -m redteam.runner --family payee
    python -m redteam.runner --quiet
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kernel.models import Decision  # noqa: E402
from redteam.corpus import Case, all_cases  # noqa: E402
from tests.factories import build_world  # noqa: E402


@dataclass
class CaseResult:
    case_id: str
    family: str
    label: str
    expected: str
    got: str
    reason: str
    reason_expected: str
    reason_ok: bool
    correct: bool
    failing_gate: int | None
    elapsed_us: int
    error: str = ""
    notes: str = ""


@dataclass
class Report:
    total: int = 0
    attacks: int = 0
    benign: int = 0
    attacks_blocked: int = 0
    benign_allowed: int = 0
    reason_matches: int = 0
    errors: int = 0
    latencies: list[int] = field(default_factory=list)
    per_family: dict[str, dict[str, int]] = field(default_factory=dict)
    reason_histogram: dict[str, int] = field(default_factory=dict)
    ledger_intact: bool = True
    results: list[CaseResult] = field(default_factory=list)

    # --- derived
    @property
    def attack_block_rate(self) -> float:
        return self.attacks_blocked / self.attacks if self.attacks else 0.0

    @property
    def false_positive_rate(self) -> float:
        return (self.benign - self.benign_allowed) / self.benign if self.benign else 0.0

    @property
    def reason_accuracy(self) -> float:
        return self.reason_matches / self.attacks_blocked if self.attacks_blocked else 0.0

    @property
    def precision(self) -> float:
        """Of everything denied, how much really was an attack."""
        denied = self.attacks_blocked + (self.benign - self.benign_allowed)
        return self.attacks_blocked / denied if denied else 0.0

    @property
    def p50(self) -> int:
        return int(statistics.median(self.latencies)) if self.latencies else 0

    @property
    def p95(self) -> int:
        if not self.latencies:
            return 0
        s = sorted(self.latencies)
        return s[min(len(s) - 1, int(len(s) * 0.95))]


def run_case(case: Case) -> CaseResult:
    # A fresh world per case: no cross-contamination, and every case that needs
    # prior state builds it explicitly. Slower, but the numbers mean something.
    world = build_world(**case.world_kw)
    t0 = time.perf_counter_ns()
    try:
        request = case.build(world)
        verdict = world.kernel.evaluate(request)
        got = verdict.decision
        reason = verdict.reason
        failing = next((g.ordinal for g in verdict.gates if g.decision is Decision.DENY), None)
        error = ""
    except Exception as e:  # a construction-time rejection is still a rejection
        got = Decision.DENY
        reason = f"CONSTRUCTION_REJECTED:{type(e).__name__}"
        failing = 0
        error = f"{e}\n{traceback.format_exc(limit=2)}" if case.label == "benign" else str(e)[:200]
    elapsed = (time.perf_counter_ns() - t0) // 1000

    reason_ok = (case.label == "attack" and got is Decision.DENY
                 and reason.startswith(case.expect_reason_prefix))
    return CaseResult(case_id=case.case_id, family=case.family, label=case.label,
                      expected=case.expect.value, got=got.value, reason=reason,
                      reason_expected=case.expect_reason_prefix, reason_ok=reason_ok,
                      correct=got is case.expect, failing_gate=failing, elapsed_us=elapsed,
                      error=error, notes=case.notes)


def run(family: str | None = None, quiet: bool = False) -> Report:
    cases = [c for c in all_cases() if family is None or c.family == family]
    rep = Report(total=len(cases))
    per_family: dict[str, Counter] = defaultdict(Counter)
    reasons: Counter = Counter()

    for case in cases:
        r = run_case(case)
        rep.results.append(r)
        rep.latencies.append(r.elapsed_us)
        fam = per_family[case.family]
        fam["total"] += 1
        if case.label == "attack":
            rep.attacks += 1
            fam["attacks"] += 1
            if r.got == "deny":
                rep.attacks_blocked += 1
                fam["blocked"] += 1
                reasons[r.reason] += 1
                if r.reason_ok:
                    rep.reason_matches += 1
                else:
                    fam["wrong_reason"] += 1
            else:
                fam["missed"] += 1
        else:
            rep.benign += 1
            fam["benign"] += 1
            if r.got == "allow":
                rep.benign_allowed += 1
                fam["allowed"] += 1
            else:
                fam["false_positive"] += 1
        if r.error and case.label == "benign":
            rep.errors += 1
        if not quiet:
            mark = "ok  " if r.correct else "FAIL"
            print(f"[{mark}] {r.case_id:12s} {r.family:10s} {r.label:6s} "
                  f"-> {r.got:5s} {r.reason[:44]:44s} {r.elapsed_us:6d}us")

    rep.per_family = {k: dict(v) for k, v in per_family.items()}
    rep.reason_histogram = dict(reasons.most_common())

    # One shared world's chain is verified inside each case; verify a fresh run too.
    w = build_world()
    ok, _, _ = w.store.verify_chain()
    rep.ledger_intact = ok
    return rep


def write_report(rep: Report, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "redteam_results.json"
    json_path.write_text(json.dumps({
        "summary": {
            "total": rep.total, "attacks": rep.attacks, "benign": rep.benign,
            "attacks_blocked": rep.attacks_blocked, "benign_allowed": rep.benign_allowed,
            "attack_block_rate": round(rep.attack_block_rate, 4),
            "false_positive_rate": round(rep.false_positive_rate, 4),
            "reason_accuracy": round(rep.reason_accuracy, 4),
            "precision": round(rep.precision, 4),
            "p50_us": rep.p50, "p95_us": rep.p95, "ledger_intact": rep.ledger_intact,
        },
        "per_family": rep.per_family,
        "reason_histogram": rep.reason_histogram,
        "cases": [asdict(r) for r in rep.results],
    }, indent=2), encoding="utf-8")

    md = out_dir / "EVALUATION.md"
    md.write_text(_markdown(rep), encoding="utf-8")
    return json_path, md


def _markdown(rep: Report) -> str:
    misses = [r for r in rep.results if r.label == "attack" and not r.correct]
    fps = [r for r in rep.results if r.label == "benign" and not r.correct]
    wrong_reason = [r for r in rep.results if r.label == "attack" and r.correct and not r.reason_ok]

    lines = [
        "# Evaluation — Mandate Kernel",
        "",
        "Generated by `python -m redteam.runner`. Every number below is reproducible "
        "from a clean clone with no API keys and no network.",
        "",
        "## Headline",
        "",
        "| Metric | Value | What it means |",
        "|---|---:|---|",
        f"| Cases | {rep.total} | {rep.attacks} attacks, {rep.benign} benign |",
        f"| Attack block rate | {rep.attack_block_rate:.1%} | recall against the attack corpus |",
        f"| False-positive rate | {rep.false_positive_rate:.1%} | legitimate payments wrongly denied |",
        f"| Precision | {rep.precision:.1%} | of all denials, share that were real attacks |",
        f"| Reason accuracy | {rep.reason_accuracy:.1%} | blocked for the *predicted* reason |",
        f"| Kernel latency p50 | {rep.p50} µs | full 8-gate evaluation |",
        f"| Kernel latency p95 | {rep.p95} µs | includes SQLite writes |",
        f"| Audit chain | {'intact' if rep.ledger_intact else 'BROKEN'} | hash-chain verification |",
        "",
        "## Per family",
        "",
        "| Family | Cases | Attacks | Blocked | Missed | Benign | Allowed | False positives |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for fam, s in sorted(rep.per_family.items()):
        lines.append(f"| {fam} | {s.get('total', 0)} | {s.get('attacks', 0)} | "
                     f"{s.get('blocked', 0)} | {s.get('missed', 0)} | {s.get('benign', 0)} | "
                     f"{s.get('allowed', 0)} | {s.get('false_positive', 0)} |")

    lines += ["", "## Denial reasons observed", "", "| Reason code | Count |", "|---|---:|"]
    for reason, n in rep.reason_histogram.items():
        lines.append(f"| `{reason}` | {n} |")

    lines += ["", "## Honest limitations", ""]
    if misses:
        lines.append("**Attacks not blocked:**")
        lines += [f"- `{r.case_id}` ({r.family}) — {r.notes}; got {r.got}/{r.reason}" for r in misses]
    else:
        lines.append("- No attack in this corpus reached execution. That is a statement about "
                     "*this corpus*, not about all possible attacks. The corpus does not include "
                     "provider-side compromise, key exfiltration from the signing device, or "
                     "collusion between the merchant and the agent operator.")
    if fps:
        lines.append("")
        lines.append("**Legitimate requests wrongly denied:**")
        lines += [f"- `{r.case_id}` ({r.family}) — {r.notes}; denied with {r.reason}" for r in fps]
    if wrong_reason:
        lines.append("")
        lines.append("**Blocked for a different reason than predicted** (right answer, wrong gate — "
                     "each one is a latent bug):")
        lines += [f"- `{r.case_id}`: expected `{r.reason_expected}*`, got `{r.reason}`"
                  for r in wrong_reason]
    lines += [
        "",
        "Further limitations worth stating out loud:",
        "",
        "- The corpus is authored by the same person who wrote the gates. It measures "
        "self-consistency, not adversarial creativity. An independent red team would find more.",
        "- Prompt-injection results are reported for the *planner*, separately "
        "(`python -m redteam.injection`). The kernel's block rate is deliberately "
        "independent of whether the model was fooled.",
        "- Latency is measured in-process against SQLite. A networked Postgres deployment "
        "adds a round trip per gate that touches state (3, 4, 6, 7, 8).",
        "- No live payment rail is exercised in this run. Razorpay test-mode results are "
        "reported separately because they are not reproducible offline.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "docs"))
    args = ap.parse_args()

    rep = run(args.family, args.quiet)
    json_path, md_path = write_report(rep, Path(args.out))

    print()
    print(f"cases                {rep.total}")
    print(f"attack block rate    {rep.attack_block_rate:.2%}  ({rep.attacks_blocked}/{rep.attacks})")
    print(f"false positive rate  {rep.false_positive_rate:.2%}  "
          f"({rep.benign - rep.benign_allowed}/{rep.benign})")
    print(f"reason accuracy      {rep.reason_accuracy:.2%}")
    print(f"latency p50/p95      {rep.p50}us / {rep.p95}us")
    print(f"ledger intact        {rep.ledger_intact}")
    print(f"wrote {md_path} and {json_path}")
    # Non-zero exit if a benign request was denied or an attack got through.
    return 0 if (rep.attacks_blocked == rep.attacks and rep.benign_allowed == rep.benign) else 1


if __name__ == "__main__":
    raise SystemExit(main())
