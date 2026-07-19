"""A4 — loop shadow resiliente: cadência, teto de orçamento e resiliência.

`tick` é determinístico (now_ms/day_epoch injetados). Fakes no lugar do
orquestrador para isolar a lógica do loop.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bybit_agent.agent.orchestrator import ShadowCycleResult
from bybit_agent.marketdata.scheduler import CandleScheduler, DailyBudget
from bybit_agent.runtime.loop import ShadowLoop

_TF_MS = 300_000  # candle de 5m


def _analyzed(cost_tokens: int = 1000) -> ShadowCycleResult:
    from bybit_agent.agent.client import AgentResult

    ar = AgentResult(
        decision={"decision_id": "d", "action": "NO_TRADE"},
        system_prompt_version="v",
        usage={"input_tokens": cost_tokens, "output_tokens": 0,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    )
    return ShadowCycleResult(
        snapshot={"symbol": "BTCUSDT"}, action="NO_TRADE", intent=None,
        risk_decision=None, agent_result=ar, analyzed=True, skip_reason=None,
    )


def _skipped() -> ShadowCycleResult:
    return ShadowCycleResult(
        snapshot={"symbol": "BTCUSDT"}, action="NO_TRADE", intent=None,
        risk_decision=None, agent_result=None, analyzed=False,
        skip_reason="orçamento diário esgotado",
    )


class _FakeOrch:
    """Registra allow_analysis e devolve um resultado configurável."""

    def __init__(self, result: ShadowCycleResult | None = None,
                 raises: bool = False) -> None:
        self._result = result or _analyzed()
        self._raises = raises
        self.calls: list[bool] = []

    async def run_cycle(self, *, now_ms: int, allow_analysis: bool = True) -> ShadowCycleResult:
        self.calls.append(allow_analysis)
        if self._raises:
            raise RuntimeError("falha de rede simulada")
        # respeita o veto de orçamento como o orquestrador real faz
        return self._result if allow_analysis else _skipped()


def _loop(orch, *, max_usd: str = "10", buffer_ms: int = 0) -> ShadowLoop:
    return ShadowLoop(
        orchestrator=orch,
        scheduler=CandleScheduler(interval="5", buffer_ms=buffer_ms),
        budget=DailyBudget(max_usd=Decimal(max_usd)),
    )


# --------------------------------------------------------------------------
# Cadência
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_returns_none_when_cadence_not_due() -> None:
    """⭐ Duas vezes no mesmo candle: só a primeira decide."""
    orch = _FakeOrch()
    loop = _loop(orch)
    r1 = await loop.tick(now_ms=10 * _TF_MS, day_epoch=1)
    r2 = await loop.tick(now_ms=10 * _TF_MS + 100, day_epoch=1)  # mesmo candle
    assert r1 is not None
    assert r2 is None
    assert loop.stats.cycles == 1


@pytest.mark.asyncio
async def test_new_candle_triggers_new_cycle() -> None:
    orch = _FakeOrch()
    loop = _loop(orch)
    await loop.tick(now_ms=10 * _TF_MS, day_epoch=1)
    r2 = await loop.tick(now_ms=11 * _TF_MS, day_epoch=1)  # candle seguinte
    assert r2 is not None
    assert loop.stats.cycles == 2


# --------------------------------------------------------------------------
# Teto de orçamento
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_exhausted_vetoes_analysis() -> None:
    """⭐ Estourado o teto, o loop passa allow_analysis=False e conta skip."""
    orch = _FakeOrch()
    loop = _loop(orch, max_usd="0")  # nenhum orçamento
    result = await loop.tick(now_ms=10 * _TF_MS, day_epoch=1)
    assert orch.calls == [False]  # análise vetada
    assert result is not None and result.analyzed is False
    assert loop.stats.skipped == 1
    assert loop.stats.analyzed == 0


@pytest.mark.asyncio
async def test_cost_is_charged_to_budget() -> None:
    """Ciclo analisado debita o custo; o próximo pode ser vetado."""
    orch = _FakeOrch(_analyzed(cost_tokens=1_000_000))  # ~5 USD (5e-6/token)
    loop = _loop(orch, max_usd="6")
    await loop.tick(now_ms=10 * _TF_MS, day_epoch=1)
    assert loop.stats.total_cost_usd == Decimal("5")
    # segundo candle: gasto 5 < 6 ainda cabe
    await loop.tick(now_ms=11 * _TF_MS, day_epoch=1)
    # agora 10 > 6 → terceiro é vetado
    r3 = await loop.tick(now_ms=12 * _TF_MS, day_epoch=1)
    assert r3 is not None and r3.analyzed is False


@pytest.mark.asyncio
async def test_budget_resets_next_day() -> None:
    orch = _FakeOrch(_analyzed(cost_tokens=1_000_000))
    loop = _loop(orch, max_usd="6")
    await loop.tick(now_ms=10 * _TF_MS, day_epoch=1)
    await loop.tick(now_ms=11 * _TF_MS, day_epoch=1)  # gasto 10 > 6
    r_veto = await loop.tick(now_ms=12 * _TF_MS, day_epoch=1)
    assert r_veto.analyzed is False
    # novo dia → orçamento renova
    r_new_day = await loop.tick(now_ms=13 * _TF_MS, day_epoch=2)
    assert r_new_day.analyzed is True


# --------------------------------------------------------------------------
# Resiliência
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cycle_error_is_counted_and_swallowed() -> None:
    """⭐ Um ciclo que explode não derruba o loop — conta erro e segue."""
    orch = _FakeOrch(raises=True)
    loop = _loop(orch)
    result = await loop.tick(now_ms=10 * _TF_MS, day_epoch=1)
    assert result is None
    assert loop.stats.errors == 1
    assert loop.stats.cycles == 1
    # o loop continua vivo para o próximo candle
    orch2_result = await loop.tick(now_ms=11 * _TF_MS, day_epoch=1)
    assert orch2_result is None  # ainda erra, mas não levanta
    assert loop.stats.errors == 2
