"""Sprint 7 — parser: decisão do Claude → TradeIntent.

A decisão vem como JSON garantido pelo schema (output_config.format). O
parser a converte para os tipos de domínio que o Risk Engine consome.

Regras:
  - NO_TRADE / WATCH / HALT_TRADING → sem intenção (None)
  - OPEN_LONG / OPEN_SHORT → TradeIntent
  - preços parseados como Decimal, NUNCA float
  - o rr_net declarado pelo modelo vira model_claimed_rr_net (diagnóstico)
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bybit_agent.agent.parser import ParsedDecision, parse_decision
from bybit_agent.risk.engine import TradeIntent


def _open_long_decision(**over: object) -> dict:
    base = {
        "decision_id": "5f1e4d3c-2b1a-4098-8765-43210fedcba9",
        "timestamp": "2026-07-19T12:00:00Z",
        "symbol": "BTCUSDT",
        "action": "OPEN_LONG",
        "data_quality": {"status": "VALID", "snapshot_age_ms": 184, "issues": []},
        "market_regime": "TRENDING_UP",
        "setup": {"name": "TREND_PULLBACK", "timeframe": "15m", "direction": "LONG",
                  "quality_score": 72, "evidence_for": ["HTF_TREND"], "evidence_against": []},
        "entry": {"type": "LIMIT", "price": "60000.00", "price_min": None,
                  "price_max": None, "confirmation": None,
                  "expires_at": "2026-07-19T13:00:00Z"},
        "risk_plan": {
            "invalidation_price": "59500.00",
            "stop_loss": "59400.00",
            "take_profit_levels": [
                {"price": "61200.00", "close_fraction": "0.5", "reason": "máxima"},
                {"price": "61800.00", "close_fraction": "0.5", "reason": "liquidez"},
            ],
            "estimated_rr_gross": "2.6",
            "estimated_rr_net": "2.2",
            "maximum_slippage_bps": "8",
        },
        "cancellation_conditions": ["fechamento abaixo de 59500"],
        "reason_codes": ["HTF_TREND_ALIGNED"],
        "summary": "Pullback em tendência de alta.",
    }
    base.update(over)  # type: ignore[arg-type]
    return base


def _no_trade_decision() -> dict:
    d = _open_long_decision()
    d["action"] = "NO_TRADE"
    return d


# --------------------------------------------------------------------------
# Ações sem intenção
# --------------------------------------------------------------------------


def test_no_trade_yields_no_intent() -> None:
    result = parse_decision(_no_trade_decision(), now_ms=1_000)
    assert isinstance(result, ParsedDecision)
    assert result.intent is None
    assert result.action == "NO_TRADE"


def test_watch_yields_no_intent() -> None:
    d = _open_long_decision(action="WATCH")
    assert parse_decision(d, now_ms=1_000).intent is None


def test_halt_yields_no_intent_but_flags_halt() -> None:
    d = _open_long_decision(action="HALT_TRADING")
    result = parse_decision(d, now_ms=1_000)
    assert result.intent is None
    assert result.action == "HALT_TRADING"


# --------------------------------------------------------------------------
# OPEN_LONG → TradeIntent
# --------------------------------------------------------------------------


def test_open_long_yields_trade_intent() -> None:
    result = parse_decision(_open_long_decision(), now_ms=1_000)
    assert isinstance(result.intent, TradeIntent)
    intent = result.intent
    assert intent.side == "BUY"
    assert intent.entry == Decimal("60000.00")
    assert intent.stop == Decimal("59400.00")
    assert intent.invalidation == Decimal("59500.00")


def test_open_short_yields_sell_intent() -> None:
    d = _open_long_decision(action="OPEN_SHORT")
    d["setup"]["direction"] = "SHORT"  # type: ignore[index]
    d["entry"]["price"] = "60000.00"  # type: ignore[index]
    d["risk_plan"]["stop_loss"] = "60600.00"  # type: ignore[index]
    d["risk_plan"]["invalidation_price"] = "60500.00"  # type: ignore[index]
    d["risk_plan"]["take_profit_levels"] = [  # type: ignore[index]
        {"price": "58800.00", "close_fraction": "1", "reason": "alvo"},
    ]
    result = parse_decision(d, now_ms=1_000)
    assert result.intent is not None
    assert result.intent.side == "SELL"


def test_take_profit_levels_are_parsed_with_prices() -> None:
    intent = parse_decision(_open_long_decision(), now_ms=1_000).intent
    assert intent is not None
    assert len(intent.take_profit_levels) == 2
    assert intent.take_profit_levels[0].price == Decimal("61200.00")
    assert intent.take_profit_levels[0].fraction == Decimal("0.5")


def test_prices_are_decimal_never_float() -> None:
    intent = parse_decision(_open_long_decision(), now_ms=1_000).intent
    assert intent is not None
    assert isinstance(intent.entry, Decimal)
    assert isinstance(intent.stop, Decimal)
    for lvl in intent.take_profit_levels:
        assert isinstance(lvl.price, Decimal)


def test_model_rr_becomes_diagnostic() -> None:
    """O rr_net do modelo NÃO decide — vira model_claimed_rr_net."""
    intent = parse_decision(_open_long_decision(), now_ms=1_000).intent
    assert intent is not None
    assert intent.model_claimed_rr_net == Decimal("2.2")


def test_decision_id_is_preserved() -> None:
    intent = parse_decision(_open_long_decision(), now_ms=1_000).intent
    assert intent is not None
    assert intent.decision_id == "5f1e4d3c-2b1a-4098-8765-43210fedcba9"


# --------------------------------------------------------------------------
# Robustez
# --------------------------------------------------------------------------


def test_open_long_without_stop_is_rejected() -> None:
    """Uma intenção de abertura sem stop é incoerente — o parser recusa."""
    d = _open_long_decision()
    d["risk_plan"]["stop_loss"] = None  # type: ignore[index]
    with pytest.raises(ValueError, match="stop"):
        parse_decision(d, now_ms=1_000)


def test_open_long_without_entry_price_is_rejected() -> None:
    d = _open_long_decision()
    d["entry"]["price"] = None  # type: ignore[index]
    with pytest.raises(ValueError, match="entr"):
        parse_decision(d, now_ms=1_000)


def test_open_long_without_tp_is_rejected() -> None:
    d = _open_long_decision()
    d["risk_plan"]["take_profit_levels"] = []  # type: ignore[index]
    with pytest.raises(ValueError, match="take.profit|TP"):
        parse_decision(d, now_ms=1_000)


def test_float_price_in_decision_is_rejected() -> None:
    """Se o preço vier como número (float no JSON), rejeita — deve ser
    string decimal."""
    d = _open_long_decision()
    d["entry"]["price"] = 60000.0  # type: ignore[index]
    with pytest.raises((TypeError, ValueError)):
        parse_decision(d, now_ms=1_000)
