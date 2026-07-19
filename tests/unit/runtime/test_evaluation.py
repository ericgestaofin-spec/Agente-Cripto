"""A4 — avaliação/agregação das decisões registradas."""

from __future__ import annotations

from decimal import Decimal

from bybit_agent.persistence.decision_log import DecisionRecord
from bybit_agent.runtime.evaluation import summarize_decisions


def _rec(**over) -> DecisionRecord:
    base = dict(
        decision_id="d", ts_ms=1, symbol="BTCUSDT", action="NO_TRADE",
        analyzed=True, skip_reason=None, snapshot={}, decision=None,
        risk_verdict=None, system_prompt_version="v", cost_usd="0",
    )
    base.update(over)
    return DecisionRecord(**base)


def test_empty_records_summary_is_all_zero() -> None:
    s = summarize_decisions([])
    assert s.total == 0
    assert s.total_cost_usd == Decimal("0")
    assert s.avg_cost_per_analyzed_usd == Decimal("0")


def test_counts_analyzed_vs_skipped() -> None:
    records = [
        _rec(analyzed=True, cost_usd="0.05"),
        _rec(analyzed=False, skip_reason="mercado parado", cost_usd="0"),
        _rec(analyzed=False, skip_reason="mercado parado", cost_usd="0"),
    ]
    s = summarize_decisions(records)
    assert s.total == 3
    assert s.analyzed == 1
    assert s.skipped == 2
    assert s.skip_reasons["mercado parado"] == 2


def test_total_and_average_cost() -> None:
    """⭐ Custo total soma exato (Decimal); média é por ciclo ANALISADO."""
    records = [
        _rec(analyzed=True, cost_usd="0.04"),
        _rec(analyzed=True, cost_usd="0.06"),
        _rec(analyzed=False, skip_reason="parado", cost_usd="0"),
    ]
    s = summarize_decisions(records)
    assert s.total_cost_usd == Decimal("0.10")
    assert s.avg_cost_per_analyzed_usd == Decimal("0.05")  # 0.10/2, não /3


def test_action_distribution() -> None:
    records = [_rec(action="OPEN_LONG"), _rec(action="OPEN_LONG"),
              _rec(action="NO_TRADE")]
    s = summarize_decisions(records)
    assert s.actions == {"OPEN_LONG": 2, "NO_TRADE": 1}


def test_approval_and_rejection_histogram() -> None:
    """⭐ Taxa de aprovação e histograma de motivos de rejeição do Risk Engine."""
    records = [
        _rec(risk_verdict={"approved": True, "rejections": []}),
        _rec(risk_verdict={"approved": False,
                           "rejections": [{"code": "STALE", "detail": "x"}]}),
        _rec(risk_verdict={"approved": False,
                           "rejections": [{"code": "STALE", "detail": "y"},
                                          {"code": "RR_TOO_LOW", "detail": "z"}]}),
        _rec(risk_verdict=None),  # ciclo sem intenção não conta
    ]
    s = summarize_decisions(records)
    assert s.approvals == 1
    assert s.rejections == 2
    assert s.rejection_codes["STALE"] == 2
    assert s.rejection_codes["RR_TOO_LOW"] == 1
