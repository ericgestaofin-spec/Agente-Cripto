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

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from bybit_agent.agent.client import AgentResult
from bybit_agent.agent.parser import parse_decision
from bybit_agent.agent.prefilter import PrefilterConfig, prefilter
from bybit_agent.features.snapshot import build_snapshot
from bybit_agent.features.structure import analyze_structure
from bybit_agent.marketdata.clock import ClockSkew
from bybit_agent.marketdata.coherent import (
    DataIssue,
    fetch_coherent,
    validate_market_data,
)
from bybit_agent.persistence.decision_log import DecisionLog, build_record
from bybit_agent.risk.engine import RiskDecision, TradeIntent, evaluate
from bybit_agent.risk.policy import RiskPolicy
from bybit_agent.risk.validators import AccountState

# Taxa taker da Bybit (conservadora para sizing). Slippage estimado do book
# na v0; a curva de impacto real vem depois (achado 7 do review).
_TAKER_FEE = Decimal("0.00055")
_PRIMARY_TF = "5"
# Primário primeiro (é o de decisão); os demais dão alinhamento multi-TF.
_TIMEFRAMES = [_PRIMARY_TF, "15", "60"]
_MAX_DATA_AGE_MS = 10_000

# Issues que indicam dados corrompidos (não só velhos) → CONFLICTING.
_INTEGRITY_CODES = {
    "BOOK_CROSSED", "LAST_OUTSIDE_BOOK", "NEGATIVE_DATA_AGE",
    "NON_POSITIVE_PRICE", "CLOCK_SKEW",
}


class _Market(Protocol):
    async def instrument(self, symbol: str = ...) -> Any: ...
    async def klines(self, symbol: str = ..., interval: str = ..., limit: int = ...) -> Any: ...
    async def orderbook(self, symbol: str = ..., depth: int = ...) -> Any: ...
    async def ticker(self, symbol: str = ...) -> Any: ...


def _data_quality(issues: list[DataIssue], age_ms: int) -> dict[str, Any]:
    codes = {i.code for i in issues}
    if codes & _INTEGRITY_CODES:
        status = "CONFLICTING"
    elif "DATA_STALE" in codes:
        status = "STALE"
    else:
        status = "VALID"
    return {
        "status": status,
        "snapshot_age_ms": age_ms,
        "issues": [i.detail for i in issues],
    }


class _Agent(Protocol):
    def analyze(self, snapshot: dict[str, Any]) -> AgentResult: ...


@dataclass(frozen=True, slots=True)
class ShadowCycleResult:
    snapshot: dict[str, Any]
    action: str
    intent: TradeIntent | None
    risk_decision: RiskDecision | None
    agent_result: AgentResult | None
    analyzed: bool = True
    skip_reason: str | None = None


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
        clock: ClockSkew | None = None,
        prefilter_config: PrefilterConfig | None = None,
        decision_log: DecisionLog | None = None,
    ) -> None:
        self._market = market
        self._agent = agent
        self._policy = policy
        self._equity = account_equity
        self._symbol = symbol
        # Skew injetado nos testes; medido ao vivo em produção.
        self._clock = clock
        self._prefilter_config = prefilter_config
        self._decision_log = decision_log

    async def _finalize(
        self, result: ShadowCycleResult, *, ts_ms: int
    ) -> ShadowCycleResult:
        """Registra o ciclo no log (se houver) e devolve o resultado.
        Todo ciclo é gravado — inclusive os pulados pelo pré-filtro."""
        if self._decision_log is not None:
            await self._decision_log.record(build_record(result, ts_ms=ts_ms))
        return result

    async def run_cycle(
        self, *, now_ms: int | None = None, allow_analysis: bool = True
    ) -> ShadowCycleResult:
        # 1. Relógio corrigido pelo skew do servidor (ou injetado nos testes).
        clock = self._clock
        if clock is None:  # pragma: no cover - caminho ao vivo
            clock = await self._market.clock_skew()

        spec = await self._market.instrument(self._symbol)

        # 2. Coleta COERENTE multi-timeframe (concorrente). O relógio é
        #    capturado DEPOIS do gather (dentro de fetch_coherent) — o `now`
        #    honesto é quando os dados chegam. `now_ms` só é injetado em teste.
        data = await fetch_coherent(
            self._market, timeframes=_TIMEFRAMES, clock=clock,
            local_now_ms=now_ms, symbol=self._symbol,
        )
        ob = data.orderbook
        now = data.corrected_now_ms

        # 3. Valida integridade e frescor — pega book cruzado, last fora do
        #    book, dados velhos, skew de relógio (os bugs que o Claude achou).
        issues = validate_market_data(data, max_data_age_ms=_MAX_DATA_AGE_MS)

        # 4. Monta o snapshot + data_quality explícito para o Claude.
        snapshot = build_snapshot(
            symbol=self._symbol,
            candles_by_tf=data.candles_by_tf,
            orderbook=ob,
            ticker=data.ticker,
            now_ms=now,
            data_ts_ms=ob.ts_ms,
        )
        snapshot["data_quality"] = _data_quality(issues, data.data_age_ms)

        # 5. Teto de orçamento (imposto pelo loop): sem verba, não chama o
        #    Claude — mas o ciclo ainda é registrado como pulado, com snapshot.
        if not allow_analysis:
            return await self._finalize(
                _skip_result(snapshot, "orçamento diário esgotado"), ts_ms=now
            )

        # 6. Pré-filtro determinístico (lever de custo): só chama o Claude se
        #    houver algo que valha analisar. Ciclos ociosos não gastam.
        structure = analyze_structure(data.candles_by_tf[_PRIMARY_TF])
        gate = prefilter(snapshot, structure, config=self._prefilter_config)
        if not gate.should_analyze:
            return await self._finalize(
                _skip_result(snapshot, gate.reason), ts_ms=now
            )

        # 7. Claude analisa.
        agent_result = self._agent.analyze(snapshot)

        # 8. Parseia a decisão em intenção de domínio.
        parsed = parse_decision(
            agent_result.decision, now_ms=now, max_leverage=self._policy.max_leverage
        )

        # 9. Se há intenção de abertura, o Risk Engine decide (shadow — não executa).
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
                data_age_ms=max(data.data_age_ms, 0),
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

        return await self._finalize(
            ShadowCycleResult(
                snapshot=snapshot,
                action=parsed.action,
                intent=parsed.intent,
                risk_decision=risk_decision,
                agent_result=agent_result,
            ),
            ts_ms=now,
        )


def _skip_result(snapshot: dict[str, Any], reason: str) -> ShadowCycleResult:
    """Resultado de um ciclo pulado (pré-filtro ou orçamento) — sem custo."""
    return ShadowCycleResult(
        snapshot=snapshot,
        action="NO_TRADE",
        intent=None,
        risk_decision=None,
        agent_result=None,
        analyzed=False,
        skip_reason=reason,
    )


def _estimate_liquidity(ob: Any) -> Decimal:
    """Liquidez disponível ~ soma dos tamanhos do lado relevante do book."""
    return sum((lvl.size for lvl in ob.asks), Decimal("0"))
