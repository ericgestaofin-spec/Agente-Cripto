"""Sprint 8 (shadow) — orquestrador de um ciclo de decisão.

Amarra: market data → snapshot → Claude → parser → Risk Engine. Em shadow
mode, NENHUMA ordem é enviada — só se registra a decisão e o veredito de
risco. Testado com fakes (sem rede, sem chave).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from bybit_agent.agent.orchestrator import ShadowOrchestrator
from bybit_agent.marketdata.clock import ClockSkew
from bybit_agent.marketdata.rest import BookLevel, Candle, OrderBook, Ticker
from bybit_agent.risk.policy import RiskPolicy

# Skew são injetado nos testes — sem rede para medir o relógio do servidor.
_CLOCK = ClockSkew(offset_ms=0, max_offset_ms=500)


class _FakeMarket:
    async def instrument(self, symbol: str = "BTCUSDT") -> Any:
        from bybit_agent.domain.instrument import InstrumentSpec

        return InstrumentSpec.from_bybit({
            "symbol": "BTCUSDT", "status": "Trading",
            "priceFilter": {"tickSize": "0.10", "minPrice": "0.10", "maxPrice": "999999"},
            "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001",
                              "maxOrderQty": "500", "maxMktOrderQty": "100",
                              "minNotionalValue": "5"},
            "leverageFilter": {"minLeverage": "1", "maxLeverage": "100"},
        })

    async def klines(self, symbol: str = "BTCUSDT", interval: str = "5",
                     limit: int = 200) -> list[Candle]:
        return [
            Candle(start_ms=1_700_000_000_000 + i * 300_000,
                   open=Decimal(60000 + i * 5), high=Decimal(60050 + i * 5),
                   low=Decimal(59950 + i * 5), close=Decimal(60010 + i * 5),
                   volume=Decimal("10"), turnover=Decimal("600000"))
            for i in range(60)
        ]

    async def orderbook(self, symbol: str = "BTCUSDT", depth: int = 50) -> OrderBook:
        return OrderBook(
            symbol="BTCUSDT",
            bids=(BookLevel(Decimal("60300"), Decimal("2")),),
            asks=(BookLevel(Decimal("60301"), Decimal("2")),),
            ts_ms=1_700_000_000_000, update_id=1,
        )

    async def ticker(self, symbol: str = "BTCUSDT") -> Ticker:
        return Ticker(symbol="BTCUSDT", last_price=Decimal("60300"),
                      mark_price=Decimal("60300"), index_price=Decimal("60300"),
                      bid1_price=Decimal("60300"), ask1_price=Decimal("60301"),
                      funding_rate=Decimal("0.0001"), open_interest=Decimal("50000"))


class _FakeAgent:
    def __init__(self, decision: dict[str, Any]) -> None:
        self._decision = decision
        self.seen_snapshot: dict[str, Any] | None = None

    def analyze(self, snapshot: dict[str, Any]) -> Any:
        from bybit_agent.agent.client import AgentResult

        self.seen_snapshot = snapshot
        return AgentResult(decision=self._decision, system_prompt_version="v")


def _open_long() -> dict[str, Any]:
    return {
        "decision_id": "d1", "symbol": "BTCUSDT", "action": "OPEN_LONG",
        "entry": {"type": "LIMIT", "price": "60300.00", "expires_at": None},
        "risk_plan": {
            "invalidation_price": "59900.00", "stop_loss": "59700.00",
            "take_profit_levels": [{"price": "61500.00", "close_fraction": "1",
                                    "reason": "alvo"}],
            "estimated_rr_net": "2.0",
        },
    }


def _no_trade() -> dict[str, Any]:
    return {"decision_id": "d2", "symbol": "BTCUSDT", "action": "NO_TRADE",
            "summary": "range"}


@pytest.mark.asyncio
async def test_cycle_no_trade_produces_no_risk_decision() -> None:
    orch = ShadowOrchestrator(
        market=_FakeMarket(), agent=_FakeAgent(_no_trade()),
        policy=RiskPolicy.conservative_v0(), account_equity=Decimal("100000"),
        clock=_CLOCK,
    )
    result = await orch.run_cycle(now_ms=1_700_000_000_000 + 60 * 300_000)
    assert result.action == "NO_TRADE"
    assert result.risk_decision is None
    assert result.intent is None


@pytest.mark.asyncio
async def test_cycle_open_long_runs_risk_engine() -> None:
    """⭐ O ciclo completo: dados → snapshot → Claude → parser → Risk Engine."""
    agent = _FakeAgent(_open_long())
    orch = ShadowOrchestrator(
        market=_FakeMarket(), agent=agent,
        policy=RiskPolicy.conservative_v0(), account_equity=Decimal("100000"),
        clock=_CLOCK,
    )
    result = await orch.run_cycle(now_ms=1_700_000_000_000 + 60 * 300_000)
    assert result.action == "OPEN_LONG"
    assert result.intent is not None
    assert result.risk_decision is not None
    # o snapshot real (com regime, spread) chegou ao agente
    assert agent.seen_snapshot is not None
    assert agent.seen_snapshot["symbol"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_cycle_never_executes_orders() -> None:
    """⭐ Shadow mode: o resultado não tem nenhuma ordem enviada — só a
    decisão e o veredito de risco. Garantia estrutural."""
    orch = ShadowOrchestrator(
        market=_FakeMarket(), agent=_FakeAgent(_open_long()),
        policy=RiskPolicy.conservative_v0(), account_equity=Decimal("100000"),
        clock=_CLOCK,
    )
    result = await orch.run_cycle(now_ms=1_700_000_000_000 + 60 * 300_000)
    assert not hasattr(result, "order_id")
    assert not hasattr(result, "sent_order")


@pytest.mark.asyncio
async def test_cycle_result_carries_snapshot_for_audit() -> None:
    orch = ShadowOrchestrator(
        market=_FakeMarket(), agent=_FakeAgent(_no_trade()),
        policy=RiskPolicy.conservative_v0(), account_equity=Decimal("100000"),
        clock=_CLOCK,
    )
    result = await orch.run_cycle(now_ms=1_700_000_000_000 + 60 * 300_000)
    assert result.snapshot["symbol"] == "BTCUSDT"
    assert "market_regime" in result.snapshot
