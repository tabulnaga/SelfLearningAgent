from __future__ import annotations

from dataclasses import dataclass

from app.models import DraftAnswer
from app.verification.checks import ALL_CHECKS, CheckResult, TurnContext


@dataclass
class Verdict:
    passed: bool
    results: list[CheckResult]

    @property
    def problems(self) -> list[str]:
        return [p for r in self.results for p in r.problems]

    def summary(self) -> dict:
        return {"passed": self.passed, "failed_checks": [r.name for r in self.results if not r.passed],
                "problems": self.problems}


class Verifier:
    def __init__(self, checks=None) -> None:
        self.checks = checks or ALL_CHECKS

    def verify(self, draft: DraftAnswer, ctx: TurnContext) -> Verdict:
        results = [check(draft, ctx) for check in self.checks]
        return Verdict(all(r.passed for r in results), results)
