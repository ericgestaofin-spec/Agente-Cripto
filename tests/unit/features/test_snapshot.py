"""Sprint 3 — montagem do snapshot de mercado.

Converte dados brutos (candles, book, ticker, spec) num snapshot compacto e
objetivo — a entrada que o Claude recebe. Determinístico: o `now_ms` e a
idade dos dados são injetados, não lidos de um relógio interno.

Campo faltante vira None, nunca 0 — o modelo trata 0 como valor real.
"""

from __future__ import annotations

from decimal import Decimal

from bybit_agent.features.snapshot import build_snapshot
from bybit_agent.marketdata.rest import BookLevel, Candle, OrderBook, Ticker


def _candles(n: int, base: int = 64000) -> list[Candle]:
    out = []
    for i in range(n):
        price = Decimal(base + i * 10)
        out.append(Candle(
            start_ms=1_700_000_000_000 + i * 300_000,
            open=price, high=price + 50, low=price - 50, close=price + 10,
            volume=Decimal("10"), turnover=Decimal("640000"),
        ))
    return out


def _orderbook() -> OrderBook:
    return OrderBook(
        symbol="BTCUSDT",
        bids=(BookLevel(Decimal("64500"), Decimal("1.5")),
              BookLevel(Decimal("64499"), Decimal("2.0"))),
        asks=(BookLevel(Decimal("64501"), Decimal("1.2")),
              BookLevel(Decimal("64502"), Decimal("3.0"))),
        ts_ms=1_700_000_000_000, update_id=1,
    )


def _ticker() -> Ticker:
    return Ticker(
        symbol="BTCUSDT", last_price=Decimal("64500"), mark_price=Decimal("64501"),
        index_price=Decimal("64500"), bid1_price=Decimal("64500"),
        ask1_price=Decimal("64501"), funding_rate=Decimal("0.0001"),
        open_interest=Decimal("50000"),
    )


def _build(**over: object):
    kwargs = {
        "symbol": "BTCUSDT",
        "candles_by_tf": {"5": _candles(60)},
        "orderbook": _orderbook(),
        "ticker": _ticker(),
        "now_ms": 1_700_000_000_000 + 60 * 300_000,
        "data_ts_ms": 1_700_000_000_000 + 60 * 300_000 - 200,
    }
    kwargs.update(over)
    return build_snapshot(**kwargs)  # type: ignore[arg-type]


def test_snapshot_has_required_top_level_fields() -> None:
    s = _build()
    assert s["symbol"] == "BTCUSDT"
    assert "timestamp" in s
    assert "data_age_ms" in s
    assert "market_regime" in s
    assert "volatility" in s
    assert "liquidity" in s
    assert "derivatives" in s


def test_snapshot_data_age_is_computed() -> None:
    s = _build(now_ms=1000, data_ts_ms=800)
    assert s["data_age_ms"] == 200


def test_snapshot_liquidity_has_spread_bps() -> None:
    s = _build()
    assert Decimal(s["liquidity"]["spread_bps"]) > 0


def test_snapshot_derivatives_has_funding() -> None:
    s = _build()
    assert s["derivatives"]["funding_rate"] == "0.0001"


def test_snapshot_contains_no_float_values() -> None:
    """⭐ Preços trafegam como string decimal. NENHUM valor no snapshot é
    float — verificado recursivamente. (Uma string decimal como '1.3E-7' é
    exata e legítima; o proibido é o tipo float.)"""
    import json

    def walk(node: object) -> None:
        if isinstance(node, float):
            raise AssertionError(f"float encontrado no snapshot: {node!r}")
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    s = _build()
    walk(s)
    json.dumps(s)  # serializa sem erro
    assert isinstance(s["liquidity"]["spread_bps"], str)
    assert isinstance(s["derivatives"]["funding_rate"], str)


def test_snapshot_volatility_none_when_insufficient_candles() -> None:
    """⭐ Poucos candles → ATR None, não 0."""
    s = _build(candles_by_tf={"5": _candles(2)})
    assert s["volatility"]["atr"] is None


def test_snapshot_regime_trending_up_on_rising_series() -> None:
    """Série claramente crescente → regime de alta."""
    s = _build(candles_by_tf={"5": _candles(60, base=60000)})  # sobe 10/candle
    assert s["market_regime"] in ("TRENDING_UP", "TRANSITION")


def test_snapshot_regime_unknown_when_insufficient() -> None:
    s = _build(candles_by_tf={"5": _candles(3)})
    assert s["market_regime"] == "UNKNOWN"


def test_snapshot_has_structure_block() -> None:
    """⭐ A2: o snapshot carrega a leitura estrutural (swings, BOS)."""
    s = _build()
    st = s["structure"]
    assert st["trend"] in ("UP", "DOWN", "RANGE", "UNKNOWN")
    assert "last_swing_high" in st
    assert "bos" in st
    assert isinstance(st["choch"], bool)


def test_snapshot_liquidity_has_imbalance_and_depth() -> None:
    s = _build()
    liq = s["liquidity"]
    assert isinstance(liq["imbalance"], str)
    assert isinstance(liq["bid_depth"], str)
    assert isinstance(liq["ask_depth"], str)


def test_snapshot_multi_timeframe_summarizes_each_tf() -> None:
    """⭐ A2: alinhamento multi-timeframe — regime e tendência por TF."""
    s = _build(candles_by_tf={"60": _candles(60), "5": _candles(60)})
    mtf = s["multi_timeframe"]
    assert set(mtf) == {"60", "5"}
    assert "regime" in mtf["5"] and "trend" in mtf["5"]


def test_snapshot_is_deterministic() -> None:
    assert _build() == _build()
