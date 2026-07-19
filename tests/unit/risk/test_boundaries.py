"""Sprint 4 — testes de fronteira (limites exatos).

Mutation testing revelou que a suite verificava os dois lados de cada
limite, mas nunca o valor EXATAMENTE no limite. É ali que moram os bugs
de off-by-one — e num motor de risco, um off-by-one num limite é a
diferença entre aceitar e recusar uma operação.

Cada teste aqui pina uma decisão de política:
  - Limites de MAGNITUDE (risco, spread, slippage, data_age, RR): o valor
    da política é o MÁXIMO/MÍNIMO PERMITIDO. Exatamente no limite → aceita;
    além → rejeita.
  - Limites de CONTAGEM (posições, entradas, perdas): o valor é o teto
    atingível. Exatamente no teto → rejeita (você está cheio).

Estes testes também são o que mantém o mutation score do `risk/` alto.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bybit_agent.domain.instrument import InstrumentSpec
from bybit_agent.domain.money import Price, Quantity
from bybit_agent.risk.policy import RiskPolicy
from bybit_agent.risk.sizing import SizingInputs, compute_size
from bybit_agent.risk.validators import AccountState, TradeContext, validate


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


def _ctx(**over: object) -> TradeContext:
    base: dict[str, object] = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "entry": Decimal("60000"),
        "stop": Decimal("59400"),
        "invalidation": Decimal("59500"),
        "liquidation": Decimal("30000"),
        "rr_net": Decimal("2.5"),
        "intent_expires_at_ms": 9_999_999_999_999,
        "now_ms": 1_000_000_000_000,
        "take_profit_fractions": [Decimal("0.5"), Decimal("0.5")],
        "is_averaging_down": False,
        "widens_stop": False,
    }
    base.update(over)
    return TradeContext(**base)  # type: ignore[arg-type]


def _codes(acc: AccountState, ctx: TradeContext) -> set[str]:
    return {r.code for r in validate(acc, ctx, RiskPolicy.conservative_v0()).rejections}


P = RiskPolicy.conservative_v0()


# ==========================================================================
# Validadores — limites de MAGNITUDE: no limite aceita, além rejeita
# ==========================================================================


def test_daily_loss_exactly_at_limit_rejects() -> None:
    """PnL exatamente no limite diário JÁ rejeita (<=)."""
    limit = -(Decimal("100000") * P.max_daily_loss)  # -1000
    assert "DAILY_LOSS_LIMIT" in _codes(_account(daily_pnl=limit), _ctx())


def test_daily_loss_one_cent_above_limit_passes() -> None:
    limit = -(Decimal("100000") * P.max_daily_loss)
    assert "DAILY_LOSS_LIMIT" not in _codes(
        _account(daily_pnl=limit + Decimal("0.01")), _ctx()
    )


def test_weekly_loss_exactly_at_limit_rejects() -> None:
    limit = -(Decimal("100000") * P.max_weekly_loss)  # -3000
    assert "WEEKLY_LOSS_LIMIT" in _codes(_account(weekly_pnl=limit), _ctx())


def test_spread_exactly_at_max_is_allowed() -> None:
    """Spread == máximo é aceito; só ACIMA rejeita (>)."""
    assert "SPREAD_TOO_WIDE" not in _codes(
        _account(spread_bps=P.max_spread_bps), _ctx()
    )


def test_spread_one_bp_above_max_rejects() -> None:
    assert "SPREAD_TOO_WIDE" in _codes(
        _account(spread_bps=P.max_spread_bps + Decimal("0.01")), _ctx()
    )


def test_slippage_exactly_at_max_is_allowed() -> None:
    assert "SLIPPAGE_TOO_HIGH" not in _codes(
        _account(estimated_slippage_bps=P.max_slippage_bps), _ctx()
    )


def test_slippage_above_max_rejects() -> None:
    assert "SLIPPAGE_TOO_HIGH" in _codes(
        _account(estimated_slippage_bps=P.max_slippage_bps + Decimal("0.01")), _ctx()
    )


def test_data_age_exactly_at_max_is_allowed() -> None:
    """⭐ Bug corrigido: idade == máximo é aceita (>), não rejeitada (>=).
    A mensagem já dizia '> máximo' — o código discordava dela."""
    assert "DATA_STALE" not in _codes(_account(data_age_ms=P.max_data_age_ms), _ctx())


def test_data_age_one_ms_above_max_rejects() -> None:
    assert "DATA_STALE" in _codes(_account(data_age_ms=P.max_data_age_ms + 1), _ctx())


def test_rr_exactly_at_minimum_is_allowed() -> None:
    """RR == mínimo é aceito; só ABAIXO rejeita (<). min_rr_net=2.0."""
    assert "RR_TOO_LOW" not in _codes(_account(), _ctx(rr_net=P.min_rr_net))


def test_rr_just_below_minimum_rejects() -> None:
    assert "RR_TOO_LOW" in _codes(
        _account(), _ctx(rr_net=P.min_rr_net - Decimal("0.01"))
    )


# ==========================================================================
# Validadores — limites de CONTAGEM: no teto rejeita
# ==========================================================================


def test_positions_exactly_at_max_rejects() -> None:
    assert "MAX_POSITIONS" in _codes(
        _account(open_positions=P.max_concurrent_positions), _ctx()
    )


def test_entries_exactly_at_max_rejects() -> None:
    assert "MAX_DAILY_ENTRIES" in _codes(
        _account(entries_today=P.max_daily_entries), _ctx()
    )


def test_consecutive_losses_exactly_at_max_rejects() -> None:
    assert "COOLDOWN" in _codes(
        _account(consecutive_losses=P.max_consecutive_losses), _ctx()
    )


# ==========================================================================
# Stop — fronteiras de igualdade (stop == entrada, stop == liquidação)
# ==========================================================================


def test_long_stop_exactly_at_entry_rejects() -> None:
    """Stop igual à entrada é incoerente — risco zero, não é operação."""
    assert "STOP_WRONG_SIDE" in _codes(
        _account(), _ctx(side="BUY", entry=Decimal("60000"), stop=Decimal("60000"))
    )


def test_short_stop_exactly_at_entry_rejects() -> None:
    assert "STOP_WRONG_SIDE" in _codes(
        _account(),
        _ctx(side="SELL", entry=Decimal("60000"), stop=Decimal("60000"),
             invalidation=Decimal("60500"), liquidation=Decimal("90000")),
    )


def test_long_valid_stop_below_entry_does_not_trigger_wrong_side() -> None:
    """Contraparte: LONG com stop abaixo da entrada é válido — garante que
    o operador de lado (side == 'BUY') não foi trocado."""
    assert "STOP_WRONG_SIDE" not in _codes(
        _account(), _ctx(side="BUY", entry=Decimal("60000"), stop=Decimal("59000"))
    )


def test_short_valid_stop_above_entry_does_not_trigger_wrong_side() -> None:
    assert "STOP_WRONG_SIDE" not in _codes(
        _account(),
        _ctx(side="SELL", entry=Decimal("60000"), stop=Decimal("61000"),
             invalidation=Decimal("60500"), liquidation=Decimal("90000")),
    )


def test_long_stop_exactly_at_liquidation_rejects() -> None:
    assert "STOP_BEYOND_LIQUIDATION" in _codes(
        _account(),
        _ctx(side="BUY", entry=Decimal("60000"), stop=Decimal("30000"),
             liquidation=Decimal("30000"), invalidation=Decimal("30500")),
    )


def test_short_stop_exactly_at_liquidation_rejects() -> None:
    assert "STOP_BEYOND_LIQUIDATION" in _codes(
        _account(),
        _ctx(side="SELL", entry=Decimal("60000"), stop=Decimal("90000"),
             liquidation=Decimal("90000"), invalidation=Decimal("60500")),
    )


def test_long_stop_just_above_liquidation_is_allowed() -> None:
    assert "STOP_BEYOND_LIQUIDATION" not in _codes(
        _account(),
        _ctx(side="BUY", entry=Decimal("60000"), stop=Decimal("30001"),
             liquidation=Decimal("30000"), invalidation=Decimal("30500")),
    )


# ==========================================================================
# TP fractions — soma exatamente 1 é permitida
# ==========================================================================


def test_intent_expiring_exactly_now_rejects() -> None:
    """Intenção que expira EXATAMENTE no instante da avaliação já expirou
    (<=). Um sinal na fronteira temporal não pode ser tratado como válido."""
    assert "INTENT_EXPIRED" in _codes(
        _account(), _ctx(intent_expires_at_ms=1_000_000_000_000,
                          now_ms=1_000_000_000_000)
    )


def test_intent_expiring_one_ms_ahead_is_valid() -> None:
    assert "INTENT_EXPIRED" not in _codes(
        _account(), _ctx(intent_expires_at_ms=1_000_000_000_001,
                          now_ms=1_000_000_000_000)
    )


def test_tp_fractions_summing_exactly_one_is_allowed() -> None:
    """Soma == 1 (fechar 100% da posição) é válido; só ACIMA de 1 rejeita."""
    assert "TP_FRACTIONS_EXCEED_ONE" not in _codes(
        _account(), _ctx(take_profit_fractions=[Decimal("0.5"), Decimal("0.5")])
    )


def test_tp_fractions_above_one_rejects() -> None:
    assert "TP_FRACTIONS_EXCEED_ONE" in _codes(
        _account(), _ctx(take_profit_fractions=[Decimal("0.5"), Decimal("0.51")])
    )


# ==========================================================================
# Projected daily breach — fronteira
# ==========================================================================


def test_projected_exactly_at_limit_is_allowed() -> None:
    """Se o pior caso do trade leva EXATAMENTE ao limite diário, ainda é
    permitido; só ULTRAPASSAR rejeita (projected < limit)."""
    # projected = daily_pnl - equity*max_risk_per_trade
    # limit = -(equity*max_daily_loss)
    equity = Decimal("100000")
    risk_budget = equity * P.max_risk_per_trade  # 250
    limit = equity * P.max_daily_loss  # 1000
    # daily_pnl tal que projected == -limit exatamente
    daily = -(limit) + risk_budget  # -750; projected = -750-250 = -1000 == limit
    assert "PROJECTED_DAILY_BREACH" not in _codes(
        _account(daily_pnl=daily), _ctx()
    )


def test_projected_one_cent_past_limit_rejects() -> None:
    equity = Decimal("100000")
    risk_budget = equity * P.max_risk_per_trade
    limit = equity * P.max_daily_loss
    daily = -(limit) + risk_budget - Decimal("0.01")
    assert "PROJECTED_DAILY_BREACH" in _codes(_account(daily_pnl=daily), _ctx())


# ==========================================================================
# Política — fronteiras dos validadores de sanidade
# ==========================================================================


def test_risk_exactly_at_sanity_ceiling_is_allowed() -> None:
    """max_risk == teto de sanidade (5%) é aceito; só ACIMA rejeita."""
    RiskPolicy.conservative_v0().replace(
        max_risk_per_trade=Decimal("0.05"), max_total_risk=Decimal("0.05")
    )  # não levanta


def test_risk_just_above_sanity_ceiling_rejects() -> None:
    with pytest.raises(ValueError, match="implausível"):
        RiskPolicy.conservative_v0().replace(
            max_risk_per_trade=Decimal("0.0501"), max_total_risk=Decimal("0.0501")
        )


def test_risk_exactly_equal_to_total_is_allowed() -> None:
    """max_risk == max_total é aceito; só EXCEDER rejeita."""
    RiskPolicy.conservative_v0().replace(
        max_risk_per_trade=Decimal("0.005"), max_total_risk=Decimal("0.005")
    )


def test_daily_exactly_equal_to_weekly_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(
        max_daily_loss=Decimal("0.03"), max_weekly_loss=Decimal("0.03")
    )


def test_leverage_exactly_one_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(max_leverage=Decimal("1"))


def test_min_rr_exactly_one_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(min_rr_net=Decimal("1"))


def test_concurrent_positions_exactly_one_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(max_concurrent_positions=1)


# ==========================================================================
# Sizing — fronteiras de rejeição
# ==========================================================================


def _sizing(**over: object) -> SizingInputs:
    base: dict[str, object] = {
        "equity": Price("100000"),
        "entry": Price("60000"),
        "stop": Price("59400"),
        "risk_fraction": Decimal("0.0025"),
        "taker_fee_rate": Decimal("0"),
        "estimated_slippage": Price("0"),
        "max_leverage": Decimal("2"),
        "available_liquidity": Quantity("1000"),
        "spec": _spec(),
        "order_type": "Limit",
    }
    base.update(over)
    return SizingInputs(**base)  # type: ignore[arg-type]


def test_zero_risk_fraction_rejects_at_budget_check() -> None:
    """Orçamento exatamente zero rejeita NO check de orçamento, com o
    motivo certo — não cai adiante e rejeita por 'quantidade mínima'."""
    r = compute_size(_sizing(risk_fraction=Decimal("0")))
    assert not r.approved
    assert "orçamento" in r.rejection_reason.lower()


def test_quantity_exactly_at_min_order_qty_is_approved() -> None:
    """Quantidade que cai EXATAMENTE no mínimo da corretora é aceita;
    só ABAIXO rejeita (rounded < min_order_qty)."""
    # Ajusta o orçamento para render exatamente 0.001 a 60000 com dist 600:
    # qty = budget/600 = 0.001 -> budget = 0.6 -> equity*rf = 0.6
    # equity=240, rf=0.0025 -> budget=0.6 -> qty=0.001
    r = compute_size(
        _sizing(equity=Price("240"), risk_fraction=Decimal("0.0025"),
                stop=Price("59400"))
    )
    assert r.approved
    assert r.quantity == Quantity("0.001")


def test_leverage_cap_produces_exact_quantity() -> None:
    """Quando a alavancagem é o binding, a quantidade é exatamente
    equity*max_leverage/entry (arredondada). Fixa o cálculo do notional
    máximo — um sinal trocado (/ no lugar de *) seria pego aqui."""
    # equity 100k, leverage 2 -> notional máx 200k; a 60000 -> 3.333...
    # stop apertado + sem taxas isola a alavancagem como binding
    r = compute_size(
        _sizing(
            equity=Price("100000"),
            entry=Price("60000"),
            stop=Price("59988"),  # dist 12 -> qty_by_risk enorme
            risk_fraction=Decimal("0.0025"),
            max_leverage=Decimal("2"),
        )
    )
    assert r.approved
    assert r.binding_constraint == "leverage"
    assert r.quantity is not None
    # 200000/60000 = 3.3333... arredondado para baixo ao step 0.001
    assert r.quantity == Quantity("3.333")


def test_notional_exactly_at_min_is_approved() -> None:
    """Notional exatamente no mínimo (5 USDT) é aceito; só ABAIXO rejeita."""
    # qty=0.001 a entry=5000 -> notional=5.0 == min_notional
    spec = _spec()
    # budget para qty 0.001 com dist tal que qty=0.001
    r = compute_size(
        _sizing(
            entry=Price("5000"), stop=Price("4900"),  # dist 100
            equity=Price("40"), risk_fraction=Decimal("0.0025"),  # budget 0.1 -> qty 0.001
            spec=spec,
        )
    )
    assert r.approved
    assert r.quantity is not None
    assert r.quantity.value * Decimal("5000") >= spec.min_notional
