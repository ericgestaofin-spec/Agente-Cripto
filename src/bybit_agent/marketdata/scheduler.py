"""Cadência de decisão e controle de orçamento de custo.

Dois levers de custo do plano do analista (docs/PLANO_ANALISTA.md §1.5):

  1. CADÊNCIA — o CandleScheduler dispara uma decisão por fechamento de
     candle do timeframe escolhido. 1h = 24/dia, 5m = 288/dia. Escolher o
     timeframe é o primeiro lever de custo.

  2. TETO — o DailyBudget para de autorizar chamadas ao atingir o limite
     diário em USD. Sem surpresa na fatura.

Ambos puros e determinísticos (relógio e gastos injetados).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

_INTERVAL_MS: Final[dict[str, int]] = {
    "1": 60_000,
    "3": 180_000,
    "5": 300_000,
    "15": 900_000,
    "30": 1_800_000,
    "60": 3_600_000,
    "120": 7_200_000,
    "240": 14_400_000,
    "360": 21_600_000,
    "720": 43_200_000,
    "D": 86_400_000,
}

# Preços do Opus 4.8 por token (USD). cache write 1.25x input, read 0.1x.
_PRICE_INPUT = Decimal("0.000005")
_PRICE_OUTPUT = Decimal("0.000025")
_PRICE_CACHE_WRITE = _PRICE_INPUT * Decimal("1.25")
_PRICE_CACHE_READ = _PRICE_INPUT * Decimal("0.1")


def interval_to_ms(tf: str) -> int:
    if tf not in _INTERVAL_MS:
        raise ValueError(f"intervalo desconhecido: {tf!r}")
    return _INTERVAL_MS[tf]


def next_candle_close_ms(now_ms: int, *, interval_ms: int) -> int:
    """Próximo fechamento de candle alinhado ao relógio de parede.

    Candles fecham em múltiplos do intervalo desde o epoch. Exatamente no
    limite, retorna o próximo candle (o atual acabou de fechar).
    """
    return (now_ms // interval_ms + 1) * interval_ms


class CandleScheduler:
    """Dispara uma decisão por candle fechado (+ buffer para a corretora
    finalizar o candle). Nunca duas vezes no mesmo candle."""

    def __init__(self, *, interval: str, buffer_ms: int = 2000) -> None:
        self._interval_ms = interval_to_ms(interval)
        self._buffer_ms = buffer_ms
        self._last_fired_close: int | None = None

    def should_decide(self, *, now_ms: int) -> bool:
        # O candle que já fechou é o múltiplo imediatamente <= now.
        last_close = (now_ms // self._interval_ms) * self._interval_ms
        if now_ms < last_close + self._buffer_ms:
            return False  # ainda no buffer pós-fechamento
        if self._last_fired_close == last_close:
            return False  # já disparou neste candle
        self._last_fired_close = last_close
        return True


class DailyBudget:
    """Teto diário de gasto com o Claude. Renova a cada dia."""

    def __init__(self, *, max_usd: Decimal) -> None:
        self._max = max_usd
        self._spent = Decimal("0")
        self._day: int | None = None

    def _roll_day(self, day_epoch: int | None) -> None:
        if day_epoch is not None and day_epoch != self._day:
            self._day = day_epoch
            self._spent = Decimal("0")

    def can_afford(self, *, day_epoch: int | None = None) -> bool:
        self._roll_day(day_epoch)
        return self._spent < self._max

    def record_spend(self, cost_usd: Decimal, *, day_epoch: int | None = None) -> None:
        self._roll_day(day_epoch)
        self._spent += cost_usd

    def spent_today(self) -> Decimal:
        return self._spent


def estimate_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
) -> Decimal:
    """Custo estimado de uma chamada, a partir do usage do Opus 4.8."""
    return (
        Decimal(input_tokens) * _PRICE_INPUT
        + Decimal(output_tokens) * _PRICE_OUTPUT
        + Decimal(cache_creation_input_tokens) * _PRICE_CACHE_WRITE
        + Decimal(cache_read_input_tokens) * _PRICE_CACHE_READ
    )
