"""Validação de fronteira em runtime — resposta ao review externo.

Achados confirmados (ver docs/REVISAO_ACHADOS.md, categoria A): os objetos
de fronteira aceitavam lixo silenciosamente ou levantavam exceção não
tratada. `Literal` e `frozen=True` são garantias de type-checker e de
rebind — não validam valores em runtime nem impedem mutação de listas.

Princípio: dado inválido falha ALTO na construção (fail-fast no boundary),
e o `evaluate` nunca levanta para o gateway — converte erro de input em
uma rejeição INVALID_INPUT.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bybit_agent.domain.instrument import InstrumentSpec
from bybit_agent.domain.money import Price, Quantity
from bybit_agent.risk.engine import TradeIntent, evaluate
from bybit_agent.risk.policy import RiskPolicy
from bybit_agent.risk.reward import TakeProfitLevel
from bybit_agent.risk.sizing import SizingInputs, compute_size
from bybit_agent.risk.validators import AccountState, TradeContext


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


def _intent(**over: object) -> TradeIntent:
    base: dict[str, object] = {
        "decision_id": "x", "symbol": "BTCUSDT", "side": "BUY",
        "entry": Decimal("60000"), "stop": Decimal("59400"),
        "invalidation": Decimal("59500"), "liquidation": Decimal("30000"),
        "intent_expires_at_ms": 9_999_999_999_999,
        "take_profit_levels": (TakeProfitLevel(Decimal("61600"), Decimal("1")),),
        "is_averaging_down": False, "widens_stop": False,
    }
    base.update(over)
    return TradeIntent(**base)  # type: ignore[arg-type]


def _account(**over: object) -> AccountState:
    base: dict[str, object] = {
        "equity": Decimal("100000"), "daily_pnl": Decimal("0"),
        "weekly_pnl": Decimal("0"), "open_positions": 0, "open_orders": 0,
        "consecutive_losses": 0, "entries_today": 0, "data_age_ms": 100,
        "spread_bps": Decimal("2"), "estimated_slippage_bps": Decimal("4"),
        "has_conflicting_position": False, "has_conflicting_order": False,
    }
    base.update(over)
    return AccountState(**base)  # type: ignore[arg-type]


def _ctx(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "symbol": "BTCUSDT", "side": "BUY", "entry": Decimal("60000"),
        "stop": Decimal("59400"), "invalidation": Decimal("59500"),
        "liquidation": Decimal("30000"), "rr_net": Decimal("2.5"),
        "intent_expires_at_ms": 9_999_999_999_999, "now_ms": 1,
        "take_profit_fractions": [Decimal("1")], "is_averaging_down": False,
        "widens_stop": False,
    }
    base.update(over)
    return base


# ==========================================================================
# 5a — lado inválido não pode construir (burlava validação de stop)
# ==========================================================================


def test_invalid_side_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="side"):
        TradeContext(**_ctx(side="WAIT"))  # type: ignore[arg-type]


def test_invalid_side_in_intent_is_rejected() -> None:
    with pytest.raises(ValueError, match="side"):
        _intent(side="HOLD")


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_valid_sides_are_accepted(side: str) -> None:
    inval = Decimal("59500") if side == "BUY" else Decimal("60500")
    liq = Decimal("30000") if side == "BUY" else Decimal("90000")
    stop = Decimal("59400") if side == "BUY" else Decimal("60600")
    _intent(side=side, stop=stop, invalidation=inval, liquidation=liq)


# ==========================================================================
# 5c — NaN / infinito não podem entrar em campo Decimal
# ==========================================================================


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_price_field_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="finit"):
        _intent(entry=Decimal(bad))


def test_empty_take_profit_levels_is_rejected() -> None:
    """Sem alvos não há plano de saída nem RR calculável — o modelo não
    pode mais aprovar declarando um RR alto com TP vazio."""
    with pytest.raises(ValueError, match="take-profit|RR"):
        _intent(take_profit_levels=())


def test_non_finite_equity_is_rejected() -> None:
    with pytest.raises(ValueError, match="finit"):
        _account(equity=Decimal("NaN"))


def test_non_finite_pnl_is_rejected() -> None:
    with pytest.raises(ValueError, match="finit"):
        _account(daily_pnl=Decimal("NaN"))


# ==========================================================================
# 5b — entry <= 0 não pode construir; e compute_size rejeita sem levantar
# ==========================================================================


def test_zero_entry_intent_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="entry|positiv"):
        _intent(entry=Decimal("0"))


def test_compute_size_with_zero_entry_rejects_not_raises() -> None:
    """Contrato: compute_size nunca levanta — retorna rejeição."""
    inp = SizingInputs(
        equity=Price("100000"), entry=Price("0"), stop=Price("1"),
        risk_fraction=Decimal("0.0025"), taker_fee_rate=Decimal("0"),
        estimated_slippage=Price("0"), max_leverage=Decimal("2"),
        available_liquidity=Quantity("1000"), spec=_spec(),
    )
    r = compute_size(inp)  # não levanta
    assert not r.approved


def test_negative_equity_is_rejected() -> None:
    """Patrimônio <= 0 é condição de halt, não input de sizing."""
    with pytest.raises(ValueError):
        _account(equity=Decimal("0"))


# ==========================================================================
# 13 — take_profit_levels são TakeProfitLevel imutáveis e auto-validados
# ==========================================================================


def test_take_profit_levels_is_immutable_tuple() -> None:
    intent = _intent()
    assert isinstance(intent.take_profit_levels, tuple)
    with pytest.raises(AttributeError):
        intent.take_profit_levels.append(  # type: ignore[attr-defined]
            TakeProfitLevel(Decimal("62000"), Decimal("1"))
        )


def test_take_profit_level_fraction_above_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="fração|fraction"):
        TakeProfitLevel(Decimal("61600"), Decimal("1.5"))


def test_take_profit_level_zero_fraction_is_rejected() -> None:
    with pytest.raises(ValueError, match="fração|fraction"):
        TakeProfitLevel(Decimal("61600"), Decimal("0"))


def test_take_profit_level_non_positive_price_is_rejected() -> None:
    with pytest.raises(ValueError, match="preço|price|positiv"):
        TakeProfitLevel(Decimal("0"), Decimal("1"))


# ==========================================================================
# 12 — InstrumentSpec valida sanidade dos campos
# ==========================================================================


def test_spec_rejects_non_finite_tick_size() -> None:
    payload = {
        "symbol": "BTCUSDT", "status": "Trading",
        "priceFilter": {"tickSize": "NaN", "minPrice": "0.10", "maxPrice": "999999"},
        "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001",
                          "maxOrderQty": "500", "maxMktOrderQty": "100",
                          "minNotionalValue": "5"},
        "leverageFilter": {"minLeverage": "1", "maxLeverage": "100"},
    }
    with pytest.raises(ValueError, match="finit|tick"):
        InstrumentSpec.from_bybit(payload)


def test_spec_rejects_negative_market_qty() -> None:
    payload = {
        "symbol": "BTCUSDT", "status": "Trading",
        "priceFilter": {"tickSize": "0.10", "minPrice": "0.10", "maxPrice": "999999"},
        "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001",
                          "maxOrderQty": "500", "maxMktOrderQty": "-1",
                          "minNotionalValue": "5"},
        "leverageFilter": {"minLeverage": "1", "maxLeverage": "100"},
    }
    with pytest.raises(ValueError, match="positiv|maxMkt|>="):
        InstrumentSpec.from_bybit(payload)


def test_spec_rejects_max_below_min_qty() -> None:
    payload = {
        "symbol": "BTCUSDT", "status": "Trading",
        "priceFilter": {"tickSize": "0.10", "minPrice": "0.10", "maxPrice": "999999"},
        "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "10",
                          "maxOrderQty": "5", "maxMktOrderQty": "100",
                          "minNotionalValue": "5"},
        "leverageFilter": {"minLeverage": "1", "maxLeverage": "100"},
    }
    with pytest.raises(ValueError, match="minOrderQty|maxOrderQty"):
        InstrumentSpec.from_bybit(payload)


# ==========================================================================
# evaluate — parâmetros injetados inválidos viram INVALID_INPUT, não exceção
# ==========================================================================


def test_evaluate_with_negative_fee_returns_invalid_input() -> None:
    d = evaluate(
        _intent(), _account(), spec=_spec(), policy=RiskPolicy.conservative_v0(),
        taker_fee_rate=Decimal("-0.5"), estimated_slippage=Decimal("6"),
        available_liquidity=Decimal("1000"), now_ms=1,
    )
    assert not d.approved
    assert any(r.code == "INVALID_INPUT" for r in d.rejections)


def test_evaluate_with_non_finite_slippage_returns_invalid_input() -> None:
    d = evaluate(
        _intent(), _account(), spec=_spec(), policy=RiskPolicy.conservative_v0(),
        taker_fee_rate=Decimal("0.00055"), estimated_slippage=Decimal("NaN"),
        available_liquidity=Decimal("1000"), now_ms=1,
    )
    assert not d.approved
    assert any(r.code == "INVALID_INPUT" for r in d.rejections)


def test_evaluate_never_raises_on_bad_injected_params() -> None:
    """O engine nunca propaga exceção para o gateway."""
    for bad in (Decimal("-1"), Decimal("NaN")):
        d = evaluate(
            _intent(), _account(), spec=_spec(), policy=RiskPolicy.conservative_v0(),
            taker_fee_rate=Decimal("0"), estimated_slippage=Decimal("0"),
            available_liquidity=bad, now_ms=1,
        )
        assert not d.approved
