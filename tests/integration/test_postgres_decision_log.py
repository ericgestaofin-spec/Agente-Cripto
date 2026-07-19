"""A3 — teste de INTEGRAÇÃO do PostgresDecisionLog contra um Postgres real.

Roda contra o banco de teste do docker-compose (porta 5433). É PULADO
automaticamente se não houver Postgres acessível — não trava a suíte unit em
máquina sem Docker.

    docker compose up -d postgres_test
    pytest tests/integration -q

Valida o que o unit test não pode: DDL aplica, JSONB round-trip, custo em
NUMERIC preserva o Decimal exato.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from bybit_agent.persistence.decision_log import DecisionRecord

_DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://agent:test@localhost:5433/bybit_agent_test"
)

asyncpg = pytest.importorskip("asyncpg")


async def _connect_or_skip():
    try:
        conn = await asyncpg.connect(_DSN, timeout=3)
    except Exception as exc:  # noqa: BLE001 - qualquer falha de conexão pula
        pytest.skip(f"Postgres de teste indisponível ({exc}); rode docker compose up")
    await conn.close()


def _record(**over) -> DecisionRecord:
    base = dict(
        decision_id="int-test-1", ts_ms=1_700_000_000_000, symbol="BTCUSDT",
        action="OPEN_LONG", analyzed=True, skip_reason=None,
        snapshot={"symbol": "BTCUSDT", "market_regime": "TRENDING_UP"},
        decision={"decision_id": "int-test-1", "action": "OPEN_LONG"},
        risk_verdict={"approved": True, "quantity": "0.5"},
        system_prompt_version="v9", cost_usd="0.01234567",
        usage={"input_tokens": 1000, "output_tokens": 200},
    )
    base.update(over)
    return DecisionRecord(**base)


@pytest.mark.asyncio
async def test_postgres_records_and_reads_back() -> None:
    await _connect_or_skip()
    from bybit_agent.persistence.postgres import PostgresDecisionLog

    log = await PostgresDecisionLog.create(_DSN)
    try:
        # tabela limpa para este teste
        async with log._pool.acquire() as conn:  # noqa: SLF001 - setup de teste
            await conn.execute("TRUNCATE decisions")

        await log.record(_record())
        await log.record(_record(decision_id="int-test-2", ts_ms=1_700_000_000_001,
                                 action="NO_TRADE", analyzed=False,
                                 skip_reason="parado", decision=None,
                                 risk_verdict=None, cost_usd="0"))

        recent = await log.recent(limit=10)
        assert len(recent) == 2
        # ordem cronológica ascendente após o reversed interno
        assert recent[0].decision_id == "int-test-1"
        assert recent[1].action == "NO_TRADE"

        # ⭐ NUMERIC preserva o Decimal exato (nada de float).
        assert Decimal(recent[0].cost_usd) == Decimal("0.01234567")
        # ⭐ JSONB round-trip preserva a estrutura.
        assert recent[0].snapshot["market_regime"] == "TRENDING_UP"
        assert recent[0].risk_verdict["quantity"] == "0.5"
        assert recent[1].decision is None
    finally:
        await log.close()
