"""Risk Engine — a autoridade final sobre toda operação.

Recebe uma `TradeIntent` (traduzida da decisão do modelo) e o estado da
conta, e produz uma `RiskDecision`: aprovada com quantidade calculada, ou
rejeitada com a lista completa de motivos.

Duas garantias que definem a arquitetura:

  1. **A quantidade nunca vem da intenção.** `TradeIntent` não tem campo
     de quantidade, tamanho ou alavancagem — não há como o modelo
     influenciar o dimensionamento. O tamanho é sempre calculado aqui.

  2. **Validação antes de sizing.** Se qualquer regra rejeita, o sizing
     não roda. Um trade proibido não recebe dimensionamento.

Biblioteca pura: sem I/O, sem estado, sem relógio interno (o `now_ms` é
injetado). Determinística por construção.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from bybit_agent.domain.instrument import InstrumentSpec, OrderType
from bybit_agent.domain.money import Price, Quantity
from bybit_agent.risk.policy import RiskPolicy
from bybit_agent.risk.sizing import BindingConstraint, SizingInputs, compute_size
from bybit_agent.risk.validators import (
    AccountState,
    Rejection,
    Side,
    TradeContext,
    validate,
)


@dataclass(frozen=True, slots=True)
class TradeIntent:
    """A intenção do modelo, já traduzida para tipos de domínio.

    Repare no que NÃO existe aqui: quantidade, tamanho, alavancagem,
    margem. O modelo propõe direção, níveis e tese — nunca o tamanho.
    """

    decision_id: str
    symbol: str
    side: Side
    entry: Decimal
    stop: Decimal
    invalidation: Decimal
    liquidation: Decimal
    rr_net: Decimal
    intent_expires_at_ms: int
    take_profit_fractions: list[Decimal]
    is_averaging_down: bool
    widens_stop: bool
    order_type: OrderType = "Limit"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    quantity: Quantity | None
    rejections: list[Rejection]
    policy_hash: str
    risk_budget: Decimal | None = None
    risk_per_unit: Decimal | None = None
    binding_constraint: BindingConstraint | None = None


def evaluate(
    intent: TradeIntent,
    account: AccountState,
    *,
    spec: InstrumentSpec,
    policy: RiskPolicy,
    taker_fee_rate: Decimal,
    estimated_slippage: Decimal,
    available_liquidity: Decimal,
    now_ms: int,
) -> RiskDecision:
    """Avalia a intenção contra as regras e calcula a quantidade.

    Retorna sempre uma RiskDecision — nunca levanta por intenção de
    mercado válida.
    """
    # 1. Validação primeiro. Trade proibido não recebe dimensionamento.
    context = TradeContext(
        symbol=intent.symbol,
        side=intent.side,
        entry=intent.entry,
        stop=intent.stop,
        invalidation=intent.invalidation,
        liquidation=intent.liquidation,
        rr_net=intent.rr_net,
        intent_expires_at_ms=intent.intent_expires_at_ms,
        now_ms=now_ms,
        take_profit_fractions=intent.take_profit_fractions,
        is_averaging_down=intent.is_averaging_down,
        widens_stop=intent.widens_stop,
    )
    outcome = validate(account, context, policy)
    if not outcome.approved:
        return RiskDecision(
            approved=False,
            quantity=None,
            rejections=outcome.rejections,
            policy_hash=policy.policy_hash,
        )

    # 2. Sizing. A quantidade é calculada aqui, nunca fornecida.
    sizing = compute_size(
        SizingInputs(
            equity=Price(account.equity),
            entry=Price(intent.entry),
            stop=Price(intent.stop),
            risk_fraction=policy.max_risk_per_trade,
            taker_fee_rate=taker_fee_rate,
            estimated_slippage=Price(estimated_slippage),
            max_leverage=policy.max_leverage,
            available_liquidity=Quantity(available_liquidity),
            spec=spec,
            order_type=intent.order_type,
        )
    )
    if not sizing.approved:
        return RiskDecision(
            approved=False,
            quantity=None,
            rejections=[Rejection("UNSIZABLE", sizing.rejection_reason)],
            policy_hash=policy.policy_hash,
        )

    return RiskDecision(
        approved=True,
        quantity=sizing.quantity,
        rejections=[],
        policy_hash=policy.policy_hash,
        risk_budget=sizing.risk_budget,
        risk_per_unit=sizing.risk_per_unit,
        binding_constraint=sizing.binding_constraint,
    )
