"""Sprint 4c — validadores de risco.

Cada validador é uma regra de rejeição da spec (§ REGRAS INVIOLÁVEIS e
§ Motor determinístico de risco). Testados isolados e depois compostos.

Princípio: TODOS os validadores rodam mesmo quando o primeiro falha —
o relatório de rejeição é completo, não para no primeiro erro. Isso dá
ao operador (e ao event log) a lista inteira de motivos.
"""

from __future__ import annotations

from decimal import Decimal

from bybit_agent.risk.policy import RiskPolicy
from bybit_agent.risk.validators import (
    AccountState,
    TradeContext,
    ValidationOutcome,
    validate,
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


def _codes(outcome: ValidationOutcome) -> set[str]:
    return {r.code for r in outcome.rejections}


# --------------------------------------------------------------------------
# Caminho feliz
# --------------------------------------------------------------------------


def test_valid_trade_passes_all_validators() -> None:
    out = validate(_account(), _ctx(), RiskPolicy.conservative_v0())
    assert out.approved
    assert out.rejections == []


# --------------------------------------------------------------------------
# Limites de perda
# --------------------------------------------------------------------------


def test_reject_when_daily_loss_limit_reached() -> None:
    out = validate(
        _account(daily_pnl=Decimal("-1000")),  # -1% de 100k
        _ctx(),
        RiskPolicy.conservative_v0(),
    )
    assert not out.approved
    assert "DAILY_LOSS_LIMIT" in _codes(out)


def test_reject_when_weekly_loss_limit_reached() -> None:
    out = validate(
        _account(weekly_pnl=Decimal("-3000")),  # -3%
        _ctx(),
        RiskPolicy.conservative_v0(),
    )
    assert "WEEKLY_LOSS_LIMIT" in _codes(out)


def test_reject_when_projected_loss_would_breach_daily_limit() -> None:
    """⭐ Preventivo: se a perda projetada deste trade estouraria o limite
    diário, rejeita ANTES de enviar, não depois."""
    # já perdeu 0,9%; um trade arriscando 0,25% levaria a 1,15% > 1%
    out = validate(
        _account(daily_pnl=Decimal("-900")),
        _ctx(),
        RiskPolicy.conservative_v0(),
    )
    assert "PROJECTED_DAILY_BREACH" in _codes(out)


# --------------------------------------------------------------------------
# Exposição e frequência
# --------------------------------------------------------------------------


def test_reject_when_max_positions_reached() -> None:
    out = validate(_account(open_positions=1), _ctx(), RiskPolicy.conservative_v0())
    assert "MAX_POSITIONS" in _codes(out)


def test_reject_when_daily_entry_count_exceeded() -> None:
    out = validate(_account(entries_today=3), _ctx(), RiskPolicy.conservative_v0())
    assert "MAX_DAILY_ENTRIES" in _codes(out)


def test_reject_when_consecutive_losses_trigger_cooldown() -> None:
    out = validate(
        _account(consecutive_losses=2), _ctx(), RiskPolicy.conservative_v0()
    )
    assert "COOLDOWN" in _codes(out)


# --------------------------------------------------------------------------
# Símbolo, dados, mercado
# --------------------------------------------------------------------------


def test_reject_when_symbol_not_in_allowlist() -> None:
    out = validate(_account(), _ctx(symbol="ETHUSDT"), RiskPolicy.conservative_v0())
    assert "SYMBOL_NOT_ALLOWED" in _codes(out)


def test_reject_when_data_is_stale() -> None:
    out = validate(
        _account(data_age_ms=6000), _ctx(), RiskPolicy.conservative_v0()
    )
    assert "DATA_STALE" in _codes(out)


def test_reject_when_spread_above_limit() -> None:
    out = validate(
        _account(spread_bps=Decimal("10")), _ctx(), RiskPolicy.conservative_v0()
    )
    assert "SPREAD_TOO_WIDE" in _codes(out)


def test_reject_when_slippage_above_limit() -> None:
    out = validate(
        _account(estimated_slippage_bps=Decimal("20")),
        _ctx(),
        RiskPolicy.conservative_v0(),
    )
    assert "SLIPPAGE_TOO_HIGH" in _codes(out)


# --------------------------------------------------------------------------
# Qualidade do trade
# --------------------------------------------------------------------------


def test_reject_when_rr_net_below_minimum() -> None:
    out = validate(
        _account(), _ctx(rr_net=Decimal("1.5")), RiskPolicy.conservative_v0()
    )
    assert "RR_TOO_LOW" in _codes(out)


def test_reject_when_stop_on_wrong_side_for_long() -> None:
    """LONG com stop acima da entrada é incoerente."""
    out = validate(
        _account(),
        _ctx(side="BUY", entry=Decimal("60000"), stop=Decimal("60500")),
        RiskPolicy.conservative_v0(),
    )
    assert "STOP_WRONG_SIDE" in _codes(out)


def test_reject_when_stop_on_wrong_side_for_short() -> None:
    out = validate(
        _account(),
        _ctx(
            side="SELL",
            entry=Decimal("60000"),
            stop=Decimal("59500"),
            invalidation=Decimal("60500"),
            liquidation=Decimal("90000"),
        ),
        RiskPolicy.conservative_v0(),
    )
    assert "STOP_WRONG_SIDE" in _codes(out)


def test_reject_when_stop_beyond_liquidation() -> None:
    """⭐ Um stop além do preço de liquidação é ficção — a posição é
    liquidada antes de o stop disparar."""
    out = validate(
        _account(),
        _ctx(side="BUY", entry=Decimal("60000"), stop=Decimal("29000"),
             liquidation=Decimal("30000"), invalidation=Decimal("29500")),
        RiskPolicy.conservative_v0(),
    )
    assert "STOP_BEYOND_LIQUIDATION" in _codes(out)


def test_reject_when_short_stop_beyond_liquidation() -> None:
    """SHORT: stop acima do preço de liquidação é ficção."""
    out = validate(
        _account(),
        _ctx(side="SELL", entry=Decimal("60000"), stop=Decimal("91000"),
             liquidation=Decimal("90000"), invalidation=Decimal("60500")),
        RiskPolicy.conservative_v0(),
    )
    assert "STOP_BEYOND_LIQUIDATION" in _codes(out)


def test_reject_when_take_profit_fractions_exceed_one() -> None:
    out = validate(
        _account(),
        _ctx(take_profit_fractions=[Decimal("0.6"), Decimal("0.6")]),
        RiskPolicy.conservative_v0(),
    )
    assert "TP_FRACTIONS_EXCEED_ONE" in _codes(out)


# --------------------------------------------------------------------------
# Anti-martingale
# --------------------------------------------------------------------------


def test_reject_when_averaging_down_detected() -> None:
    """⭐ Média contra posição perdedora é proibida pela spec."""
    out = validate(
        _account(), _ctx(is_averaging_down=True), RiskPolicy.conservative_v0()
    )
    assert "AVERAGING_DOWN" in _codes(out)


def test_reject_when_stop_widening_detected() -> None:
    """⭐ Ampliar o stop após a entrada aumenta a perda máxima. Proibido."""
    out = validate(
        _account(), _ctx(widens_stop=True), RiskPolicy.conservative_v0()
    )
    assert "STOP_WIDENING" in _codes(out)


# --------------------------------------------------------------------------
# Conflitos e validade
# --------------------------------------------------------------------------


def test_reject_when_conflicting_position_exists() -> None:
    out = validate(
        _account(has_conflicting_position=True), _ctx(), RiskPolicy.conservative_v0()
    )
    assert "CONFLICTING_POSITION" in _codes(out)


def test_reject_when_conflicting_order_exists() -> None:
    out = validate(
        _account(has_conflicting_order=True), _ctx(), RiskPolicy.conservative_v0()
    )
    assert "CONFLICTING_ORDER" in _codes(out)


def test_reject_when_intent_expired() -> None:
    out = validate(
        _account(),
        _ctx(intent_expires_at_ms=999, now_ms=1000),
        RiskPolicy.conservative_v0(),
    )
    assert "INTENT_EXPIRED" in _codes(out)


# --------------------------------------------------------------------------
# Relatório completo
# --------------------------------------------------------------------------


def test_all_validators_run_even_when_first_fails() -> None:
    """⭐ Múltiplas violações simultâneas produzem TODOS os códigos, não
    só o primeiro. O relatório é completo."""
    out = validate(
        _account(
            daily_pnl=Decimal("-2000"),
            open_positions=1,
            spread_bps=Decimal("50"),
        ),
        _ctx(symbol="DOGEUSDT", rr_net=Decimal("0.5")),
        RiskPolicy.conservative_v0(),
    )
    codes = _codes(out)
    assert {
        "DAILY_LOSS_LIMIT",
        "MAX_POSITIONS",
        "SPREAD_TOO_WIDE",
        "SYMBOL_NOT_ALLOWED",
        "RR_TOO_LOW",
    } <= codes


def test_rejection_reason_is_machine_readable_code() -> None:
    out = validate(_account(open_positions=1), _ctx(), RiskPolicy.conservative_v0())
    for r in out.rejections:
        assert r.code.isupper()
        assert " " not in r.code
        assert r.detail  # mensagem legível para humano acompanha o código


def test_validate_is_deterministic() -> None:
    acc, ctx, pol = _account(open_positions=1), _ctx(), RiskPolicy.conservative_v0()
    assert validate(acc, ctx, pol) == validate(acc, ctx, pol)
