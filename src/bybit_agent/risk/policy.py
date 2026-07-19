"""Política de risco — a autoridade do sistema.

O requisito central da especificação: **o modelo não pode modificar estes
valores.** Nem por prompt, nem por ferramenta, nem por variável de ambiente.

Três garantias mecânicas:
  1. `frozen=True` + `slots=True` — impossível mutar ou injetar atributo.
  2. Carregada de arquivo em disco, nunca de env var nem da API.
  3. `policy_hash` (SHA-256) vai no event log de toda decisão — se a
     política mudar, é auditável qual decisão usou qual versão.

A imutabilidade não é conveniência de design; é o controle que impede que
um bug (ou um prompt malicioso) afrouxe um limite de risco em runtime.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

# Teto de sanidade: acima disso, é erro de digitação, não estratégia.
# 0,25 no lugar de 0,0025 é o erro que apaga a conta num único trade.
_MAX_PLAUSIBLE_RISK_PER_TRADE = Decimal("0.05")


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """Limites de risco imutáveis. Ver docs/PLANO.md §5.2."""

    max_risk_per_trade: Decimal
    max_total_risk: Decimal
    max_daily_loss: Decimal
    max_weekly_loss: Decimal
    max_concurrent_positions: int
    max_leverage: Decimal
    min_rr_net: Decimal
    max_consecutive_losses: int
    max_daily_entries: int
    max_spread_bps: Decimal
    max_slippage_bps: Decimal
    max_data_age_ms: int
    allowed_symbols: frozenset[str]

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if self.max_risk_per_trade <= 0:
            raise ValueError("max_risk_per_trade deve ser positivo")
        if self.max_risk_per_trade > _MAX_PLAUSIBLE_RISK_PER_TRADE:
            raise ValueError(
                f"max_risk_per_trade implausível ({self.max_risk_per_trade}); "
                f"teto de sanidade é {_MAX_PLAUSIBLE_RISK_PER_TRADE}. "
                f"Verifique se não confundiu 0,25% com 25%."
            )
        if self.max_risk_per_trade > self.max_total_risk:
            raise ValueError(
                "risco por operação não pode exceder o risco total simultâneo"
            )
        if self.max_daily_loss > self.max_weekly_loss:
            raise ValueError("perda diária não pode exceder a perda semanal")
        if self.max_leverage < 1:
            raise ValueError("alavancagem máxima deve ser >= 1")
        if self.min_rr_net < 1:
            raise ValueError("relação risco/retorno mínima deve ser >= 1")
        if self.max_concurrent_positions < 1:
            raise ValueError("max_concurrent_positions deve ser >= 1")
        if not self.allowed_symbols:
            raise ValueError("a lista de símbolos permitidos não pode ser vazia")

    @property
    def policy_hash(self) -> str:
        """SHA-256 determinístico do conjunto de regras.

        Ordenado por nome de campo para ser estável entre execuções.
        Gravado em cada decisão para rastreabilidade da versão da política.
        """
        payload = {
            f.name: sorted(getattr(self, f.name))
            if f.name == "allowed_symbols"
            else str(getattr(self, f.name))
            for f in sorted(fields(self), key=lambda x: x.name)
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def replace(self, **changes: Any) -> RiskPolicy:
        """Retorna uma nova política com os campos alterados.

        A política original permanece intacta — é imutável. Revalida os
        invariantes na construção da nova instância.
        """
        return replace(self, **changes)

    @classmethod
    def conservative_v0(cls) -> RiskPolicy:
        """Valores iniciais conservadores da spec — parâmetros de engenharia
        para validação, não recomendação financeira."""
        return cls(
            max_risk_per_trade=Decimal("0.0025"),
            max_total_risk=Decimal("0.0050"),
            max_daily_loss=Decimal("0.0100"),
            max_weekly_loss=Decimal("0.0300"),
            max_concurrent_positions=1,
            max_leverage=Decimal("2"),
            min_rr_net=Decimal("2.0"),
            max_consecutive_losses=2,
            max_daily_entries=3,
            max_spread_bps=Decimal("5"),
            max_slippage_bps=Decimal("10"),
            max_data_age_ms=5000,
            allowed_symbols=frozenset({"BTCUSDT"}),
        )


def load_policy(path: Path) -> RiskPolicy:
    """Carrega a política de um arquivo JSON em disco.

    Valores numéricos DEVEM ser strings no arquivo — um número JSON vira
    float no parse, e 0.0025 já entraria com erro de representação. A
    detecção de float é explícita e bloqueante.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))

    def dec(key: str) -> Decimal:
        val = raw[key]
        if isinstance(val, float):
            raise TypeError(
                f"política: '{key}' é float ({val}); use string decimal no arquivo"
            )
        return Decimal(str(val))

    return RiskPolicy(
        max_risk_per_trade=dec("max_risk_per_trade"),
        max_total_risk=dec("max_total_risk"),
        max_daily_loss=dec("max_daily_loss"),
        max_weekly_loss=dec("max_weekly_loss"),
        max_concurrent_positions=int(raw["max_concurrent_positions"]),
        max_leverage=dec("max_leverage"),
        min_rr_net=dec("min_rr_net"),
        max_consecutive_losses=int(raw["max_consecutive_losses"]),
        max_daily_entries=int(raw["max_daily_entries"]),
        max_spread_bps=dec("max_spread_bps"),
        max_slippage_bps=dec("max_slippage_bps"),
        max_data_age_ms=int(raw["max_data_age_ms"]),
        allowed_symbols=frozenset(raw["allowed_symbols"]),
    )
