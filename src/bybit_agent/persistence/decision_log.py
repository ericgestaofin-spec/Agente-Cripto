"""Registro durável de decisões — a memória de auditoria do analista.

Toda decisão de um ciclo (inclusive as PULADAS pelo pré-filtro) vira um
`DecisionRecord` imutável e serializável. Isso alimenta a auditoria e a
avaliação posterior (A4): comparar o que o analista viu, o que decidiu, o
que o Risk Engine sentenciou e quanto custou.

A `DecisionLog` é uma interface (Protocol) — o loop não sabe se por trás
há memória, Postgres ou um arquivo. `InMemoryDecisionLog` serve testes e
dev; `PostgresDecisionLog` (persistence/postgres.py) é a produção.

Determinístico: `ts_ms` é injetado, nunca lido de relógio interno.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from bybit_agent.marketdata.scheduler import estimate_cost_usd

if TYPE_CHECKING:
    from bybit_agent.agent.orchestrator import ShadowCycleResult
    from bybit_agent.risk.engine import RiskDecision


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision_id: str
    ts_ms: int
    symbol: str
    action: str
    analyzed: bool
    skip_reason: str | None
    snapshot: dict[str, Any]
    decision: dict[str, Any] | None
    risk_verdict: dict[str, Any] | None
    system_prompt_version: str | None
    cost_usd: str | None
    usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DecisionLog(Protocol):
    async def record(self, rec: DecisionRecord) -> None: ...
    async def recent(self, *, limit: int = 50) -> list[DecisionRecord]: ...


class InMemoryDecisionLog:
    """Log volátil — testes e execução efêmera. Mantém ordem de inserção."""

    def __init__(self) -> None:
        self._records: list[DecisionRecord] = []

    async def record(self, rec: DecisionRecord) -> None:
        self._records.append(rec)

    async def recent(self, *, limit: int = 50) -> list[DecisionRecord]:
        return self._records[-limit:]

    def __len__(self) -> int:
        return len(self._records)


def _serialize_verdict(rd: RiskDecision) -> dict[str, Any]:
    return {
        "approved": rd.approved,
        "quantity": str(rd.quantity.value) if rd.quantity is not None else None,
        "binding_constraint": rd.binding_constraint,
        "computed_rr_net": (
            str(rd.computed_rr_net) if rd.computed_rr_net is not None else None
        ),
        "policy_hash": rd.policy_hash,
        "rejections": [{"code": r.code, "detail": r.detail} for r in rd.rejections],
    }


def build_record(result: ShadowCycleResult, *, ts_ms: int) -> DecisionRecord:
    """Converte o resultado de um ciclo num registro durável.

    O custo é derivado do `usage` do Claude quando houve análise; em ciclos
    pulados pelo pré-filtro o custo é zero (nenhuma chamada foi feita).
    """
    ar = result.agent_result
    usage = dict(ar.usage) if ar is not None else {}
    cost = cost_of(result)
    decision_id = _decision_id(result)

    return DecisionRecord(
        decision_id=decision_id,
        ts_ms=ts_ms,
        symbol=result.snapshot.get("symbol", "UNKNOWN"),
        action=result.action,
        analyzed=result.analyzed,
        skip_reason=result.skip_reason,
        snapshot=result.snapshot,
        decision=ar.decision if ar is not None else None,
        risk_verdict=(
            _serialize_verdict(result.risk_decision)
            if result.risk_decision is not None
            else None
        ),
        system_prompt_version=ar.system_prompt_version if ar is not None else None,
        cost_usd=str(cost),
        usage=usage,
    )


def cost_of(result: ShadowCycleResult) -> Decimal:
    """Custo em USD de um ciclo, a partir do usage do Claude. Zero se pulado."""
    ar = result.agent_result
    if ar is None or not ar.usage:
        return Decimal("0")
    return _cost_from_usage(dict(ar.usage))


def _cost_from_usage(usage: dict[str, int]) -> Decimal:
    return estimate_cost_usd(
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
    )


def _decision_id(result: ShadowCycleResult) -> str:
    ar = result.agent_result
    if ar is not None and isinstance(ar.decision, dict):
        did = ar.decision.get("decision_id")
        if did:
            return str(did)
    # Ciclo pulado (sem decisão do modelo): id sintético estável pelo ts.
    return f"skip-{result.snapshot.get('symbol', 'NA')}-{result.snapshot.get('timestamp', 0)}"
