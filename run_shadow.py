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
from decimal import Decimal
from pathlib import Path

from bybit_agent.agent.client import DecisionAgent
from bybit_agent.agent.orchestrator import ShadowOrchestrator
from bybit_agent.agent.prefilter import prefilter
from bybit_agent.features.snapshot import build_snapshot
from bybit_agent.features.structure import analyze_structure
from bybit_agent.marketdata.coherent import fetch_coherent, validate_market_data
from bybit_agent.marketdata.rest import BybitPublicClient
from bybit_agent.risk.policy import RiskPolicy

SCHEMA_PATH = Path(__file__).parent / "contracts" / "decision_v1.json"
_TIMEFRAMES = ["60", "15", "5"]  # maior → menor (alinhamento multi-timeframe)


def _load_env() -> None:
    """Carrega .env sem dependência externa (chaves = valores simples)."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


async def _snapshot_only() -> None:
    """Sem chave Anthropic: prova A1+A2 com dados reais da Bybit.

    Coleta coerente multi-timeframe, valida integridade/frescor com relógio
    corrigido, monta o snapshot enriquecido (estrutura, liquidez) e mostra o
    veredito do pré-filtro — tudo de graça (market data é público)."""
    async with BybitPublicClient() as market:
        clock = await market.clock_skew()
        data = await fetch_coherent(
            market, timeframes=_TIMEFRAMES, clock=clock, symbol="BTCUSDT",
        )
    issues = validate_market_data(data, max_data_age_ms=10_000)
    snapshot = build_snapshot(
        symbol="BTCUSDT",
        candles_by_tf=data.candles_by_tf,
        orderbook=data.orderbook,
        ticker=data.ticker,
        now_ms=data.corrected_now_ms,
        data_ts_ms=data.orderbook.ts_ms,
    )
    snapshot["data_quality"] = {
        "status": "CONFLICTING" if issues else "VALID",
        "issues": [i.detail for i in issues],
    }
    print("=== SNAPSHOT (dados reais da Bybit) ===")
    print(json.dumps(snapshot, indent=2, ensure_ascii=False))

    structure = analyze_structure(data.candles_by_tf["5"])
    gate = prefilter(snapshot, structure)
    print(f"\n=== PRÉ-FILTRO: {'ANALISARIA' if gate.should_analyze else 'PULARIA'} ===")
    print(f"motivo: {gate.reason}")
    print(f"relógio: offset {clock.offset_ms}ms (saudável={clock.is_healthy()})")
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

    if not result.analyzed:
        print(f"\n=== PRÉ-FILTRO PULOU (sem custo): {result.skip_reason} ===")
        print("\n(shadow mode — nenhuma ordem enviada)")
        return

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
