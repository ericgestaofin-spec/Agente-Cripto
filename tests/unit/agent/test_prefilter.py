"""A2 — pré-filtro determinístico (lever de custo).

Cada teste trava uma regra do gate que decide se vale gastar uma chamada ao
Claude. Conservador: na dúvida, analisa.
"""

from __future__ import annotations

from decimal import Decimal

from bybit_agent.agent.prefilter import PrefilterConfig, prefilter
from bybit_agent.features.structure import MarketStructure


def _snapshot(*, regime: str = "RANGE", spread: str = "1",
              rv: str | None = "0.0005", dq: str = "VALID") -> dict:
    return {
        "market_regime": regime,
        "liquidity": {"spread_bps": spread},
        "volatility": {"realized_volatility": rv},
        "data_quality": {"status": dq},
    }


def _flat() -> MarketStructure:
    return MarketStructure("RANGE", Decimal("100"), Decimal("90"), None, False)


# --------------------------------------------------------------------------
# Pular (não gastar)
# --------------------------------------------------------------------------


def test_skips_on_conflicting_data() -> None:
    """⭐ Dados incoerentes: analisar seria lixo — pula."""
    res = prefilter(_snapshot(dq="CONFLICTING"), _flat())
    assert res.should_analyze is False
    assert "incoerente" in res.reason


def test_skips_on_wide_spread() -> None:
    res = prefilter(_snapshot(spread="20"), _flat(),
                    config=PrefilterConfig(max_spread_bps=Decimal("5")))
    assert res.should_analyze is False
    assert "spread" in res.reason


def test_skips_flat_market_with_no_event() -> None:
    """⭐ Range, sem quebra, vol baixa → mercado parado, não gasta."""
    res = prefilter(_snapshot(regime="RANGE", rv="0.0001"), _flat(),
                    config=PrefilterConfig(min_realized_vol=Decimal("0.001")))
    assert res.should_analyze is False
    assert "parado" in res.reason


def test_skips_when_volatility_missing_and_no_event() -> None:
    res = prefilter(_snapshot(regime="RANGE", rv=None), _flat())
    assert res.should_analyze is False


# --------------------------------------------------------------------------
# Analisar (vale a chamada)
# --------------------------------------------------------------------------


def test_analyzes_on_break_of_structure() -> None:
    """⭐ BOS é evento — sempre vale analisar, mesmo em range."""
    st = MarketStructure("RANGE", Decimal("100"), Decimal("90"), "BULLISH", False)
    res = prefilter(_snapshot(regime="RANGE", rv="0.0001"), st)
    assert res.should_analyze is True
    assert "BOS" in res.reason


def test_analyzes_on_choch() -> None:
    st = MarketStructure("UP", Decimal("100"), Decimal("90"), "BEARISH", True)
    res = prefilter(_snapshot(), st)
    assert res.should_analyze is True


def test_analyzes_on_clear_trend() -> None:
    res = prefilter(_snapshot(regime="TRENDING_UP", rv="0.0001"), _flat())
    assert res.should_analyze is True
    assert "tend" in res.reason.lower()


def test_analyzes_when_volatility_sufficient() -> None:
    res = prefilter(_snapshot(regime="RANGE", rv="0.005"), _flat(),
                    config=PrefilterConfig(min_realized_vol=Decimal("0.001")))
    assert res.should_analyze is True
    assert "volatil" in res.reason.lower()


def test_default_config_is_conservative() -> None:
    """Sem config, um mercado com evento passa; usa defaults sensatos."""
    st = MarketStructure("RANGE", Decimal("100"), Decimal("90"), "BULLISH", False)
    res = prefilter(_snapshot(), st)
    assert res.should_analyze is True
