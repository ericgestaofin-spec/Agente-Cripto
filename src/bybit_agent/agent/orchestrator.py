"""Orquestrador shadow — um ciclo de decisão ponta a ponta.

Amarra as camadas: market data (Bybit) → snapshot → Claude → parser →
Risk Engine. Em SHADOW MODE nenhuma ordem é enviada; o resultado carrega
a decisão e o veredito de risco para registro e análise.

É a Fase 3 do plano: o agente gera decisões, mas nenhuma ordem toca a
corretora. É o caminho seguro para validar o pipeline inteiro com dados
reais antes de qualquer execução.

Não há caminho de execução aqui — a ausência de `send_order` é uma
garantia estrutural testada.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from bybit_agent.agent.client import AgentResult
from bybit_agent.agent.parser import parse_decision
from bybit_agent.features.snapshot import build_snapshot
from bybit_agent.risk.engine import RiskDecision, TradeIntent, evaluate
from bybit_agent.risk.policy import RiskPolicy
from bybit_agent.risk.validators import AccountState

# Taxa taker da Bybit (conservadora para sizing). Slippage estimado do book
# na v0; a curva de impacto real vem depois (achado 7 do review).
_TAKER_FEE = Decimal("0.00055")
_PRIMARY_TF = "5"


class _Market(Protocol):
    async def instrument(self, symbol: str = ...) -> Any: ...
    async def klines(self, symbol: str = ..., interval: str = ..., limit: int = ...) -> Any: ...
    async def orderbook(self, symbol: str = ..., depth: int = ...) -> Any: ...
    async def ticker(self, symbol: str = ...) -> Any: ...


class _Agent(Protocol):
    def analyze(self, snapshot: dict[str, Any]) -> AgentResult: ...


@dataclass(frozen=True, slots=True)
class ShadowCycleResult:
    snapshot: dict[str, Any]
    action: str
    intent: TradeIntent | None
    risk_decision: RiskDecision | None
    agent_result: AgentResult


class ShadowOrchestrator:
    """Executa ciclos de decisão em shadow mode (sem execução)."""

    def __init__(
        self,
        *,
        market: _Market,
        agent: _Agent,
        policy: RiskPolicy,
        account_equity: Decimal,
        symbol: str = "BTCUSDT",
    ) -> None:
        self._market = market
        self._agent = agent
        self._policy = policy
        self._equity = account_equity
        self._symbol = symbol

    async def run_cycle(self, *, now_ms: int | None = None) -> ShadowCycleResult:
        # 1. Coleta dados reais da Bybit CONCORRENTEMENTE — book e ticker
        #    precisam ser o mais próximos possível no tempo, senão o mercado
        #    se move entre eles e o snapshot fica incoerente (last abaixo do
        #    best_bid, etc.). O `now` é capturado DEPOIS da coleta, senão a
        #    latência das chamadas faz os dados parecerem datados no futuro
        #    (data_age_ms negativo). Ambos os bugs foram flagrados pelo
        #    próprio Claude num ciclo ao vivo.
        spec, candles, ob, ticker = await asyncio.gather(
            self._market.instrument(self._symbol),
            self._market.klines(self._symbol, interval=_PRIMARY_TF, limit=200),
            self._market.orderbook(self._symbol, depth=50),
            self._market.ticker(self._symbol),
        )
        now = now_ms if now_ms is not None else int(time.time() * 1000)

        # 2. Monta o snapshot estruturado.
        snapshot = build_snapshot(
            symbol=self._symbol,
            candles_by_tf={_PRIMARY_TF: candles},
            orderbook=ob,
            ticker=ticker,
            now_ms=now,
            data_ts_ms=ob.ts_ms,
        )

        # 3. Claude analisa.
        agent_result = self._agent.analyze(snapshot)

        # 4. Parseia a decisão em intenção de domínio.
        parsed = parse_decision(
            agent_result.decision, now_ms=now, max_leverage=self._policy.max_leverage
        )

        # 5. Se há intenção de abertura, o Risk Engine decide (shadow — não executa).
        risk_decision: RiskDecision | None = None
        if parsed.intent is not None:
            spread_bps = ob.spread_bps()
            account = AccountState(
                equity=self._equity,
                daily_pnl=Decimal("0"),
                weekly_pnl=Decimal("0"),
                open_positions=0,
                open_orders=0,
                consecutive_losses=0,
                entries_today=0,
                data_age_ms=snapshot["data_age_ms"],
                spread_bps=spread_bps,
                estimated_slippage_bps=spread_bps,  # proxy v0
                has_conflicting_position=False,
                has_conflicting_order=False,
            )
            # slippage monetário estimado do spread (proxy v0).
            est_slippage = ob.mid() * spread_bps / Decimal("10000")
            risk_decision = evaluate(
                parsed.intent,
                account,
                spec=spec,
                policy=self._policy,
                taker_fee_rate=_TAKER_FEE,
                estimated_slippage=est_slippage,
                available_liquidity=_estimate_liquidity(ob),
                now_ms=now,
            )

        return ShadowCycleResult(
            snapshot=snapshot,
            action=parsed.action,
            intent=parsed.intent,
            risk_decision=risk_decision,
            agent_result=agent_result,
        )


def _estimate_liquidity(ob: Any) -> Decimal:
    """Liquidez disponível ~ soma dos tamanhos do lado relevante do book."""
    return sum((lvl.size for lvl in ob.asks), Decimal("0"))
