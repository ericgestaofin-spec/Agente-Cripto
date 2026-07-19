"""A1 — coleta coerente e validação de dados de mercado.

Coleta concorrente (fontes ~ mesmo instante), frescor honesto via relógio
corrigido, e validação que pega as incoerências que o Claude flagrou ao
vivo: book cruzado e last fora do book.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bybit_agent.marketdata.clock import ClockSkew
from bybit_agent.marketdata.coherent import (
    CoherentMarketData,
    fetch_coherent,
    validate_market_data,
)
from bybit_agent.marketdata.rest import BookLevel, Candle, OrderBook, Ticker


def _candles(n: int = 30) -> list[Candle]:
    return [
        Candle(start_ms=1_700_000_000_000 + i * 300_000,
               open=Decimal("60000"), high=Decimal("60100"), low=Decimal("59900"),
               close=Decimal("60050"), volume=Decimal("10"), turnover=Decimal("6"))
        for i in range(n)
    ]


def _ob(bid: str = "60050", ask: str = "60051", ts: int = 1_700_000_000_000) -> OrderBook:
    return OrderBook(
        symbol="BTCUSDT",
        bids=(BookLevel(Decimal(bid), Decimal("2")), BookLevel(Decimal("60049"), Decimal("3"))),
        asks=(BookLevel(Decimal(ask), Decimal("2")), BookLevel(Decimal("60052"), Decimal("3"))),
        ts_ms=ts, update_id=1,
    )


def _ticker(last: str = "60050") -> Ticker:
    return Ticker(symbol="BTCUSDT", last_price=Decimal(last), mark_price=Decimal("60050"),
                  index_price=Decimal("60050"), bid1_price=Decimal("60050"),
                  ask1_price=Decimal("60051"), funding_rate=Decimal("0.0001"),
                  open_interest=Decimal("50000"))


class _FakeMarket:
    def __init__(self, ob: OrderBook, ticker: Ticker, candles: list[Candle]) -> None:
        self._ob, self._ticker, self._candles = ob, ticker, candles
        self.calls: list[str] = []

    async def instrument(self, symbol: str = "BTCUSDT"):  # noqa: ANN201
        from bybit_agent.domain.instrument import InstrumentSpec
        self.calls.append("instrument")
        return InstrumentSpec.from_bybit({
            "symbol": "BTCUSDT", "status": "Trading",
            "priceFilter": {"tickSize": "0.10", "minPrice": "0.10", "maxPrice": "999999"},
            "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001",
                              "maxOrderQty": "500", "maxMktOrderQty": "100",
                              "minNotionalValue": "5"},
            "leverageFilter": {"minLeverage": "1", "maxLeverage": "100"}})

    async def klines(self, symbol="BTCUSDT", interval="5", limit=200):  # noqa: ANN001,ANN201
        self.calls.append(f"klines:{interval}")
        return self._candles

    async def orderbook(self, symbol="BTCUSDT", depth=50):  # noqa: ANN001,ANN201
        self.calls.append("orderbook")
        return self._ob

    async def ticker(self, symbol="BTCUSDT"):  # noqa: ANN001,ANN201
        self.calls.append("ticker")
        return self._ticker


# --------------------------------------------------------------------------
# Coleta coerente
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_collects_all_sources() -> None:
    market = _FakeMarket(_ob(), _ticker(), _candles())
    data = await fetch_coherent(
        market, timeframes=["5"], clock=ClockSkew(0, 500), local_now_ms=1_700_000_010_000
    )
    assert isinstance(data, CoherentMarketData)
    assert "5" in data.candles_by_tf
    assert data.orderbook.symbol == "BTCUSDT"
    assert data.ticker.symbol == "BTCUSDT"


@pytest.mark.asyncio
async def test_data_age_uses_corrected_time_and_book_ts() -> None:
    """frescor = agora corrigido − ts do book. Nunca negativo com relógio são."""
    data = await fetch_coherent(
        _FakeMarket(_ob(ts=1_700_000_000_000), _ticker(), _candles()),
        timeframes=["5"], clock=ClockSkew(offset_ms=0, max_offset_ms=500),
        local_now_ms=1_700_000_000_500,
    )
    assert data.data_age_ms == 500


@pytest.mark.asyncio
async def test_data_age_applies_clock_offset() -> None:
    """Offset do servidor entra no cálculo do frescor."""
    data = await fetch_coherent(
        _FakeMarket(_ob(ts=1_700_000_000_000), _ticker(), _candles()),
        timeframes=["5"], clock=ClockSkew(offset_ms=100, max_offset_ms=500),
        local_now_ms=1_700_000_000_400,
    )
    # corrected_now = 1_700_000_000_500; age = 500
    assert data.data_age_ms == 500


@pytest.mark.asyncio
async def test_multiple_timeframes_fetched() -> None:
    market = _FakeMarket(_ob(), _ticker(), _candles())
    data = await fetch_coherent(
        market, timeframes=["60", "15", "5"], clock=ClockSkew(0, 500),
        local_now_ms=1_700_000_010_000,
    )
    assert set(data.candles_by_tf) == {"60", "15", "5"}


# --------------------------------------------------------------------------
# Validação
# --------------------------------------------------------------------------


def _data(ob: OrderBook, ticker: Ticker, age: int = 200) -> CoherentMarketData:
    return CoherentMarketData(
        candles_by_tf={"5": _candles()}, orderbook=ob, ticker=ticker,
        corrected_now_ms=ob.ts_ms + age, clock_healthy=True,
    )


def _codes(data: CoherentMarketData) -> set[str]:
    return {issue.code for issue in validate_market_data(data, max_data_age_ms=5000)}


def test_healthy_data_has_no_issues() -> None:
    assert validate_market_data(_data(_ob(), _ticker()), max_data_age_ms=5000) == []


def test_crossed_book_is_flagged() -> None:
    """⭐ best_bid >= best_ask é integridade quebrada."""
    crossed = _ob(bid="60052", ask="60051")
    assert "BOOK_CROSSED" in _codes(_data(crossed, _ticker()))


def test_last_price_below_bid_is_flagged() -> None:
    """⭐ O caso que o Claude achou: last abaixo do best_bid."""
    assert "LAST_OUTSIDE_BOOK" in _codes(_data(_ob(bid="60050"), _ticker(last="60040")))


def test_last_price_above_ask_is_flagged() -> None:
    assert "LAST_OUTSIDE_BOOK" in _codes(_data(_ob(ask="60051"), _ticker(last="60060")))


def test_last_within_book_is_ok() -> None:
    assert "LAST_OUTSIDE_BOOK" not in _codes(_data(_ob(), _ticker(last="60050")))


def test_stale_data_is_flagged() -> None:
    data = _data(_ob(), _ticker(), age=6000)
    assert "DATA_STALE" in _codes(data)


def test_negative_age_is_flagged_not_silent() -> None:
    """⭐ Frescor negativo vira issue explícita, nunca passa como VALID."""
    data = _data(_ob(), _ticker(), age=-100)
    assert "NEGATIVE_DATA_AGE" in _codes(data)


def test_unhealthy_clock_is_flagged() -> None:
    data = CoherentMarketData(
        candles_by_tf={"5": _candles()}, orderbook=_ob(), ticker=_ticker(),
        corrected_now_ms=_ob().ts_ms + 200, clock_healthy=False,
    )
    assert "CLOCK_SKEW" in _codes(data)


def test_non_positive_price_is_flagged() -> None:
    """Book com preço zero (dado corrompido) é sinalizado."""
    zero_book = OrderBook(
        symbol="BTCUSDT",
        bids=(BookLevel(Decimal("0"), Decimal("1")),),
        asks=(BookLevel(Decimal("60051"), Decimal("1")),),
        ts_ms=1_700_000_000_000, update_id=1,
    )
    assert "NON_POSITIVE_PRICE" in _codes(_data(zero_book, _ticker()))


def test_validation_issues_are_machine_readable() -> None:
    issues = validate_market_data(_data(_ob(bid="60052", ask="60051"), _ticker()),
                                  max_data_age_ms=5000)
    for issue in issues:
        assert issue.code.isupper()
        assert issue.detail
