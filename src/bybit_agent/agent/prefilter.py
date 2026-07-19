"""Pré-filtro determinístico — o terceiro lever de custo (§1.5 do plano).

Antes de gastar uma chamada ao Claude (~5 centavos), um gate barato decide
se há ALGO que valha analisar. Cadência e teto diário limitam a frequência;
o pré-filtro corta os ciclos ociosos — mercado parado, sem evento, dados
ruins ou spread proibitivo.

Conservador por design: na dúvida, ANALISA. Só pula quando há razão clara.
Cada decisão vem com um motivo legível (auditoria e calibração de custo).

Puro: recebe o snapshot e a estrutura já calculados, não busca nada.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bybit_agent.features.structure import MarketStructure


@dataclass(frozen=True, slots=True)
class PrefilterConfig:
    max_spread_bps: Decimal = Decimal("5")
    min_realized_vol: Decimal = Decimal("0.001")


@dataclass(frozen=True, slots=True)
class PrefilterResult:
    should_analyze: bool
    reason: str


def _skip(reason: str) -> PrefilterResult:
    return PrefilterResult(False, reason)


def _analyze(reason: str) -> PrefilterResult:
    return PrefilterResult(True, reason)


def prefilter(
    snapshot: dict,
    structure: MarketStructure,
    *,
    config: PrefilterConfig | None = None,
) -> PrefilterResult:
    """Decide se este ciclo merece uma chamada ao Claude."""
    cfg = config or PrefilterConfig()

    # 1. Dados incoerentes — analisar seria lixo entra, lixo sai. Pula.
    status = snapshot.get("data_quality", {}).get("status", "VALID")
    if status == "CONFLICTING":
        return _skip("dados incoerentes (data_quality=CONFLICTING)")

    # 2. Spread proibitivo — qualquer trade nasceria caro demais.
    spread = Decimal(str(snapshot["liquidity"]["spread_bps"]))
    if spread > cfg.max_spread_bps:
        return _skip(f"spread {spread}bps > máximo {cfg.max_spread_bps}bps")

    # 3. Evento de estrutura ou tendência clara — vale analisar. Um CHoCH é
    #    sempre um BOS (contra a tendência), então o check de BOS já o cobre.
    if structure.bos is not None:
        kind = "CHoCH" if structure.choch else "BOS"
        return _analyze(f"quebra de estrutura ({kind} {structure.bos})")
    if snapshot.get("market_regime") in ("TRENDING_UP", "TRENDING_DOWN"):
        return _analyze(f"tendência ({snapshot['market_regime']})")

    # 4. Sem evento: só analisa se há volatilidade suficiente para haver setup.
    rv = snapshot.get("volatility", {}).get("realized_volatility")
    if rv is not None and Decimal(str(rv)) >= cfg.min_realized_vol:
        return _analyze("volatilidade suficiente")

    return _skip("sem evento; mercado parado")
