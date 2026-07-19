"""Cálculo de relação risco/retorno — o motor calcula, o modelo não declara.

Achado 1 do review externo (crítico): o `rr_net` vinha declarado na
intenção do modelo, e o motor confiava nele. Um `rr_net=999` com TP vazio
era aprovado — o modelo influenciava a decisão de risco.

Aqui o RR é derivado dos preços de TP, da entrada, do stop e dos custos
reais. O valor que o modelo eventualmente declare fica apenas como
diagnóstico; a aprovação usa este cálculo.

Biblioteca pura, sob o contexto Decimal do sistema (traps ativas).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Literal

from bybit_agent.domain.money import decimal_context

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True, slots=True)
class TakeProfitLevel:
    """Um alvo de realização: preço + fração da posição a fechar ali."""

    price: Decimal
    fraction: Decimal

    def __post_init__(self) -> None:
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError(f"preço de TP deve ser positivo e finito, recebido {self.price!r}")
        if not self.fraction.is_finite() or not (0 < self.fraction <= 1):
            raise ValueError(
                f"fração de TP deve estar em (0, 1], recebido {self.fraction!r}"
            )


def compute_rr_net(
    *,
    side: Side,
    entry: Decimal,
    stop: Decimal,
    take_profit_levels: tuple[TakeProfitLevel, ...],
    entry_fee_rate: Decimal,
    exit_fee_rate: Decimal,
    entry_slippage: Decimal,
    exit_slippage: Decimal,
) -> Decimal:
    """RR líquido a partir dos alvos e custos. Levanta se o plano é incoerente.

    - risco/unidade = |entrada − stop| + taxas(entrada+stop) + slippage_entrada
      (IDÊNTICO ao risco usado no sizing — as duas definições não podem
      divergir, senão o RR aprovado não corresponde ao trade dimensionado)
    - recompensa/unidade = Σ fração_i · (|TP_i − entrada| − taxa_saída − slippage_saída)
    - RR = recompensa / risco

    Exige plano de saída COMPLETO (frações somam 1): sem isso o RR é ambíguo
    (parte da posição sem alvo definido), e um motor de risco não pode
    superestimar o retorno. Taxas efetivas são >= 0 (rebate não infla o RR).
    """
    with localcontext(decimal_context()):
        if not take_profit_levels:
            raise ValueError("plano sem take-profit: RR indefinido")

        total_fraction = sum((tp.fraction for tp in take_profit_levels), Decimal("0"))
        if total_fraction != 1:
            raise ValueError(
                f"frações de TP devem somar 1 (plano completo), somam {total_fraction}"
            )

        entry_fee = max(entry_fee_rate, Decimal("0"))
        exit_fee = max(exit_fee_rate, Decimal("0"))

        distance = abs(entry - stop)
        risk_per_unit = distance + (entry + stop) * entry_fee + entry_slippage
        if risk_per_unit <= 0:  # pragma: no cover - distância>0 garante isto
            raise ValueError("risco por unidade não-positivo")

        reward_per_unit = Decimal("0")
        for tp in take_profit_levels:
            if side == "BUY" and tp.price <= entry:
                raise ValueError(f"LONG: alvo {tp.price} não está acima da entrada {entry}")
            if side == "SELL" and tp.price >= entry:
                raise ValueError(f"SHORT: alvo {tp.price} não está abaixo da entrada {entry}")
            gross_gain = abs(tp.price - entry)
            exit_cost = tp.price * exit_fee + exit_slippage
            net_gain = gross_gain - exit_cost
            reward_per_unit += tp.fraction * net_gain

        return reward_per_unit / risk_per_unit
