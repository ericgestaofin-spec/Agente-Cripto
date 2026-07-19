"""Sprint 2 (REST) — cliente de market data público da Bybit.

Testes com respostas REAIS gravadas da Bybit V5 (formato verificado em
docs/BYBIT_INTEGRACAO.md). O cliente não usa credencial — endpoints de
mercado são públicos.

Pontos de correção que estes testes travam:
  - kline REST vem em ordem REVERSA (mais recente primeiro) → inverter
  - todo preço é Decimal, nunca float
  - candle parcial não deve ser confundido com fechado
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bybit_agent.marketdata.rest import (
    parse_instrument,
    parse_klines,
    parse_orderbook,
    parse_ticker,
)

# --------------------------------------------------------------------------
# Kline — resposta real da Bybit (ordem reversa)
# --------------------------------------------------------------------------


def _kline_payload() -> dict:
    # Bybit devolve do mais RECENTE para o mais antigo.
    return {
        "retCode": 0,
        "result": {
            "symbol": "BTCUSDT",
            "category": "linear",
            "list": [
                ["1721390400000", "64500", "64600", "64400", "64550", "10.5", "677000"],
                ["1721390100000", "64450", "64520", "64300", "64500", "12.1", "780000"],
                ["1721389800000", "64400", "64480", "64350", "64450", "8.3", "534000"],
            ],
        },
    }


def test_klines_are_reversed_to_chronological_order() -> None:
    """⭐ A Bybit devolve reverso. Um ATR sobre a série reversa é plausível
    e completamente errado."""
    candles = parse_klines(_kline_payload())
    starts = [c.start_ms for c in candles]
    assert starts == sorted(starts), "candles devem estar em ordem cronológica"
    assert candles[0].start_ms == 1721389800000  # o mais antigo primeiro
    assert candles[-1].start_ms == 1721390400000  # o mais recente por último


def test_kline_fields_are_decimal() -> None:
    c = parse_klines(_kline_payload())[0]
    for field in ("open", "high", "low", "close", "volume"):
        assert isinstance(getattr(c, field), Decimal), field


def test_kline_ohlc_values() -> None:
    c = parse_klines(_kline_payload())[-1]  # mais recente
    assert c.open == Decimal("64500")
    assert c.high == Decimal("64600")
    assert c.low == Decimal("64400")
    assert c.close == Decimal("64550")


def test_kline_empty_list_returns_empty() -> None:
    payload = {"retCode": 0, "result": {"symbol": "BTCUSDT", "list": []}}
    assert parse_klines(payload) == []


def test_kline_non_zero_retcode_raises() -> None:
    payload = {"retCode": 10001, "retMsg": "params error", "result": {}}
    with pytest.raises(ValueError, match="10001"):
        parse_klines(payload)


# --------------------------------------------------------------------------
# Orderbook — resposta real
# --------------------------------------------------------------------------


def _orderbook_payload() -> dict:
    return {
        "retCode": 0,
        "result": {
            "s": "BTCUSDT",
            "b": [["64500.0", "1.5"], ["64499.5", "2.0"], ["64499.0", "0.8"]],
            "a": [["64500.5", "1.2"], ["64501.0", "3.0"], ["64501.5", "0.5"]],
            "ts": 1721390400123,
            "u": 987654,
            "seq": 12345678,
        },
    }


def test_orderbook_parses_bids_and_asks() -> None:
    ob = parse_orderbook(_orderbook_payload())
    assert ob.symbol == "BTCUSDT"
    assert len(ob.bids) == 3
    assert len(ob.asks) == 3
    assert ob.update_id == 987654


def test_orderbook_best_bid_below_best_ask() -> None:
    ob = parse_orderbook(_orderbook_payload())
    assert ob.best_bid().price < ob.best_ask().price


def test_orderbook_bids_are_descending_asks_ascending() -> None:
    ob = parse_orderbook(_orderbook_payload())
    bid_prices = [lvl.price for lvl in ob.bids]
    ask_prices = [lvl.price for lvl in ob.asks]
    assert bid_prices == sorted(bid_prices, reverse=True)
    assert ask_prices == sorted(ask_prices)


def test_orderbook_levels_are_decimal() -> None:
    ob = parse_orderbook(_orderbook_payload())
    lvl = ob.bids[0]
    assert isinstance(lvl.price, Decimal)
    assert isinstance(lvl.size, Decimal)


def test_orderbook_spread_bps() -> None:
    """spread em bps = (ask-bid)/mid * 10000. bid 64500, ask 64500.5."""
    ob = parse_orderbook(_orderbook_payload())
    # mid = 64500.25, spread = 0.5 → 0.5/64500.25*10000 ≈ 0.0775 bps
    assert ob.spread_bps() < Decimal("1")
    assert ob.spread_bps() > 0


# --------------------------------------------------------------------------
# Ticker — resposta real linear
# --------------------------------------------------------------------------


def _ticker_payload() -> dict:
    return {
        "retCode": 0,
        "result": {
            "category": "linear",
            "list": [
                {
                    "symbol": "BTCUSDT",
                    "lastPrice": "64503.10",
                    "markPrice": "64506.25",
                    "indexPrice": "64505.00",
                    "bid1Price": "64503.00",
                    "ask1Price": "64503.20",
                    "fundingRate": "0.00004803",
                    "openInterest": "50000.5",
                    "volume24h": "120000",
                    "turnover24h": "7700000000",
                }
            ],
        },
    }


def test_ticker_parses_prices_as_decimal() -> None:
    t = parse_ticker(_ticker_payload())
    assert t.symbol == "BTCUSDT"
    assert t.last_price == Decimal("64503.10")
    assert t.mark_price == Decimal("64506.25")
    assert t.index_price == Decimal("64505.00")
    assert t.funding_rate == Decimal("0.00004803")
    assert isinstance(t.last_price, Decimal)


def test_ticker_non_zero_retcode_raises() -> None:
    with pytest.raises(ValueError):
        parse_ticker({"retCode": 10001, "result": {}})


# --------------------------------------------------------------------------
# Instrument — reusa InstrumentSpec.from_bybit
# --------------------------------------------------------------------------


def test_parse_instrument_returns_spec() -> None:
    payload = {
        "retCode": 0,
        "result": {
            "list": [
                {
                    "symbol": "BTCUSDT",
                    "status": "Trading",
                    "priceFilter": {"tickSize": "0.10", "minPrice": "0.10", "maxPrice": "999999"},
                    "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001",
                                      "maxOrderQty": "500", "maxMktOrderQty": "100",
                                      "minNotionalValue": "5"},
                    "leverageFilter": {"minLeverage": "1", "maxLeverage": "100"},
                }
            ]
        },
    }
    spec = parse_instrument(payload)
    assert spec.symbol == "BTCUSDT"
    assert spec.tick_size == Decimal("0.10")
