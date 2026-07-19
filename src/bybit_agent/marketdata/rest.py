"""Cliente REST de market data público da Bybit V5.

Endpoints de mercado são públicos — sem credencial. Este módulo tem as
funções de parsing (puras, testáveis com respostas gravadas) e um cliente
assíncrono fino que as usa.

Correções travadas por teste (docs/BYBIT_INTEGRACAO.md §4):
  - kline REST vem em ordem REVERSA → invertemos para cronológica
  - todo preço é Decimal, nunca float
  - retCode != 0 levanta, nunca é ignorado
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import httpx

from bybit_agent.domain.instrument import InstrumentSpec

MAINNET: Final[str] = "https://api.bybit.com"
TESTNET: Final[str] = "https://api-testnet.bybit.com"
CATEGORY: Final[str] = "linear"


def _check_ret_code(payload: dict[str, Any]) -> dict[str, Any]:
    code = payload.get("retCode")
    if code != 0:
        raise ValueError(
            f"Bybit retCode {code}: {payload.get('retMsg', 'sem mensagem')}"
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("resposta da Bybit sem bloco 'result'")
    return result


# --------------------------------------------------------------------------
# Tipos de domínio
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candle:
    start_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class OrderBook:
    symbol: str
    bids: tuple[BookLevel, ...]  # ordem decrescente de preço
    asks: tuple[BookLevel, ...]  # ordem crescente de preço
    ts_ms: int
    update_id: int

    def best_bid(self) -> BookLevel:
        return self.bids[0]

    def best_ask(self) -> BookLevel:
        return self.asks[0]

    def mid(self) -> Decimal:
        return (self.best_bid().price + self.best_ask().price) / 2

    def spread_bps(self) -> Decimal:
        spread = self.best_ask().price - self.best_bid().price
        return spread / self.mid() * Decimal("10000")


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: str
    last_price: Decimal
    mark_price: Decimal
    index_price: Decimal
    bid1_price: Decimal
    ask1_price: Decimal
    funding_rate: Decimal
    open_interest: Decimal


# --------------------------------------------------------------------------
# Parsing (puro)
# --------------------------------------------------------------------------


def parse_klines(payload: dict[str, Any]) -> list[Candle]:
    """Converte a resposta de /v5/market/kline em candles CRONOLÓGICOS.

    A Bybit devolve do mais recente para o mais antigo — invertemos.
    """
    result = _check_ret_code(payload)
    rows = result.get("list", [])
    candles = [
        Candle(
            start_ms=int(row[0]),
            open=Decimal(row[1]),
            high=Decimal(row[2]),
            low=Decimal(row[3]),
            close=Decimal(row[4]),
            volume=Decimal(row[5]),
            turnover=Decimal(row[6]),
        )
        for row in rows
    ]
    candles.sort(key=lambda c: c.start_ms)  # cronológico
    return candles


def parse_orderbook(payload: dict[str, Any]) -> OrderBook:
    result = _check_ret_code(payload)
    bids = tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in result["b"])
    asks = tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in result["a"])
    return OrderBook(
        symbol=str(result["s"]),
        bids=bids,
        asks=asks,
        ts_ms=int(result["ts"]),
        update_id=int(result["u"]),
    )


def parse_ticker(payload: dict[str, Any]) -> Ticker:
    result = _check_ret_code(payload)
    row = result["list"][0]
    return Ticker(
        symbol=str(row["symbol"]),
        last_price=Decimal(row["lastPrice"]),
        mark_price=Decimal(row["markPrice"]),
        index_price=Decimal(row["indexPrice"]),
        bid1_price=Decimal(row["bid1Price"]),
        ask1_price=Decimal(row["ask1Price"]),
        funding_rate=Decimal(row["fundingRate"]),
        open_interest=Decimal(row["openInterest"]),
    )


def parse_instrument(payload: dict[str, Any]) -> InstrumentSpec:
    result = _check_ret_code(payload)
    return InstrumentSpec.from_bybit(result["list"][0])


# --------------------------------------------------------------------------
# Cliente assíncrono
# --------------------------------------------------------------------------


class BybitPublicClient:
    """Cliente REST assíncrono para dados de mercado públicos. Sem auth."""

    def __init__(self, base_url: str = MAINNET, timeout: float = 15.0) -> None:  # noqa: no-float
        # timeout em segundos (rede), não é valor monetário
        self._base = base_url
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def __aenter__(self) -> BybitPublicClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return dict(resp.json())

    async def instrument(self, symbol: str = "BTCUSDT") -> InstrumentSpec:
        payload = await self._get(
            "/v5/market/instruments-info",
            {"category": CATEGORY, "symbol": symbol},
        )
        return parse_instrument(payload)

    async def klines(
        self, symbol: str = "BTCUSDT", interval: str = "5", limit: int = 200
    ) -> list[Candle]:
        payload = await self._get(
            "/v5/market/kline",
            {"category": CATEGORY, "symbol": symbol,
             "interval": interval, "limit": str(limit)},
        )
        return parse_klines(payload)

    async def orderbook(self, symbol: str = "BTCUSDT", depth: int = 50) -> OrderBook:
        payload = await self._get(
            "/v5/market/orderbook",
            {"category": CATEGORY, "symbol": symbol, "limit": str(depth)},
        )
        return parse_orderbook(payload)

    async def ticker(self, symbol: str = "BTCUSDT") -> Ticker:
        payload = await self._get(
            "/v5/market/tickers",
            {"category": CATEGORY, "symbol": symbol},
        )
        return parse_ticker(payload)
