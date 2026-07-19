"""Runner shadow — o pipeline ao vivo: Bybit → snapshot → Claude → Risk Engine.

SHADOW MODE: analisa e decide, mas NENHUMA ordem é enviada. É a Fase 3 do
plano — validar o pipeline inteiro com dados reais antes de qualquer
execução.

Uso:
    python run_shadow.py

Market data é público (sem chave). A chamada ao Claude precisa de
ANTHROPIC_API_KEY no ambiente; sem ela, o runner ainda puxa os dados reais
e monta o snapshot (provando a conexão com a Bybit), e apenas pula a
análise do modelo.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from decimal import Decimal
from pathlib import Path

from bybit_agent.agent.client import DecisionAgent
from bybit_agent.agent.orchestrator import ShadowOrchestrator
from bybit_agent.features.snapshot import build_snapshot
from bybit_agent.marketdata.rest import BybitPublicClient
from bybit_agent.risk.policy import RiskPolicy

SCHEMA_PATH = Path(__file__).parent / "contracts" / "decision_v1.json"


def _load_env() -> None:
    """Carrega .env sem dependência externa (chaves = valores simples)."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


async def _snapshot_only() -> None:
    """Sem chave Anthropic: prova a conexão com a Bybit montando o snapshot."""
    async with BybitPublicClient() as market:
        candles = await market.klines(interval="5", limit=200)
        ob = await market.orderbook(depth=50)
        ticker = await market.ticker()
    snapshot = build_snapshot(
        symbol="BTCUSDT",
        candles_by_tf={"5": candles},
        orderbook=ob,
        ticker=ticker,
        now_ms=int(time.time() * 1000),
        data_ts_ms=ob.ts_ms,
    )
    print("=== SNAPSHOT (dados reais da Bybit) ===")
    print(json.dumps(snapshot, indent=2, ensure_ascii=False))
    print("\nANTHROPIC_API_KEY não definida — análise do Claude pulada.")
    print("Defina a chave para rodar o ciclo completo.")


async def _full_cycle() -> None:
    """Com chave: ciclo completo Bybit → Claude → Risk Engine (shadow)."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    async with BybitPublicClient() as market:
        agent = DecisionAgent(decision_schema=schema)
        orch = ShadowOrchestrator(
            market=market,
            agent=agent,
            policy=RiskPolicy.conservative_v0(),
            account_equity=Decimal("10000"),  # patrimônio simulado (shadow)
        )
        result = await orch.run_cycle()

    print("=== SNAPSHOT ===")
    print(json.dumps(result.snapshot, indent=2, ensure_ascii=False))
    print(f"\n=== DECISÃO DO CLAUDE: {result.action} ===")
    print(json.dumps(result.agent_result.decision, indent=2, ensure_ascii=False))
    print(f"\ntokens: {result.agent_result.usage}")

    if result.risk_decision is not None:
        rd = result.risk_decision
        print("\n=== VEREDITO DO RISK ENGINE ===")
        if rd.approved:
            print(f"APROVADO — quantidade {rd.quantity} "
                  f"(binding: {rd.binding_constraint}, RR calculado: {rd.computed_rr_net})")
        else:
            print("REJEITADO:")
            for r in rd.rejections:
                print(f"  [{r.code}] {r.detail}")
    print("\n(shadow mode — nenhuma ordem enviada)")


async def main() -> None:
    _load_env()
    if os.environ.get("ANTHROPIC_API_KEY"):
        await _full_cycle()
    else:
        await _snapshot_only()


if __name__ == "__main__":
    asyncio.run(main())
