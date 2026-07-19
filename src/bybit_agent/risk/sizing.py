"""Cálculo de quantidade — o modelo nunca faz isto; este módulo faz.

A quantidade final é o MÍNIMO entre a quantidade permitida pelo risco e
todos os tetos (exposição, alavancagem, liquidez, símbolo), arredondada
SEMPRE para baixo pelo qtyStep. Abaixo do mínimo da corretora → rejeita.

Biblioteca pura: sem I/O, sem estado. `compute_size` é uma função
determinística de suas entradas.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from bybit_agent.domain.instrument import InstrumentSpec, OrderType
from bybit_agent.domain.money import Price, Quantity, round_down_to_step

BindingConstraint = Literal[
    "risk_budget", "leverage", "liquidity", "symbol_max", "none"
]


@dataclass(frozen=True, slots=True)
class SizingInputs:
    equity: Price
    entry: Price
    stop: Price
    risk_fraction: Decimal
    taker_fee_rate: Decimal
    estimated_slippage: Price
    max_leverage: Decimal
    available_liquidity: Quantity
    spec: InstrumentSpec
    order_type: OrderType = "Limit"


@dataclass(frozen=True, slots=True)
class SizingResult:
    approved: bool
    quantity: Quantity | None
    quantity_unrounded: Decimal
    risk_per_unit: Decimal
    risk_budget: Decimal
    binding_constraint: BindingConstraint
    rejection_reason: str = ""


def _rejected(reason: str, *, risk_per_unit: Decimal, budget: Decimal) -> SizingResult:
    return SizingResult(
        approved=False,
        quantity=None,
        quantity_unrounded=Decimal("0"),
        risk_per_unit=risk_per_unit,
        risk_budget=budget,
        binding_constraint="none",
        rejection_reason=reason,
    )


def compute_size(inp: SizingInputs) -> SizingResult:
    """Calcula a quantidade máxima permitida pelo risco e pelos tetos.

    Retorna sempre um SizingResult — aprovação ou rejeição com motivo.
    Nunca levanta exceção por entrada de mercado válida; erros de
    configuração (spec inconsistente) podem levantar.
    """
    budget = inp.equity.value * inp.risk_fraction
    if budget <= 0:
        return _rejected(
            "orçamento de risco é zero ou negativo",
            risk_per_unit=Decimal("0"),
            budget=budget,
        )

    # Distância entrada→stop, sempre absoluta (cobre LONG e SHORT).
    distance = abs(inp.entry.value - inp.stop.value)
    if distance <= 0:
        return _rejected(
            "distância entre entrada e stop é zero",
            risk_per_unit=Decimal("0"),
            budget=budget,
        )

    # Taxa incide na entrada e na saída (ida e volta).
    fee_per_unit = (inp.entry.value + inp.stop.value) * inp.taker_fee_rate
    risk_per_unit = distance + fee_per_unit + inp.estimated_slippage.value

    # Guarda defensiva: com distance > 0 (garantido acima) e taxas/slippage
    # de mercado não-negativas, risk_per_unit é sempre > 0. Só atingível com
    # um rebate de taxa artificialmente negativo — mantida por segurança num
    # cálculo de dinheiro, mas não representa entrada de mercado válida.
    if risk_per_unit <= 0:  # pragma: no cover
        return _rejected(
            "risco por unidade é zero ou negativo",
            risk_per_unit=risk_per_unit,
            budget=budget,
        )

    qty_by_risk = budget / risk_per_unit

    # Tetos — a quantidade final é o menor de todos.
    max_notional = inp.equity.value * inp.max_leverage
    qty_by_leverage = max_notional / inp.entry.value
    qty_by_liquidity = inp.available_liquidity.value
    qty_by_symbol = inp.spec.max_qty_for(order_type=inp.order_type)

    caps: list[tuple[BindingConstraint, Decimal]] = [
        ("risk_budget", qty_by_risk),
        ("leverage", qty_by_leverage),
        ("liquidity", qty_by_liquidity),
        ("symbol_max", qty_by_symbol),
    ]
    binding, unrounded = min(caps, key=lambda c: c[1])

    rounded = round_down_to_step(Quantity(unrounded), inp.spec.qty_step)

    # Abaixo do mínimo da corretora → rejeita. NUNCA arredonda para cima.
    if rounded.value < inp.spec.min_order_qty:
        return _rejected(
            f"quantidade {rounded.value} abaixo do mínimo da corretora "
            f"({inp.spec.min_order_qty})",
            risk_per_unit=risk_per_unit,
            budget=budget,
        )

    notional = rounded.value * inp.entry.value
    if notional < inp.spec.min_notional:
        return _rejected(
            f"notional {notional} abaixo do mínimo ({inp.spec.min_notional})",
            risk_per_unit=risk_per_unit,
            budget=budget,
        )

    return SizingResult(
        approved=True,
        quantity=rounded,
        quantity_unrounded=unrounded,
        risk_per_unit=risk_per_unit,
        risk_budget=budget,
        binding_constraint=binding,
    )
