"""Coleta coerente e validação de dados de mercado.

Coleta todas as fontes CONCORRENTEMENTE (para ficarem o mais próximas
possível no tempo) e computa o frescor com o relógio corrigido pelo skew
do servidor. Depois valida a integridade — book cruzado, last fora do
book, dados velhos — produzindo issues legíveis por máquina que o agente e
o Risk Engine usam para decidir.

Fecha os dois bugs que o Claude flagrou ao vivo: `data_age` negativo e
`last` fora do book.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from bybit_agent.marketdata.clock import ClockSkew
from bybit_agent.marketdata.rest import Candle, OrderBook, Ticker


class _Market(Protocol):
    async def klines(self, symbol: str = ..., interval: str = ..., limit: int = ...) -> Any: ...
    async def orderbook(self, symbol: str = ..., depth: int = ...) -> Any: ...
    async def ticker(self, symbol: str = ...) -> Any: ...


@dataclass(frozen=True, slots=True)
class CoherentMarketData:
    candles_by_tf: dict[str, list[Candle]]
    orderbook: OrderBook
    ticker: Ticker
    corrected_now_ms: int
    clock_healthy: bool

    @property
    def data_age_ms(self) -> int:
        """Frescor: agora corrigido − timestamp do book (a fonte mais
        sensível ao tempo, com ts explícito do servidor)."""
        return self.corrected_now_ms - self.orderbook.ts_ms


@dataclass(frozen=True, slots=True)
class DataIssue:
    code: str
    detail: str


async def fetch_coherent(
    market: _Market,
    *,
    timeframes: list[str],
    clock: ClockSkew,
    local_now_ms: int,
    symbol: str = "BTCUSDT",
) -> CoherentMarketData:
    """Coleta book, ticker e candles de todos os timeframes concorrentemente.

    O `now` corrigido é derivado do relógio local + offset do servidor.
    Chamado APÓS a coleta pelo orquestrador — mas aqui recebemos o local_now
    injetado para determinismo nos testes.
    """
    ob_task = market.orderbook(symbol, depth=50)
    ticker_task = market.ticker(symbol)
    kline_tasks = [market.klines(symbol, interval=tf, limit=200) for tf in timeframes]

    results = await asyncio.gather(ob_task, ticker_task, *kline_tasks)
    ob: OrderBook = results[0]
    ticker: Ticker = results[1]
    candles_by_tf = {tf: results[2 + i] for i, tf in enumerate(timeframes)}

    return CoherentMarketData(
        candles_by_tf=candles_by_tf,
        orderbook=ob,
        ticker=ticker,
        corrected_now_ms=clock.corrected_now_ms(local_now_ms=local_now_ms),
        clock_healthy=clock.is_healthy(),
    )


def validate_market_data(
    data: CoherentMarketData, *, max_data_age_ms: int
) -> list[DataIssue]:
    """Valida integridade e frescor. Lista COMPLETA de issues (não para no
    primeiro), cada uma com código legível por máquina.
    """
    issues: list[DataIssue] = []
    ob = data.orderbook

    best_bid = ob.best_bid().price
    best_ask = ob.best_ask().price

    # Book cruzado — integridade quebrada.
    if best_bid >= best_ask:
        issues.append(DataIssue(
            "BOOK_CROSSED", f"best_bid {best_bid} >= best_ask {best_ask}"))

    # Last fora do book — o mercado se moveu entre fontes, ou dado corrompido.
    last = data.ticker.last_price
    if last < best_bid or last > best_ask:
        issues.append(DataIssue(
            "LAST_OUTSIDE_BOOK",
            f"last {last} fora do book [{best_bid}, {best_ask}]"))

    # Frescor.
    age = data.data_age_ms
    if age < 0:
        issues.append(DataIssue(
            "NEGATIVE_DATA_AGE", f"data_age {age}ms negativo — inconsistência de tempo"))
    elif age > max_data_age_ms:
        issues.append(DataIssue(
            "DATA_STALE", f"data_age {age}ms > máximo {max_data_age_ms}ms"))

    # Relógio.
    if not data.clock_healthy:
        issues.append(DataIssue("CLOCK_SKEW", "desvio de relógio além do limite"))

    # Preços não-positivos (sanidade).
    if best_bid <= 0 or best_ask <= 0 or last <= Decimal("0"):
        issues.append(DataIssue("NON_POSITIVE_PRICE", "preço zero ou negativo no snapshot"))

    return issues
