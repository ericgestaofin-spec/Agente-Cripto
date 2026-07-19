"""Indicadores técnicos — puros, em Decimal, calculados por código.

A spec é explícita: indicadores são calculados por CÓDIGO, não pedindo ao
modelo para recalcular a partir de centenas de candles. Isso economiza
tokens e, mais importante, torna o cálculo determinístico e testável.

Invariante: dados insuficientes retornam `None`, nunca um número
improvisado. O modelo trata `0` como um valor real; um indicador que não
pode ser calculado precisa dizer isso explicitamente.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from bybit_agent.domain.money import decimal_context
from bybit_agent.marketdata.rest import Candle


def atr(candles: list[Candle], *, period: int) -> Decimal | None:
    """Average True Range — média simples do True Range sobre `period`.

    TR = max(high−low, |high−prevClose|, |low−prevClose|). Precisa de
    `period + 1` candles (o TR do primeiro exige um fechamento anterior).
    """
    if period < 1 or len(candles) < period + 1:
        return None
    with localcontext(decimal_context()):
        trs: list[Decimal] = []
        for prev, cur in zip(candles[-(period + 1):-1], candles[-period:], strict=True):
            tr = max(
                cur.high - cur.low,
                abs(cur.high - prev.close),
                abs(cur.low - prev.close),
            )
            trs.append(tr)
        return sum(trs, Decimal("0")) / Decimal(period)


def ema(values: list[Decimal], *, period: int) -> Decimal | None:
    """Exponential Moving Average. Semeada com o primeiro valor.

    k = 2/(period+1). Precisa de pelo menos `period` valores.
    """
    if period < 1 or len(values) < period:
        return None
    with localcontext(decimal_context()):
        k = Decimal(2) / Decimal(period + 1)
        ema_val = values[0]
        for price in values[1:]:
            ema_val = price * k + ema_val * (1 - k)
        return ema_val


def realized_volatility(prices: list[Decimal]) -> Decimal | None:
    """Desvio-padrão dos retornos simples. Precisa de >= 2 preços.

    Retornos simples (não-log) para manter tudo em Decimal exato — log
    exigiria float. Suficiente como proxy de volatilidade para o snapshot.
    """
    if len(prices) < 2:
        return None
    with localcontext(decimal_context()):
        returns: list[Decimal] = []
        for prev, cur in zip(prices[:-1], prices[1:], strict=True):
            if prev == 0:
                continue
            returns.append((cur - prev) / prev)
        if not returns:
            return None
        n = Decimal(len(returns))
        mean = sum(returns, Decimal("0")) / n
        variance = sum(((r - mean) ** 2 for r in returns), Decimal("0")) / n
        return variance.sqrt()
