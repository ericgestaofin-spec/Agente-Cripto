"""Persistência das decisões em Postgres (asyncpg).

Implementa a interface `DecisionLog`. Os campos ricos (snapshot, decisão,
veredito, usage) vão em colunas JSONB — consultáveis depois para avaliação.
O custo fica em NUMERIC (dinheiro é exato, nunca float).

I/O puro: marcado `pragma: no cover` — a LÓGICA (serialização, custo, id)
está em decision_log.py com 100% de cobertura. Esta classe é validada por
smoke test contra um Postgres real (docker-compose), não por unit test que
só exercitaria o mock do driver.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from bybit_agent.persistence.decision_log import DecisionRecord

if TYPE_CHECKING:
    import asyncpg


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS decisions (
    id                    BIGSERIAL PRIMARY KEY,
    decision_id           TEXT NOT NULL,
    ts_ms                 BIGINT NOT NULL,
    symbol                TEXT NOT NULL,
    action                TEXT NOT NULL,
    analyzed              BOOLEAN NOT NULL,
    skip_reason           TEXT,
    snapshot              JSONB NOT NULL,
    decision              JSONB,
    risk_verdict          JSONB,
    system_prompt_version TEXT,
    cost_usd              NUMERIC NOT NULL DEFAULT 0,
    usage                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions (ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_symbol ON decisions (symbol, ts_ms DESC);
"""


class PostgresDecisionLog:  # pragma: no cover - I/O asyncpg, validado por smoke real
    """DecisionLog respaldado em Postgres. Use `create` para abrir o pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def create(cls, dsn: str, *, min_size: int = 1, max_size: int = 5) -> PostgresDecisionLog:
        import asyncpg

        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        assert pool is not None
        log = cls(pool)
        await log.ensure_schema()
        return log

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_DDL)

    async def record(self, rec: DecisionRecord) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO decisions (
                    decision_id, ts_ms, symbol, action, analyzed, skip_reason,
                    snapshot, decision, risk_verdict, system_prompt_version,
                    cost_usd, usage
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7::jsonb, $8::jsonb, $9::jsonb, $10,
                    $11::numeric, $12::jsonb
                )
                """,
                rec.decision_id, rec.ts_ms, rec.symbol, rec.action, rec.analyzed,
                rec.skip_reason,
                json.dumps(rec.snapshot),
                json.dumps(rec.decision) if rec.decision is not None else None,
                json.dumps(rec.risk_verdict) if rec.risk_verdict is not None else None,
                rec.system_prompt_version,
                rec.cost_usd,
                json.dumps(rec.usage),
            )

    async def recent(self, *, limit: int = 50) -> list[DecisionRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM decisions ORDER BY ts_ms DESC LIMIT $1", limit
            )
        return [_row_to_record(dict(r)) for r in reversed(rows)]

    async def close(self) -> None:
        await self._pool.close()


def _row_to_record(row: dict[str, Any]) -> DecisionRecord:  # pragma: no cover - I/O
    def _j(v: Any) -> Any:
        return json.loads(v) if isinstance(v, str) else v

    return DecisionRecord(
        decision_id=row["decision_id"],
        ts_ms=row["ts_ms"],
        symbol=row["symbol"],
        action=row["action"],
        analyzed=row["analyzed"],
        skip_reason=row["skip_reason"],
        snapshot=_j(row["snapshot"]),
        decision=_j(row["decision"]),
        risk_verdict=_j(row["risk_verdict"]),
        system_prompt_version=row["system_prompt_version"],
        cost_usd=str(row["cost_usd"]),
        usage=_j(row["usage"]),
    )
