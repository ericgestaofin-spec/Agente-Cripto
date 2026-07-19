"""Runner do LOOP shadow contínuo — o analista vivo, decidindo por candle.

Amarra tudo: Bybit → snapshot → pré-filtro → Claude → Risk Engine → log,
com cadência (por candle), teto diário de custo e resiliência. SHADOW MODE:
nenhuma ordem é enviada.

Uso:
    python run_shadow_loop.py

Variáveis de ambiente (.env é carregado):
    ANTHROPIC_API_KEY   necessária para a análise do Claude.
    DATABASE_URL        se setada, persiste em Postgres; senão, memória.
    SHADOW_TF           timeframe de decisão (default 5).
    SHADOW_DAILY_USD    teto diário de custo em USD (default 5).

Encerre com Ctrl+C.
"""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path

from bybit_agent.agent.client import DecisionAgent
from bybit_agent.agent.orchestrator import ShadowCycleResult, ShadowOrchestrator
from bybit_agent.marketdata.rest import BybitPublicClient
from bybit_agent.marketdata.scheduler import CandleScheduler, DailyBudget
from bybit_agent.persistence.decision_log import DecisionLog, InMemoryDecisionLog
from bybit_agent.risk.policy import RiskPolicy
from bybit_agent.runtime.loop import ShadowLoop

SCHEMA_PATH = Path(__file__).parent / "contracts" / "decision_v1.json"


def _load_env() -> None:
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


async def _make_log() -> DecisionLog:
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        from bybit_agent.persistence.postgres import PostgresDecisionLog

        print(f"persistência: Postgres ({dsn.split('@')[-1]})")
        return await PostgresDecisionLog.create(dsn)
    print("persistência: memória (defina DATABASE_URL para Postgres)")
    return InMemoryDecisionLog()


async def _on_cycle(result: ShadowCycleResult) -> None:
    if not result.analyzed:
        print(f"  · pulado: {result.skip_reason}")
        return
    line = f"  → {result.action}"
    if result.risk_decision is not None:
        rd = result.risk_decision
        line += (f" | risk: {'APROVADO' if rd.approved else 'REJEITADO'}"
                 f" qty={rd.quantity.value if rd.quantity else '-'}")
    print(line)


async def main() -> None:
    _load_env()
    tf = os.environ.get("SHADOW_TF", "5")
    daily_usd = Decimal(os.environ.get("SHADOW_DAILY_USD", "5"))

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY não definida — o loop precisa dela para analisar.")
        print("Defina a chave no .env e rode de novo.")
        return

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    log = await _make_log()
    async with BybitPublicClient() as market:
        orch = ShadowOrchestrator(
            market=market,
            agent=DecisionAgent(decision_schema=schema),
            policy=RiskPolicy.conservative_v0(),
            account_equity=Decimal("10000"),  # patrimônio simulado (shadow)
            decision_log=log,
        )
        loop = ShadowLoop(
            orchestrator=orch,
            scheduler=CandleScheduler(interval=tf),
            budget=DailyBudget(max_usd=daily_usd),
        )
        print(f"loop shadow iniciado — candle {tf}m, teto ${daily_usd}/dia. Ctrl+C encerra.")
        try:
            await loop.run_forever(poll_interval_s=5.0, on_cycle=_on_cycle)
        except KeyboardInterrupt:
            pass
    s = loop.stats
    print(f"\nencerrado — ciclos={s.cycles} analisados={s.analyzed} "
          f"pulados={s.skipped} erros={s.errors} custo=${s.total_cost_usd}")


if __name__ == "__main__":
    asyncio.run(main())
