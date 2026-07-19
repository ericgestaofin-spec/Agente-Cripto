"""Sprint 4e — orquestração do Risk Engine.

O engine é a autoridade final: recebe uma intenção do modelo, roda os
validadores E o sizing, e devolve uma decisão de risco — aprovada com
quantidade calculada, ou rejeitada com a lista de motivos.

Regra de ouro: validação primeiro. Se qualquer regra rejeita, o sizing
nem roda — não faz sentido calcular quantidade para um trade proibido.
E a quantidade NUNCA vem da intenção; é sempre calculada aqui.
"""

from __future__ import annotations

from decimal import Decimal

from bybit_agent.domain.instrument import InstrumentSpec
from bybit_agent.risk.engine import RiskDecision, TradeIntent, evaluate
from bybit_agent.risk.policy import RiskPolicy
from bybit_agent.risk.validators import AccountState


def _spec() -> InstrumentSpec:
    return InstrumentSpec.from_bybit(
        {
            "symbol": "BTCUSDT",
            "status": "Trading",
            "priceFilter": {"tickSize": "0.10", "minPrice": "0.10", "maxPrice": "999999"},
            "lotSizeFilter": {
                "qtyStep": "0.001",
                "minOrderQty": "0.001",
                "maxOrderQty": "500",
                "maxMktOrderQty": "100",
                "minNotionalValue": "5",
            },
            "leverageFilter": {"minLeverage": "1", "maxLeverage": "100"},
        }
    )


def _account(**over: object) -> AccountState:
    base: dict[str, object] = {
        "equity": Decimal("100000"),
        "daily_pnl": Decimal("0"),
        "weekly_pnl": Decimal("0"),
        "open_positions": 0,
        "open_orders": 0,
        "consecutive_losses": 0,
        "entries_today": 0,
        "data_age_ms": 100,
        "spread_bps": Decimal("2"),
        "estimated_slippage_bps": Decimal("4"),
        "has_conflicting_position": False,
        "has_conflicting_order": False,
    }
    base.update(over)
    return AccountState(**base)  # type: ignore[arg-type]


def _intent(**over: object) -> TradeIntent:
    base: dict[str, object] = {
        "decision_id": "5f1e4d3c-2b1a-4098-8765-43210fedcba9",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "entry": Decimal("60000"),
        "stop": Decimal("59400"),
        "invalidation": Decimal("59500"),
        "liquidation": Decimal("30000"),
        "rr_net": Decimal("2.5"),
        "intent_expires_at_ms": 9_999_999_999_999,
        "take_profit_fractions": [Decimal("0.5"), Decimal("0.5")],
        "is_averaging_down": False,
        "widens_stop": False,
        "order_type": "Limit",
    }
    base.update(over)
    return TradeIntent(**base)  # type: ignore[arg-type]


def _config() -> dict[str, object]:
    return {
        "spec": _spec(),
        "policy": RiskPolicy.conservative_v0(),
        "taker_fee_rate": Decimal("0.00055"),
        "estimated_slippage": Decimal("6"),
        "available_liquidity": Decimal("1000"),
        "now_ms": 1_000_000_000_000,
    }


# --------------------------------------------------------------------------
# Aprovação
# --------------------------------------------------------------------------


def test_valid_intent_is_approved_with_computed_quantity() -> None:
    d = evaluate(_intent(), _account(), **_config())
    assert isinstance(d, RiskDecision)
    assert d.approved
    assert d.quantity is not None
    assert d.quantity.value > 0
    assert d.rejections == []


def test_approved_decision_records_policy_hash() -> None:
    """⭐ Rastreabilidade: qual versão da política aprovou este trade."""
    policy = RiskPolicy.conservative_v0()
    d = evaluate(_intent(), _account(), **{**_config(), "policy": policy})
    assert d.policy_hash == policy.policy_hash


def test_approved_decision_carries_risk_breakdown() -> None:
    d = evaluate(_intent(), _account(), **_config())
    assert d.risk_budget is not None
    assert d.risk_per_unit is not None
    assert d.binding_constraint is not None


# --------------------------------------------------------------------------
# A quantidade NUNCA vem da intenção
# --------------------------------------------------------------------------


def test_intent_has_no_quantity_field() -> None:
    """⭐ Garantia estrutural: TradeIntent não tem como carregar quantidade.
    Se tivesse, o modelo poderia influenciar o tamanho."""
    assert not hasattr(_intent(), "quantity")
    assert not hasattr(_intent(), "qty")
    assert not hasattr(_intent(), "size")
    assert not hasattr(_intent(), "leverage")


# --------------------------------------------------------------------------
# Rejeição — validação antes do sizing
# --------------------------------------------------------------------------


def test_rejected_intent_has_no_quantity() -> None:
    d = evaluate(_intent(), _account(open_positions=1), **_config())
    assert not d.approved
    assert d.quantity is None
    assert any(r.code == "MAX_POSITIONS" for r in d.rejections)


def test_sizing_does_not_run_when_validation_fails() -> None:
    """Se a validação rejeita, não há quantidade — o sizing é curto-
    circuitado. Um trade proibido não recebe dimensionamento."""
    d = evaluate(_intent(symbol="ETHUSDT"), _account(), **_config())
    assert not d.approved
    assert d.quantity is None
    assert d.binding_constraint is None


def test_valid_rules_but_unsizable_is_rejected() -> None:
    """Passa nos validadores mas o patrimônio é pequeno demais para
    qualquer quantidade válida → rejeitado pelo sizing."""
    d = evaluate(_intent(), _account(equity=Decimal("10")),
                 **{**_config(), "policy": RiskPolicy.conservative_v0()})
    assert not d.approved
    assert d.quantity is None
    assert any(r.code == "UNSIZABLE" for r in d.rejections)


# --------------------------------------------------------------------------
# Determinismo — a invariante que sustenta o resto
# --------------------------------------------------------------------------


def test_engine_is_deterministic() -> None:
    intent, account, cfg = _intent(), _account(), _config()
    assert evaluate(intent, account, **cfg) == evaluate(intent, account, **cfg)


def test_short_intent_is_sized_correctly() -> None:
    d = evaluate(
        _intent(
            side="SELL",
            entry=Decimal("60000"),
            stop=Decimal("60600"),
            invalidation=Decimal("60500"),
            liquidation=Decimal("90000"),
        ),
        _account(),
        **_config(),
    )
    assert d.approved
    assert d.quantity is not None
    assert d.quantity.value > 0
