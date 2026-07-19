"""A2 — liquidez do book: imbalance, profundidade e slippage de impacto.

Books sintéticos com números redondos para verificar o cálculo à mão.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bybit_agent.features.liquidity import (
    book_imbalance,
    depth_within_bps,
    estimate_slippage_bps,
    summarize_liquidity,
)
from bybit_agent.marketdata.rest import BookLevel, OrderBook


def _ob(bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> OrderBook:
    return OrderBook(
        symbol="BTCUSDT",
        bids=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in bids),
        asks=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in asks),
        ts_ms=1_700_000_000_000, update_id=1,
    )


# --------------------------------------------------------------------------
# Imbalance
# --------------------------------------------------------------------------


def test_imbalance_positive_when_more_bids() -> None:
    ob = _ob(bids=[("100", "8")], asks=[("101", "2")])
    # (8-2)/(8+2) = 0.6
    assert book_imbalance(ob) == Decimal("0.6")


def test_imbalance_negative_when_more_asks() -> None:
    ob = _ob(bids=[("100", "2")], asks=[("101", "8")])
    assert book_imbalance(ob) == Decimal("-0.6")


def test_imbalance_zero_when_balanced() -> None:
    ob = _ob(bids=[("100", "5")], asks=[("101", "5")])
    assert book_imbalance(ob) == Decimal("0")


def test_imbalance_zero_on_empty_book() -> None:
    """Book vazio de um lado não divide por zero — retorna 0."""
    ob = OrderBook(symbol="BTCUSDT", bids=(), asks=(),
                   ts_ms=1_700_000_000_000, update_id=1)
    assert book_imbalance(ob) == Decimal("0")


def test_imbalance_only_counts_top_levels() -> None:
    ob = _ob(bids=[("100", "5"), ("99", "100")],
             asks=[("101", "5"), ("102", "100")])
    # com levels=1 só conta o topo → equilíbrio
    assert book_imbalance(ob, levels=1) == Decimal("0")


# --------------------------------------------------------------------------
# Profundidade dentro de bps
# --------------------------------------------------------------------------


def test_depth_sums_sizes_within_band() -> None:
    # mid = 100.55. 20 bps ≈ 0.201 → alcança 100.35..100.75
    ob = _ob(bids=[("100.5", "1"), ("100.4", "2"), ("100.0", "9")],
             asks=[("100.6", "3"), ("101.5", "9")])
    assert depth_within_bps(ob, bps=Decimal("20"), side="BID") == Decimal("3")
    assert depth_within_bps(ob, bps=Decimal("20"), side="ASK") == Decimal("3")


def test_depth_rejects_bad_side() -> None:
    ob = _ob(bids=[("100", "1")], asks=[("101", "1")])
    with pytest.raises(ValueError, match="side"):
        depth_within_bps(ob, bps=Decimal("10"), side="LONG")


# --------------------------------------------------------------------------
# Slippage de impacto (caminhando o book)
# --------------------------------------------------------------------------


def test_slippage_zero_when_fits_at_touch() -> None:
    """Preenche todo no melhor ask → sem impacto além do toque."""
    ob = _ob(bids=[("100", "10")], asks=[("101", "10")])
    slip = estimate_slippage_bps(ob, side="BUY", quantity=Decimal("5"))
    assert slip == Decimal("0")


def test_buy_slippage_walks_into_deeper_levels() -> None:
    """⭐ Ordem maior que o topo consome níveis piores → slippage > 0."""
    ob = _ob(bids=[("99", "10")], asks=[("100", "2"), ("110", "8")])
    # comprar 4: 2@100 + 2@110 → avg 105; ref 100 → (105-100)/100*10000 = 500 bps
    slip = estimate_slippage_bps(ob, side="BUY", quantity=Decimal("4"))
    assert slip == Decimal("500")


def test_sell_slippage_walks_bids_down() -> None:
    ob = _ob(bids=[("100", "2"), ("90", "8")], asks=[("101", "10")])
    # vender 4: 2@100 + 2@90 → avg 95; ref 100 → (100-95)/100*10000 = 500 bps
    slip = estimate_slippage_bps(ob, side="SELL", quantity=Decimal("4"))
    assert slip == Decimal("500")


def test_slippage_none_when_book_too_thin() -> None:
    """⭐ Book raso demais → None, nunca finge que preencheu."""
    ob = _ob(bids=[("99", "1")], asks=[("100", "1")])
    assert estimate_slippage_bps(ob, side="BUY", quantity=Decimal("50")) is None


def test_slippage_rejects_non_positive_quantity() -> None:
    ob = _ob(bids=[("99", "1")], asks=[("100", "1")])
    with pytest.raises(ValueError):
        estimate_slippage_bps(ob, side="BUY", quantity=Decimal("0"))


def test_slippage_rejects_bad_side() -> None:
    ob = _ob(bids=[("99", "1")], asks=[("100", "1")])
    with pytest.raises(ValueError, match="side"):
        estimate_slippage_bps(ob, side="BID", quantity=Decimal("1"))


# --------------------------------------------------------------------------
# Resumo
# --------------------------------------------------------------------------


def test_summary_bundles_metrics() -> None:
    ob = _ob(bids=[("100.5", "4"), ("100.0", "9")],
             asks=[("100.6", "2"), ("101.5", "9")])
    summary = summarize_liquidity(ob, depth_bps=Decimal("10"))
    assert summary.imbalance == book_imbalance(ob)
    assert summary.bid_depth == Decimal("4")
    assert summary.ask_depth == Decimal("2")
