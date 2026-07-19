"""Montagem do snapshot de mercado — a entrada estruturada do Claude.

Converte dados brutos num objeto compacto e objetivo. Indicadores já
calculados por código (não desperdiçar tokens pedindo ao modelo). Preços
como string decimal; campo indisponível como `None`, nunca `0`.

Determinístico: `now_ms` e `data_ts_ms` são injetados de fora — sem
relógio interno.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from bybit_agent.features.indicators import atr, ema, realized_volatility
from bybit_agent.features.liquidity import summarize_liquidity
from bybit_agent.features.structure import analyze_structure
from bybit_agent.marketdata.rest import Candle, OrderBook, Ticker

# Timeframes e períodos de indicador. Simples e explícito para a v0.
_EMA_FAST = 9
_EMA_SLOW = 21
_ATR_PERIOD = 14
_MIN_CANDLES_FOR_REGIME = 25


def _classify_regime(candles: list[Candle]) -> str:
    """Regime de mercado a partir do cruzamento de EMAs e da inclinação.

    Conservador: UNKNOWN quando não há candles suficientes. Não inventa.
    """
    if len(candles) < _MIN_CANDLES_FOR_REGIME:
        return "UNKNOWN"
    closes = [c.close for c in candles]
    fast = ema(closes, period=_EMA_FAST)
    slow = ema(closes, period=_EMA_SLOW)
    if fast is None or slow is None:
        return "UNKNOWN"
    # inclinação recente da EMA rápida
    fast_prev = ema(closes[:-5], period=_EMA_FAST)
    if fast_prev is None:
        return "UNKNOWN"
    rising = fast > fast_prev
    if fast > slow and rising:
        return "TRENDING_UP"
    if fast < slow and not rising:
        return "TRENDING_DOWN"
    diff = abs(fast - slow) / slow if slow != 0 else Decimal("0")
    if diff < Decimal("0.001"):
        return "RANGE"
    return "TRANSITION"


def _s(value: Decimal | None) -> str | None:
    """Decimal → string decimal, ou None preservado."""
    return None if value is None else str(value)


def build_snapshot(
    *,
    symbol: str,
    candles_by_tf: dict[str, list[Candle]],
    orderbook: OrderBook,
    ticker: Ticker,
    now_ms: int,
    data_ts_ms: int,
) -> dict[str, Any]:
    """Monta o snapshot. Preços como string; indisponível como None."""
    primary_tf = next(iter(candles_by_tf))
    candles = candles_by_tf[primary_tf]
    closes = [c.close for c in candles]

    atr_val = atr(candles, period=_ATR_PERIOD)
    rv = realized_volatility(closes)
    ema_fast = ema(closes, period=_EMA_FAST)
    ema_slow = ema(closes, period=_EMA_SLOW)

    structure = analyze_structure(candles)
    liq = summarize_liquidity(orderbook)

    # Alinhamento multi-timeframe: regime e tendência estrutural por TF. É o
    # contexto que separa um pullback numa tendência maior de uma reversão.
    multi_tf = {
        tf: {
            "regime": _classify_regime(cs),
            "trend": analyze_structure(cs).trend,
        }
        for tf, cs in candles_by_tf.items()
    }

    return {
        "symbol": symbol,
        "timestamp": data_ts_ms,
        "data_age_ms": now_ms - data_ts_ms,
        "market_regime": _classify_regime(candles),
        "trend": {
            "ema_fast": _s(ema_fast),
            "ema_slow": _s(ema_slow),
            "primary_timeframe": primary_tf,
        },
        "structure": {
            "trend": structure.trend,
            "last_swing_high": _s(structure.last_swing_high),
            "last_swing_low": _s(structure.last_swing_low),
            "bos": structure.bos,
            "choch": structure.choch,
        },
        "multi_timeframe": multi_tf,
        "price": {
            "last": str(ticker.last_price),
            "mark": str(ticker.mark_price),
            "index": str(ticker.index_price),
        },
        "volatility": {
            "atr": _s(atr_val),
            "realized_volatility": _s(rv),
        },
        "liquidity": {
            "spread_bps": str(orderbook.spread_bps()),
            "best_bid": str(orderbook.best_bid().price),
            "best_ask": str(orderbook.best_ask().price),
            "book_levels": len(orderbook.bids),
            "imbalance": str(liq.imbalance),
            "bid_depth": str(liq.bid_depth),
            "ask_depth": str(liq.ask_depth),
        },
        "derivatives": {
            "funding_rate": str(ticker.funding_rate),
            "open_interest": str(ticker.open_interest),
        },
    }
