"""Loop shadow resiliente — a cadência viva do analista.

Amarra os três controles de custo e a resiliência ao redor do orquestrador:

  - CADÊNCIA: `CandleScheduler` dispara no máximo um ciclo por candle
    fechado. Nada de decidir a cada tick.
  - TETO: `DailyBudget` corta a análise (a chamada ao Claude) quando o gasto
    do dia estoura — o ciclo ainda roda e é registrado como pulado.
  - RESILIÊNCIA: um erro num ciclo (rede, parsing, o que for) é contado e
    engolido — o loop NUNCA morre por causa de um ciclo ruim.

`tick` é puro e determinístico (`now_ms` e `day_epoch` injetados). O
`run_forever` (relógio real + sleep) é I/O, marcado `pragma: no cover`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

from bybit_agent.agent.orchestrator import ShadowCycleResult, ShadowOrchestrator
from bybit_agent.marketdata.scheduler import CandleScheduler, DailyBudget
from bybit_agent.persistence.decision_log import cost_of

_DAY_MS = 86_400_000


@dataclass(slots=True)
class LoopStats:
    cycles: int = 0
    analyzed: int = 0
    skipped: int = 0
    errors: int = 0
    total_cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))


class ShadowLoop:
    """Orquestra ciclos ao longo do tempo, com cadência, teto e resiliência."""

    def __init__(
        self,
        *,
        orchestrator: ShadowOrchestrator,
        scheduler: CandleScheduler,
        budget: DailyBudget,
    ) -> None:
        self._orch = orchestrator
        self._scheduler = scheduler
        self._budget = budget
        self._stats = LoopStats()

    @property
    def stats(self) -> LoopStats:
        return self._stats

    async def tick(
        self, *, now_ms: int, day_epoch: int
    ) -> ShadowCycleResult | None:
        """Um passo do loop. Retorna o resultado do ciclo, ou None se a
        cadência não disparou neste instante.

        Determinístico: nada de relógio interno. Nunca levanta — um ciclo com
        erro é contabilizado e o loop segue.
        """
        if not self._scheduler.should_decide(now_ms=now_ms):
            return None

        self._stats.cycles += 1
        allow = self._budget.can_afford(day_epoch=day_epoch)

        try:
            result = await self._orch.run_cycle(now_ms=now_ms, allow_analysis=allow)
        except Exception:  # noqa: BLE001 - resiliência: nenhum ciclo derruba o loop
            self._stats.errors += 1
            return None

        if result.analyzed:
            self._stats.analyzed += 1
            cost = cost_of(result)
            self._budget.record_spend(cost, day_epoch=day_epoch)
            self._stats.total_cost_usd += cost
        else:
            self._stats.skipped += 1
        return result

    async def run_forever(  # pragma: no cover - I/O: relógio real + sleep
        self,
        *,
        poll_interval_s: float = 5.0,
        on_cycle: Callable[[ShadowCycleResult], Awaitable[None]] | None = None,
        max_ticks: int | None = None,
    ) -> None:
        """Roda o loop com o relógio real. `on_cycle` recebe cada ciclo que
        de fato decidiu; `max_ticks` bound para demo/encerramento gracioso.

        O `tick` já é resiliente — este método só provê tempo real e ritmo.
        """
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            now_ms = time.time_ns() // 1_000_000
            result = await self.tick(now_ms=now_ms, day_epoch=now_ms // _DAY_MS)
            if result is not None and on_cycle is not None:
                await on_cycle(result)
            ticks += 1
            await asyncio.sleep(poll_interval_s)
