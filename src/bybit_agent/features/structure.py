"""Estrutura de mercado — swings, tendência e quebras (BOS/CHoCH).

Dá ao Claude uma leitura ESTRUTURAL objetiva do preço, não só indicadores.
Puro e determinístico sobre a série de candles (cronológica).

Conceitos (price action clássico):
  - swing high/low: fractal — extremo local com `window` candles menores de
    cada lado. Só é "confirmado" quando há `window` candles depois dele.
  - tendência: HH+HL = alta; LH+LL = baixa; senão range.
  - BOS (Break of Structure): o fechamento mais recente rompe o último swing
    confirmado — continuação na direção do rompimento.
  - CHoCH (Change of Character): BOS contra a tendência vigente — primeiro
    sinal de possível reversão.

Invariante: dados insuficientes → tendência UNKNOWN e campos None. Nunca
inventa estrutura onde não há.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bybit_agent.marketdata.rest import Candle


@dataclass(frozen=True, slots=True)
class Swing:
    index: int
    price: Decimal
    kind: str  # "HIGH" | "LOW"


@dataclass(frozen=True, slots=True)
class MarketStructure:
    trend: str  # "UP" | "DOWN" | "RANGE" | "UNKNOWN"
    last_swing_high: Decimal | None
    last_swing_low: Decimal | None
    bos: str | None  # "BULLISH" | "BEARISH" | None
    choch: bool


def find_swings(candles: list[Candle], *, window: int = 2) -> list[Swing]:
    """Swings fractais confirmados. Um extremo em `i` precisa de `window`
    candles estritamente menores (highs) / maiores (lows) de cada lado.

    Os últimos `window` candles nunca são swings — ainda não confirmados.
    """
    swings: list[Swing] = []
    n = len(candles)
    for i in range(window, n - window):
        hi = candles[i].high
        left = range(i - window, i)
        right = range(i + 1, i + window + 1)
        if all(hi > candles[j].high for j in left) and all(
            hi > candles[j].high for j in right
        ):
            swings.append(Swing(i, hi, "HIGH"))
        lo = candles[i].low
        if all(lo < candles[j].low for j in left) and all(
            lo < candles[j].low for j in right
        ):
            swings.append(Swing(i, lo, "LOW"))
    return swings


def _trend(highs: list[Swing], lows: list[Swing]) -> str:
    if len(highs) < 2 or len(lows) < 2:
        return "RANGE"
    higher_high = highs[-1].price > highs[-2].price
    higher_low = lows[-1].price > lows[-2].price
    lower_high = highs[-1].price < highs[-2].price
    lower_low = lows[-1].price < lows[-2].price
    if higher_high and higher_low:
        return "UP"
    if lower_high and lower_low:
        return "DOWN"
    return "RANGE"


def analyze_structure(candles: list[Candle], *, window: int = 2) -> MarketStructure:
    """Leitura estrutural: tendência, últimos swings e quebra (BOS/CHoCH)."""
    if len(candles) < 2 * window + 1:
        return MarketStructure("UNKNOWN", None, None, None, False)

    swings = find_swings(candles, window=window)
    highs = [s for s in swings if s.kind == "HIGH"]
    lows = [s for s in swings if s.kind == "LOW"]
    last_high = highs[-1].price if highs else None
    last_low = lows[-1].price if lows else None
    trend = _trend(highs, lows)

    # BOS: o fechamento mais recente rompe o último swing confirmado.
    last_close = candles[-1].close
    bos: str | None = None
    if last_high is not None and last_close > last_high:
        bos = "BULLISH"
    elif last_low is not None and last_close < last_low:
        bos = "BEARISH"

    # CHoCH: rompimento contra a tendência vigente.
    choch = bos is not None and (
        (trend == "UP" and bos == "BEARISH") or (trend == "DOWN" and bos == "BULLISH")
    )
    return MarketStructure(trend, last_high, last_low, bos, choch)
