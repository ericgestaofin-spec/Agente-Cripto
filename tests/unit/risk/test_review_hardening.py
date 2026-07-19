"""Batch A2 — endurecimento a partir do review externo.

Cobre: contexto Decimal explícito nos cálculos, conservadorismo de taxa
(rebate negativo não reduz risco), sanidade absoluta da política, aperto
da invariante de exposição, e coerência invalidação↔stop.

Ver docs/REVISAO_ACHADOS.md, categoria A/B (parcial).
"""

from __future__ import annotations

from decimal import Decimal, getcontext, localcontext

import pytest

from bybit_agent.domain.instrument import InstrumentSpec
from bybit_agent.domain.money import Price, Quantity
from bybit_agent.risk.policy import RiskPolicy
from bybit_agent.risk.sizing import SizingInputs, compute_size
from bybit_agent.risk.validators import AccountState, TradeContext, validate


def _spec() -> InstrumentSpec:
    return InstrumentSpec.from_bybit(
        {
            "symbol": "BTCUSDT", "status": "Trading",
            "priceFilter": {"tickSize": "0.10", "minPrice": "0.10", "maxPrice": "999999"},
            "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001",
                              "maxOrderQty": "500", "maxMktOrderQty": "100",
                              "minNotionalValue": "5"},
            "leverageFilter": {"minLeverage": "1", "maxLeverage": "100"},
        }
    )


def _sizing(**over: object) -> SizingInputs:
    base: dict[str, object] = {
        "equity": Price("100000"), "entry": Price("60000"), "stop": Price("59400"),
        "risk_fraction": Decimal("0.0025"), "taker_fee_rate": Decimal("0.00055"),
        "estimated_slippage": Price("6"), "max_leverage": Decimal("2"),
        "available_liquidity": Quantity("1000"), "spec": _spec(), "order_type": "Limit",
    }
    base.update(over)
    return SizingInputs(**base)  # type: ignore[arg-type]


# ==========================================================================
# Contexto Decimal explícito — determinismo sob contexto externo hostil
# ==========================================================================


def test_sizing_result_is_invariant_to_external_decimal_context() -> None:
    """⭐ Um contexto Decimal global hostil não pode mudar o resultado.
    Os cálculos rodam sob localcontext próprio, então prec externa baixa
    não corrompe o dimensionamento."""
    inp = _sizing()
    with localcontext():
        getcontext().prec = 6  # hostil
        low = compute_size(inp)
    with localcontext():
        getcontext().prec = 50  # generoso
        high = compute_size(inp)
    assert low.quantity == high.quantity
    assert low.risk_per_unit == high.risk_per_unit


# ==========================================================================
# Conservadorismo de taxa — rebate negativo não reduz o risco
# ==========================================================================


def test_negative_fee_rebate_does_not_reduce_risk() -> None:
    """⭐ Um maker rebate (taxa negativa) NÃO pode inflar a posição
    reduzindo o risco/unidade calculado. Para sizing, taxa efetiva >= 0."""
    zero_fee = compute_size(_sizing(taker_fee_rate=Decimal("0")))
    rebate = compute_size(_sizing(taker_fee_rate=Decimal("-0.0005")))
    assert zero_fee.risk_per_unit is not None
    assert rebate.risk_per_unit is not None
    # com rebate tratado como 0, o risco/unidade é o mesmo do fee zero
    assert rebate.risk_per_unit == zero_fee.risk_per_unit
    assert rebate.quantity == zero_fee.quantity


# ==========================================================================
# Exposição — invariante estrita, sem tolerância de um qtyStep
# ==========================================================================


def test_exposure_never_exceeds_leverage_cap_strictly() -> None:
    """Como a quantidade é arredondada para baixo, o notional final nunca
    excede equity*max_leverage — sem folga de um qtyStep."""
    r = compute_size(
        _sizing(stop=Price("59988"), max_leverage=Decimal("2"),
                taker_fee_rate=Decimal("0"), estimated_slippage=Price("0"))
    )
    assert r.approved
    assert r.quantity is not None
    assert r.quantity.value * Decimal("60000") <= Decimal("100000") * Decimal("2")


# ==========================================================================
# Política — limites absolutos de segurança não configuráveis
# ==========================================================================


def test_policy_rejects_absurd_total_risk() -> None:
    with pytest.raises(ValueError, match="total"):
        RiskPolicy.conservative_v0().replace(
            max_risk_per_trade=Decimal("0.05"), max_total_risk=Decimal("0.50")
        )


def test_policy_rejects_absurd_leverage() -> None:
    with pytest.raises(ValueError, match="alavancagem|leverage"):
        RiskPolicy.conservative_v0().replace(max_leverage=Decimal("1000"))


def test_policy_rejects_absurd_daily_loss() -> None:
    with pytest.raises(ValueError, match="di[áa]ria"):
        RiskPolicy.conservative_v0().replace(
            max_daily_loss=Decimal("1"), max_weekly_loss=Decimal("3")
        )


def test_policy_rejects_non_finite_field() -> None:
    with pytest.raises(ValueError, match="finit"):
        RiskPolicy.conservative_v0().replace(max_spread_bps=Decimal("NaN"))


def test_policy_accepts_reasonable_aggressive_but_bounded() -> None:
    """Valores agressivos mas dentro dos tetos de sanidade passam —
    a política é conservadora por default, não engessada."""
    RiskPolicy.conservative_v0().replace(
        max_risk_per_trade=Decimal("0.02"),
        max_total_risk=Decimal("0.04"),
        max_leverage=Decimal("5"),
    )


# ==========================================================================
# Coerência invalidação ↔ stop ↔ entrada
# ==========================================================================


def _ctx(**over: object) -> TradeContext:
    base: dict[str, object] = {
        "symbol": "BTCUSDT", "side": "BUY", "entry": Decimal("60000"),
        "stop": Decimal("59400"), "invalidation": Decimal("59500"),
        "liquidation": Decimal("30000"), "rr_net": Decimal("2.5"),
        "intent_expires_at_ms": 9_999_999_999_999, "now_ms": 1,
        "take_profit_fractions": [Decimal("1")], "is_averaging_down": False,
        "widens_stop": False,
    }
    base.update(over)
    return TradeContext(**base)  # type: ignore[arg-type]


def _codes(ctx: TradeContext) -> set[str]:
    acc = AccountState(
        equity=Decimal("100000"), daily_pnl=Decimal("0"), weekly_pnl=Decimal("0"),
        open_positions=0, open_orders=0, consecutive_losses=0, entries_today=0,
        data_age_ms=100, spread_bps=Decimal("2"), estimated_slippage_bps=Decimal("4"),
        has_conflicting_position=False, has_conflicting_order=False,
    )
    return {r.code for r in validate(acc, ctx, RiskPolicy.conservative_v0()).rejections}


def test_long_invalidation_above_entry_is_incoherent() -> None:
    """⭐ LONG: invalidação ACIMA da entrada é incoerente — a tese não
    pode ser invalidada por um preço mais alto num long."""
    codes = _codes(_ctx(side="BUY", entry=Decimal("60000"),
                        invalidation=Decimal("70000"), stop=Decimal("59400")))
    assert "INVALIDATION_INCOHERENT" in codes


def test_short_invalidation_below_entry_is_incoherent() -> None:
    codes = _codes(_ctx(side="SELL", entry=Decimal("60000"),
                        invalidation=Decimal("50000"), stop=Decimal("60600"),
                        liquidation=Decimal("90000")))
    assert "INVALIDATION_INCOHERENT" in codes


def test_long_stop_above_invalidation_is_incoherent() -> None:
    """LONG: o stop deve estar em/abaixo da invalidação (proteger quando a
    tese quebra), não entre a invalidação e a entrada."""
    codes = _codes(_ctx(side="BUY", entry=Decimal("60000"),
                        invalidation=Decimal("59500"), stop=Decimal("59800")))
    assert "INVALIDATION_INCOHERENT" in codes


def test_coherent_long_passes() -> None:
    codes = _codes(_ctx(side="BUY", entry=Decimal("60000"),
                        invalidation=Decimal("59500"), stop=Decimal("59400")))
    assert "INVALIDATION_INCOHERENT" not in codes


def test_coherent_short_passes() -> None:
    codes = _codes(_ctx(side="SELL", entry=Decimal("60000"),
                        invalidation=Decimal("60500"), stop=Decimal("60600"),
                        liquidation=Decimal("90000")))
    assert "INVALIDATION_INCOHERENT" not in codes


def test_long_stop_exactly_at_invalidation_is_coherent() -> None:
    """Fronteira: stop == invalidação é aceito."""
    codes = _codes(_ctx(side="BUY", entry=Decimal("60000"),
                        invalidation=Decimal("59500"), stop=Decimal("59500")))
    assert "INVALIDATION_INCOHERENT" not in codes


def test_long_invalidation_exactly_at_entry_is_incoherent() -> None:
    """Fronteira: invalidação == entrada (LONG) é incoerente — precisa ser
    estritamente abaixo da entrada."""
    codes = _codes(_ctx(side="BUY", entry=Decimal("60000"),
                        invalidation=Decimal("60000"), stop=Decimal("59400")))
    assert "INVALIDATION_INCOHERENT" in codes


def test_short_invalidation_exactly_at_entry_is_incoherent() -> None:
    codes = _codes(_ctx(side="SELL", entry=Decimal("60000"),
                        invalidation=Decimal("60000"), stop=Decimal("60600"),
                        liquidation=Decimal("90000")))
    assert "INVALIDATION_INCOHERENT" in codes


def test_short_stop_exactly_at_invalidation_is_coherent() -> None:
    """Fronteira SHORT: invalidação == stop é aceito (invalidação <= stop)."""
    codes = _codes(_ctx(side="SELL", entry=Decimal("60000"),
                        invalidation=Decimal("60600"), stop=Decimal("60600"),
                        liquidation=Decimal("90000")))
    assert "INVALIDATION_INCOHERENT" not in codes


# ==========================================================================
# Política — cobertura dos tetos absolutos restantes
# ==========================================================================


def test_policy_rejects_absurd_weekly_loss() -> None:
    with pytest.raises(ValueError, match="semanal"):
        RiskPolicy.conservative_v0().replace(
            max_daily_loss=Decimal("0.05"), max_weekly_loss=Decimal("0.50")
        )


def test_policy_rejects_absurd_spread() -> None:
    with pytest.raises(ValueError, match="spread"):
        RiskPolicy.conservative_v0().replace(max_spread_bps=Decimal("500"))


def test_policy_rejects_absurd_slippage() -> None:
    with pytest.raises(ValueError, match="slippage"):
        RiskPolicy.conservative_v0().replace(max_slippage_bps=Decimal("500"))


def test_policy_rejects_negative_spread() -> None:
    with pytest.raises(ValueError, match="spread"):
        RiskPolicy.conservative_v0().replace(max_spread_bps=Decimal("-1"))


def test_policy_rejects_negative_slippage() -> None:
    with pytest.raises(ValueError, match="slippage"):
        RiskPolicy.conservative_v0().replace(max_slippage_bps=Decimal("-1"))


# -- fronteiras exatas dos tetos: no limite aceita, acima rejeita ----------


def test_policy_total_risk_exactly_at_hard_max_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(
        max_risk_per_trade=Decimal("0.05"), max_total_risk=Decimal("0.10")
    )


def test_policy_daily_exactly_at_hard_max_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(
        max_daily_loss=Decimal("0.10"), max_weekly_loss=Decimal("0.10")
    )


def test_policy_weekly_exactly_at_hard_max_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(
        max_daily_loss=Decimal("0.10"), max_weekly_loss=Decimal("0.30")
    )


def test_policy_leverage_exactly_at_hard_max_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(max_leverage=Decimal("20"))


def test_policy_spread_exactly_at_hard_max_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(max_spread_bps=Decimal("100"))


def test_policy_spread_zero_is_allowed() -> None:
    """spread == 0 é um limite válido (rejeitar tudo); só negativo é inválido."""
    RiskPolicy.conservative_v0().replace(max_spread_bps=Decimal("0"))


def test_policy_slippage_exactly_at_hard_max_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(max_slippage_bps=Decimal("100"))


def test_policy_slippage_zero_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(max_slippage_bps=Decimal("0"))


def test_policy_daily_one_step_above_hard_max_rejects() -> None:
    with pytest.raises(ValueError, match="di[áa]ria"):
        RiskPolicy.conservative_v0().replace(
            max_daily_loss=Decimal("0.11"), max_weekly_loss=Decimal("0.11")
        )


# ==========================================================================
# Helpers de validação — branches de tipo (dados malformados)
# ==========================================================================


def test_validation_helpers_reject_non_decimal() -> None:
    from bybit_agent.risk._validation import (
        require_finite,
        require_non_negative_int,
        validate_take_profit_fractions,
    )

    with pytest.raises(TypeError):
        require_finite(1.5, name="x")  # type: ignore[arg-type]  # float
    with pytest.raises(TypeError):
        require_non_negative_int(True, name="x")  # bool não é int válido
    with pytest.raises(TypeError):
        require_non_negative_int(Decimal("1"), name="x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        validate_take_profit_fractions([1.5])  # type: ignore[list-item]  # float


def test_validate_tp_fractions_rejects_non_finite() -> None:
    from bybit_agent.risk._validation import validate_take_profit_fractions

    with pytest.raises(ValueError, match="finit"):
        validate_take_profit_fractions([Decimal("NaN")])


def test_require_non_empty_symbol_rejects_blank() -> None:
    from bybit_agent.risk._validation import require_non_empty_symbol

    with pytest.raises(ValueError):
        require_non_empty_symbol("   ")


def test_require_finite_non_negative_rejects_negative() -> None:
    from bybit_agent.risk._validation import require_finite_non_negative

    with pytest.raises(ValueError, match=">= 0"):
        require_finite_non_negative(Decimal("-0.01"), name="x")


def test_require_non_negative_int_rejects_negative() -> None:
    from bybit_agent.risk._validation import require_non_negative_int

    with pytest.raises(ValueError, match=">= 0"):
        require_non_negative_int(-1, name="x")
