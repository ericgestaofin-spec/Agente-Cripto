"""A2 — estrutura de mercado (swings, tendência, BOS, CHoCH).

Dados sintéticos com swings verificáveis à mão. Cada teste trava uma regra
de price action que o Claude usa para contextualizar a decisão.
"""

from __future__ import annotations

from decimal import Decimal

from bybit_agent.features.structure import (
    analyze_structure,
    find_swings,
)
from bybit_agent.marketdata.rest import Candle


def _candles(highs: list[float], lows: list[float],
             closes: list[float] | None = None) -> list[Candle]:
    """Constrói candles a partir de highs/lows explícitos. close = meio se
    não fornecido."""
    n = len(highs)
    cl = closes if closes is not None else [(highs[i] + lows[i]) / 2 for i in range(n)]
    return [
        Candle(start_ms=1_700_000_000_000 + i * 300_000,
               open=Decimal(str(cl[i])), high=Decimal(str(highs[i])),
               low=Decimal(str(lows[i])), close=Decimal(str(cl[i])),
               volume=Decimal("10"), turnover=Decimal("600000"))
        for i in range(n)
    ]


# --------------------------------------------------------------------------
# Swings fractais
# --------------------------------------------------------------------------


def test_swing_high_needs_lower_neighbors_both_sides() -> None:
    # highs: pico claro em i=1 e i=3 (window=1)
    candles = _candles(highs=[10, 13, 11, 16, 12], lows=[5, 8, 6, 9, 7])
    swings = find_swings(candles, window=1)
    highs = [s for s in swings if s.kind == "HIGH"]
    assert [s.index for s in highs] == [1, 3]
    assert [s.price for s in highs] == [Decimal("13"), Decimal("16")]


def test_swing_low_needs_higher_neighbors_both_sides() -> None:
    candles = _candles(highs=[10, 12, 11, 14, 13], lows=[5, 3, 6, 4, 7])
    swings = find_swings(candles, window=1)
    lows = [s for s in swings if s.kind == "LOW"]
    assert [s.index for s in lows] == [1, 3]
    assert [s.price for s in lows] == [Decimal("3"), Decimal("4")]


def test_last_candles_are_never_swings() -> None:
    """⭐ Os últimos `window` candles não têm confirmação — nunca são swings."""
    candles = _candles(highs=[10, 11, 12, 20], lows=[5, 4, 3, 1])
    swings = find_swings(candles, window=1)
    # índice 3 (último) é o maior high, mas não pode ser swing confirmado.
    assert all(s.index != 3 for s in swings)


def test_window_two_needs_two_neighbors_each_side() -> None:
    candles = _candles(highs=[10, 11, 15, 11, 10, 12, 9],
                       lows=[5, 4, 6, 4, 3, 5, 2])
    swings = find_swings(candles, window=2)
    highs = [s for s in swings if s.kind == "HIGH"]
    assert [s.index for s in highs] == [2]  # 15 é o único pico com 2 vizinhos menores


# --------------------------------------------------------------------------
# Tendência
# --------------------------------------------------------------------------


def test_uptrend_is_higher_highs_and_higher_lows() -> None:
    candles = _candles(highs=[10, 13, 11, 16, 12], lows=[5, 3, 6, 4, 7],
                       closes=[9, 12, 10, 15, 11])
    st = analyze_structure(candles, window=1)
    assert st.trend == "UP"
    assert st.last_swing_high == Decimal("16")
    assert st.last_swing_low == Decimal("4")


def test_downtrend_is_lower_highs_and_lower_lows() -> None:
    # dois swing highs descendentes (18, 14) e dois swing lows (9, 5).
    candles = _candles(highs=[20, 16, 18, 12, 14, 8], lows=[15, 9, 13, 5, 11, 3],
                       closes=[17, 12, 15, 8, 12, 10])
    st = analyze_structure(candles, window=1)
    assert st.trend == "DOWN"
    assert st.last_swing_high == Decimal("14")
    assert st.last_swing_low == Decimal("5")


def test_conflicting_swings_are_range() -> None:
    """⭐ Highs subindo mas lows descendo (expansão) → sem tendência: RANGE."""
    candles = _candles(highs=[10, 13, 11, 16, 12], lows=[5, 2, 6, 1, 7],
                       closes=[9, 12, 10, 15, 10])
    st = analyze_structure(candles, window=1)
    assert st.trend == "RANGE"


def test_insufficient_candles_is_unknown() -> None:
    candles = _candles(highs=[10, 11], lows=[5, 4])
    st = analyze_structure(candles, window=2)
    assert st.trend == "UNKNOWN"
    assert st.last_swing_high is None
    assert st.bos is None


# --------------------------------------------------------------------------
# BOS / CHoCH
# --------------------------------------------------------------------------


def test_bullish_bos_when_close_breaks_last_swing_high() -> None:
    """⭐ Fechamento rompe o último swing high confirmado → BOS de alta."""
    candles = _candles(highs=[10, 13, 11, 16, 12], lows=[5, 3, 6, 4, 7],
                       closes=[9, 12, 10, 15, 17])  # último close 17 > swing high 16
    st = analyze_structure(candles, window=1)
    assert st.bos == "BULLISH"


def test_bearish_bos_when_close_breaks_last_swing_low() -> None:
    candles = _candles(highs=[16, 13, 14, 11, 12], lows=[9, 7, 8, 5, 6],
                       closes=[15, 12, 13, 10, 4])  # último close 4 < swing low 5
    st = analyze_structure(candles, window=1)
    assert st.bos == "BEARISH"


def test_no_bos_when_close_inside_structure() -> None:
    candles = _candles(highs=[10, 13, 11, 16, 12], lows=[5, 3, 6, 4, 7],
                       closes=[9, 12, 10, 15, 11])  # 11 dentro do range
    st = analyze_structure(candles, window=1)
    assert st.bos is None
    assert st.choch is False


def test_choch_when_bos_opposes_trend() -> None:
    """⭐ Tendência de alta mas fechamento rompe o swing low → CHoCH."""
    # HH/HL (alta) mas o último candle despenca abaixo do último swing low.
    candles = _candles(highs=[10, 13, 11, 16, 12], lows=[5, 3, 6, 4, 7],
                       closes=[9, 12, 10, 15, 3])  # 3 < swing low 4 → BOS baixa
    st = analyze_structure(candles, window=1)
    assert st.trend == "UP"
    assert st.bos == "BEARISH"
    assert st.choch is True
