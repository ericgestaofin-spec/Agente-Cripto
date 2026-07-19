"""A1 — scheduler alinhado ao candle e controle de orçamento de custo.

O scheduler é o lever de CADÊNCIA: decidir no fechamento de candle do
timeframe escolhido (1h = 24 decisões/dia, 5m = 288). Puro e determinístico
(o relógio é injetado).

O DailyBudget é o TETO de custo: para de autorizar chamadas ao Claude ao
atingir o limite diário. Sem surpresa na fatura.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bybit_agent.marketdata.scheduler import (
    CandleScheduler,
    DailyBudget,
    interval_to_ms,
    next_candle_close_ms,
)

# --------------------------------------------------------------------------
# Conversão de intervalo
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tf", "expected"),
    [("1", 60_000), ("5", 300_000), ("15", 900_000), ("60", 3_600_000),
     ("240", 14_400_000), ("D", 86_400_000)],
)
def test_interval_to_ms(tf: str, expected: int) -> None:
    assert interval_to_ms(tf) == expected


def test_unknown_interval_raises() -> None:
    with pytest.raises(ValueError):
        interval_to_ms("7")


# --------------------------------------------------------------------------
# Próximo fechamento de candle (alinhado ao relógio de parede)
# --------------------------------------------------------------------------


def test_next_close_of_5m_candle() -> None:
    """Candles de 5m fecham em múltiplos de 300_000ms desde o epoch.
    Às 12:02 (dentro do candle 12:00-12:05), o próximo fecho é 12:05."""
    # 1_700_000_100_000 está dentro de um candle de 5m; o próximo múltiplo:
    now = 1_700_000_100_000
    close = next_candle_close_ms(now, interval_ms=300_000)
    assert close > now
    assert close % 300_000 == 0
    assert close - now <= 300_000


def test_next_close_at_exact_boundary_is_next_candle() -> None:
    """Exatamente no fecho → o próximo é o candle seguinte, não o atual."""
    boundary = 1_700_000_100_000 - (1_700_000_100_000 % 300_000)
    assert next_candle_close_ms(boundary, interval_ms=300_000) == boundary + 300_000


# --------------------------------------------------------------------------
# CandleScheduler — dispara uma vez por candle fechado
# --------------------------------------------------------------------------


def test_scheduler_triggers_after_close_plus_buffer() -> None:
    sched = CandleScheduler(interval="5", buffer_ms=2000)
    # candle fecha em C; antes de C+buffer não dispara
    close = next_candle_close_ms(1_700_000_100_000, interval_ms=300_000)
    assert not sched.should_decide(now_ms=close + 1000)  # dentro do buffer
    assert sched.should_decide(now_ms=close + 2000)  # após o buffer


def test_scheduler_fires_once_per_candle() -> None:
    """⭐ Não dispara duas vezes no mesmo candle — evita chamada (e custo)
    duplicada."""
    sched = CandleScheduler(interval="5", buffer_ms=1000)
    close = next_candle_close_ms(1_700_000_100_000, interval_ms=300_000)
    assert sched.should_decide(now_ms=close + 1000)
    assert not sched.should_decide(now_ms=close + 2000)  # mesmo candle, já disparou
    # próximo candle
    assert sched.should_decide(now_ms=close + 300_000 + 1000)


def test_scheduler_is_deterministic() -> None:
    a = CandleScheduler(interval="15", buffer_ms=1000)
    b = CandleScheduler(interval="15", buffer_ms=1000)
    close = next_candle_close_ms(1_700_000_100_000, interval_ms=900_000)
    assert a.should_decide(now_ms=close + 1000) == b.should_decide(now_ms=close + 1000)


# --------------------------------------------------------------------------
# DailyBudget — teto de custo
# --------------------------------------------------------------------------


def test_budget_allows_calls_under_limit() -> None:
    budget = DailyBudget(max_usd=Decimal("5.00"))
    assert budget.can_afford()


def test_budget_stops_at_limit() -> None:
    """⭐ Ao atingir o teto diário, não autoriza mais chamadas."""
    budget = DailyBudget(max_usd=Decimal("0.10"))
    budget.record_spend(Decimal("0.06"))
    assert budget.can_afford()
    budget.record_spend(Decimal("0.06"))  # total 0.12 > 0.10
    assert not budget.can_afford()


def test_budget_resets_on_new_day() -> None:
    budget = DailyBudget(max_usd=Decimal("0.10"))
    budget.record_spend(Decimal("0.20"), day_epoch=100)
    assert not budget.can_afford(day_epoch=100)
    assert budget.can_afford(day_epoch=101)  # novo dia, orçamento renovado


def test_budget_tracks_total_spend() -> None:
    budget = DailyBudget(max_usd=Decimal("5.00"))
    budget.record_spend(Decimal("0.05"))
    budget.record_spend(Decimal("0.07"))
    assert budget.spent_today() == Decimal("0.12")


def test_budget_estimate_from_usage() -> None:
    """Custo estimado dos tokens do Opus 4.8: $5/M input, $25/M output,
    cache write 1.25x, cache read 0.1x."""
    from bybit_agent.marketdata.scheduler import estimate_cost_usd

    cost = estimate_cost_usd(
        input_tokens=300, output_tokens=1000,
        cache_creation_input_tokens=4000, cache_read_input_tokens=0,
    )
    # 300*5e-6 + 1000*25e-6 + 4000*6.25e-6 + 0 = 0.0015+0.025+0.025 = 0.0515
    assert cost == Decimal("0.0515")
