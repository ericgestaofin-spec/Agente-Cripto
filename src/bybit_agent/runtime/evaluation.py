"""Avaliação das decisões registradas — o retorno sobre o histórico.

Lê uma lista de `DecisionRecord` (do DecisionLog) e produz um resumo
auditável: quantos ciclos analisaram vs pularam, custo total e médio,
distribuição de ações, taxa de aprovação do Risk Engine e o histograma dos
motivos de rejeição e de skip.

Puro e determinístico — só agrega. Base para calibrar custo (§1.5) e para o
review de decisões: onde o analista mais gasta, o que o Risk Engine mais
barra, quando o pré-filtro mais poupa.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from bybit_agent.persistence.decision_log import DecisionRecord


@dataclass(frozen=True, slots=True)
class DecisionSummary:
    total: int
    analyzed: int
    skipped: int
    total_cost_usd: Decimal
    avg_cost_per_analyzed_usd: Decimal
    actions: dict[str, int] = field(default_factory=dict)
    skip_reasons: dict[str, int] = field(default_factory=dict)
    rejection_codes: dict[str, int] = field(default_factory=dict)
    approvals: int = 0
    rejections: int = 0


def summarize_decisions(records: list[DecisionRecord]) -> DecisionSummary:
    """Agrega uma lista de registros num resumo. Lista vazia → tudo zero."""
    total = len(records)
    analyzed = sum(1 for r in records if r.analyzed)
    skipped = total - analyzed

    total_cost = sum((Decimal(r.cost_usd) for r in records), Decimal("0"))
    avg_cost = total_cost / Decimal(analyzed) if analyzed else Decimal("0")

    actions: Counter[str] = Counter(r.action for r in records)
    skip_reasons: Counter[str] = Counter(
        r.skip_reason for r in records if r.skip_reason is not None
    )

    rejection_codes: Counter[str] = Counter()
    approvals = 0
    rejections = 0
    for r in records:
        verdict = r.risk_verdict
        if verdict is None:
            continue
        if verdict.get("approved"):
            approvals += 1
        else:
            rejections += 1
            for rej in verdict.get("rejections", []):
                rejection_codes[rej["code"]] += 1

    return DecisionSummary(
        total=total,
        analyzed=analyzed,
        skipped=skipped,
        total_cost_usd=total_cost,
        avg_cost_per_analyzed_usd=avg_cost,
        actions=dict(actions),
        skip_reasons=dict(skip_reasons),
        rejection_codes=dict(rejection_codes),
        approvals=approvals,
        rejections=rejections,
    )
