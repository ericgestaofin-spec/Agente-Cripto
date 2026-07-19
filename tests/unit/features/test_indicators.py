"""Sprint 3 — indicadores puros.

Calculados por CÓDIGO, não pelo modelo (a spec é explícita: não gastar
tokens pedindo ao Claude para recalcular indicadores de centenas de
candles). Valores de referência calculados à mão.

Regra crítica: dados insuficientes → None, nunca um número improvisado.
`0` e "não sei" são coisas diferentes.
"""

from __future__ import annotations

from decimal import Decimal

from bybit_agent.features.indicators import atr, ema, realized_volatility
from bybit_agent.marketdata.rest import Candle


def _candle(h: str, low: str, c: str, start: int = 0) -> Candle:
    return Candle(
        start_ms=start, open=Decimal(c), high=Decimal(h), low=Decimal(low),
        close=Decimal(c), volume=Decimal("1"), turnover=Decimal("1"),
    )


# --------------------------------------------------------------------------
# ATR
# --------------------------------------------------------------------------


def test_atr_reference_value() -> None:
    """3 candles, period=2.
    C0: H110 L90 C100. C1: H105 L95 C102 → TR=max(10,5,5)=10.
    C2: H108 L98 C106 → TR=max(10,6,4)=10. ATR=(10+10)/2=10."""
    candles = [
        _candle("110", "90", "100", 0),
        _candle("105", "95", "102", 1),
        _candle("108", "98", "106", 2),
    ]
    assert atr(candles, period=2) == Decimal("10")


def test_atr_insufficient_candles_returns_none() -> None:
    """⭐ Menos candles que o necessário → None, nunca improvisa."""
    assert atr([_candle("110", "90", "100")], period=2) is None


def test_atr_is_decimal() -> None:
    candles = [_candle("110", "90", "100", i) for i in range(5)]
    result = atr(candles, period=3)
    assert isinstance(result, Decimal)


def test_atr_true_range_uses_previous_close() -> None:
    """TR considera o gap contra o fechamento anterior."""
    # C0 C=100. C1: H=100 L=99 mas gap: prevClose=100 → TR=max(1, 0, 1)=1
    candles = [
        _candle("100", "100", "100", 0),
        _candle("100", "99", "99", 1),
    ]
    # TR1 = max(100-99=1, |100-100|=0, |99-100|=1) = 1
    assert atr(candles, period=1) == Decimal("1")


# --------------------------------------------------------------------------
# EMA
# --------------------------------------------------------------------------


def test_ema_reference_value() -> None:
    """N=3 → k=2/4=0.5 (limpo). seed=primeiro preço.
    prices=[100,102,106]. EMA0=100, EMA1=101, EMA2=103.5."""
    prices = [Decimal("100"), Decimal("102"), Decimal("106")]
    assert ema(prices, period=3) == Decimal("103.5")


def test_ema_insufficient_returns_none() -> None:
    assert ema([Decimal("100")], period=3) is None


def test_ema_responds_to_price_increase() -> None:
    """EMA de série crescente é crescente."""
    rising = [Decimal(str(100 + i)) for i in range(10)]
    falling = [Decimal(str(100 - i)) for i in range(10)]
    assert ema(rising, period=5) > Decimal("100")
    assert ema(falling, period=5) < Decimal("100")


def test_ema_is_decimal() -> None:
    prices = [Decimal("100"), Decimal("102"), Decimal("106")]
    assert isinstance(ema(prices, period=3), Decimal)


# --------------------------------------------------------------------------
# Volatilidade realizada
# --------------------------------------------------------------------------


def test_realized_volatility_of_flat_series_is_zero() -> None:
    """Série de preços constante tem volatilidade zero."""
    flat = [Decimal("100")] * 10
    assert realized_volatility(flat) == Decimal("0")


def test_realized_volatility_is_positive_for_moving_series() -> None:
    moving = [Decimal(str(100 + (i % 3))) for i in range(20)]
    rv = realized_volatility(moving)
    assert rv is not None
    assert rv > 0


def test_realized_volatility_insufficient_returns_none() -> None:
    assert realized_volatility([Decimal("100")]) is None
