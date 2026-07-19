"""A3 — registro durável de decisões (lógica pura).

A interface DecisionLog e o construtor de registro. A implementação Postgres
é I/O (smoke test separado); aqui travamos a serialização, o custo e o id.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from bybit_agent.agent.client import AgentResult
from bybit_agent.agent.orchestrator import ShadowCycleResult
from bybit_agent.persistence.decision_log import (
    DecisionRecord,
    InMemoryDecisionLog,
    build_record,
)
from bybit_agent.risk.engine import RiskDecision
from bybit_agent.risk.validators import Rejection


def _snapshot(symbol: str = "BTCUSDT") -> dict:
    return {"symbol": symbol, "timestamp": 1_700_000_000_000, "market_regime": "RANGE"}


def _skip_result() -> ShadowCycleResult:
    return ShadowCycleResult(
        snapshot=_snapshot(), action="NO_TRADE", intent=None, risk_decision=None,
        agent_result=None, analyzed=False, skip_reason="mercado parado",
    )


def _analyzed_result(*, usage: dict | None = None,
                     risk: RiskDecision | None = None) -> ShadowCycleResult:
    decision = {"decision_id": "abc123", "symbol": "BTCUSDT", "action": "OPEN_LONG"}
    ar = AgentResult(decision=decision, system_prompt_version="v9",
                     usage=usage or {})
    return ShadowCycleResult(
        snapshot=_snapshot(), action="OPEN_LONG", intent=None, risk_decision=risk,
        agent_result=ar, analyzed=True, skip_reason=None,
    )


# --------------------------------------------------------------------------
# InMemoryDecisionLog
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inmemory_records_and_reads_back() -> None:
    log = InMemoryDecisionLog()
    rec = build_record(_skip_result(), ts_ms=1_700_000_000_000)
    await log.record(rec)
    got = await log.recent(limit=10)
    assert len(got) == 1
    assert got[0].decision_id == rec.decision_id


@pytest.mark.asyncio
async def test_recent_respects_limit_and_order() -> None:
    log = InMemoryDecisionLog()
    for i in range(5):
        r = _skip_result()
        r.snapshot["timestamp"] = 1_700_000_000_000 + i
        await log.record(build_record(r, ts_ms=1_700_000_000_000 + i))
    recent = await log.recent(limit=2)
    assert len(recent) == 2
    # os dois mais recentes, em ordem de inserção
    assert recent[-1].ts_ms == 1_700_000_000_004


# --------------------------------------------------------------------------
# build_record
# --------------------------------------------------------------------------


def test_skip_record_has_zero_cost_and_no_decision() -> None:
    """⭐ Ciclo pulado pelo pré-filtro: custo ZERO, sem decisão do modelo."""
    rec = build_record(_skip_result(), ts_ms=123)
    assert rec.analyzed is False
    assert rec.skip_reason == "mercado parado"
    assert rec.decision is None
    assert rec.cost_usd == "0"
    assert rec.system_prompt_version is None


def test_analyzed_record_computes_cost_from_usage() -> None:
    """⭐ Custo derivado do usage do Claude — auditável por decisão."""
    usage = {"input_tokens": 1000, "output_tokens": 200,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 4000}
    rec = build_record(_analyzed_result(usage=usage), ts_ms=456)
    # 1000*5e-6 + 200*25e-6 + 4000*(5e-6*0.1) = 0.005+0.005+0.002 = 0.012
    assert Decimal(rec.cost_usd) == Decimal("0.012")
    assert rec.decision_id == "abc123"
    assert rec.system_prompt_version == "v9"


def test_record_serializes_risk_verdict() -> None:
    rd = RiskDecision(
        approved=False, quantity=None,
        rejections=[Rejection("STALE", "dados velhos")],
        policy_hash="deadbeef",
    )
    rec = build_record(_analyzed_result(risk=rd), ts_ms=789)
    assert rec.risk_verdict is not None
    assert rec.risk_verdict["approved"] is False
    assert rec.risk_verdict["rejections"][0]["code"] == "STALE"
    assert rec.risk_verdict["policy_hash"] == "deadbeef"


def test_record_is_json_serializable() -> None:
    """⭐ O registro inteiro serializa para JSON — nada de Decimal/float solto."""
    usage = {"input_tokens": 10, "output_tokens": 5,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    rec = build_record(_analyzed_result(usage=usage), ts_ms=1)
    json.dumps(rec.to_dict())  # não levanta


def test_skip_record_has_synthetic_stable_id() -> None:
    rec = build_record(_skip_result(), ts_ms=1)
    assert rec.decision_id.startswith("skip-BTCUSDT-")


@pytest.mark.asyncio
async def test_inmemory_len_reflects_records() -> None:
    log = InMemoryDecisionLog()
    assert len(log) == 0
    await log.record(build_record(_skip_result(), ts_ms=1))
    assert len(log) == 1


def test_analyzed_without_decision_id_falls_back_to_synthetic() -> None:
    """Decisão do modelo sem decision_id → id sintético, não quebra."""
    ar = AgentResult(decision={"symbol": "BTCUSDT", "action": "NO_TRADE"},
                     system_prompt_version="v", usage={})
    result = ShadowCycleResult(
        snapshot=_snapshot(), action="NO_TRADE", intent=None, risk_decision=None,
        agent_result=ar, analyzed=True, skip_reason=None,
    )
    rec = build_record(result, ts_ms=1)
    assert rec.decision_id.startswith("skip-")


def test_dataclass_record_roundtrips_dict() -> None:
    rec = DecisionRecord(
        decision_id="x", ts_ms=1, symbol="BTCUSDT", action="NO_TRADE",
        analyzed=False, skip_reason="r", snapshot={}, decision=None,
        risk_verdict=None, system_prompt_version=None, cost_usd="0",
    )
    d = rec.to_dict()
    assert d["decision_id"] == "x"
    assert d["usage"] == {}
