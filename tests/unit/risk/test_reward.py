"""B1 — RR calculado dos preços de TP, não declarado pelo modelo.

Achado 1 do review (crítico): o motor confiava no `rr_net` que o modelo
declarava. Com `rr_net=999` e TP vazio, um trade era aprovado. Isso viola
o princípio central — o modelo influenciava a aprovação.

Agora o RR é CALCULADO a partir dos preços de TP, da entrada, do stop e
dos custos. O valor declarado pelo modelo (se houver) é só diagnóstico.

Valores de referência calculados à mão para cada teste.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bybit_agent.risk.reward import TakeProfitLevel, compute_rr_net


def _tp(price: str, fraction: str) -> TakeProfitLevel:
    return TakeProfitLevel(price=Decimal(price), fraction=Decimal(fraction))


# --------------------------------------------------------------------------
# TakeProfitLevel — validação
# --------------------------------------------------------------------------


def test_tp_level_rejects_non_positive_price() -> None:
    with pytest.raises(ValueError):
        TakeProfitLevel(price=Decimal("0"), fraction=Decimal("0.5"))


def test_tp_level_rejects_fraction_out_of_range() -> None:
    for bad in ("0", "-0.1", "1.5"):
        with pytest.raises(ValueError):
            TakeProfitLevel(price=Decimal("61000"), fraction=Decimal(bad))


def test_tp_level_rejects_non_finite() -> None:
    with pytest.raises(ValueError):
        TakeProfitLevel(price=Decimal("NaN"), fraction=Decimal("0.5"))


# --------------------------------------------------------------------------
# RR sem custos — referência limpa
# --------------------------------------------------------------------------


def test_long_rr_two_tps_no_costs() -> None:
    """LONG entry=60000 stop=59400 (risco 600). TP1 61200@0.5, TP2 61800@0.5.
    reward = 0.5*1200 + 0.5*1800 = 1500. RR = 1500/600 = 2.5."""
    rr = compute_rr_net(
        side="BUY", entry=Decimal("60000"), stop=Decimal("59400"),
        take_profit_levels=(_tp("61200", "0.5"), _tp("61800", "0.5")),
        entry_fee_rate=Decimal("0"), exit_fee_rate=Decimal("0"),
        entry_slippage=Decimal("0"), exit_slippage=Decimal("0"),
    )
    assert rr == Decimal("2.5")


def test_short_rr_single_tp_no_costs() -> None:
    """SHORT entry=60000 stop=60600 (risco 600). TP 58800@1.0.
    reward = 60000-58800 = 1200. RR = 1200/600 = 2.0."""
    rr = compute_rr_net(
        side="SELL", entry=Decimal("60000"), stop=Decimal("60600"),
        take_profit_levels=(_tp("58800", "1"),),
        entry_fee_rate=Decimal("0"), exit_fee_rate=Decimal("0"),
        entry_slippage=Decimal("0"), exit_slippage=Decimal("0"),
    )
    assert rr == Decimal("2")


# --------------------------------------------------------------------------
# Custos reduzem o RR — sempre para o lado conservador
# --------------------------------------------------------------------------


def test_fees_and_slippage_reduce_rr() -> None:
    """Taxas e slippage aumentam o risco e reduzem a recompensa, então o
    RR líquido é MENOR que o bruto."""
    kwargs = dict(
        side="BUY", entry=Decimal("60000"), stop=Decimal("59400"),
        take_profit_levels=(_tp("61200", "1"),),
    )
    gross = compute_rr_net(
        **kwargs, entry_fee_rate=Decimal("0"), exit_fee_rate=Decimal("0"),
        entry_slippage=Decimal("0"), exit_slippage=Decimal("0"),
    )
    net = compute_rr_net(
        **kwargs, entry_fee_rate=Decimal("0.00055"), exit_fee_rate=Decimal("0.00055"),
        entry_slippage=Decimal("5"), exit_slippage=Decimal("5"),
    )
    assert net < gross


def test_negative_fee_rebate_does_not_inflate_rr() -> None:
    """Um rebate (taxa negativa) não pode inflar o RR — taxa efetiva >= 0."""
    no_fee = compute_rr_net(
        side="BUY", entry=Decimal("60000"), stop=Decimal("59400"),
        take_profit_levels=(_tp("61200", "1"),),
        entry_fee_rate=Decimal("0"), exit_fee_rate=Decimal("0"),
        entry_slippage=Decimal("0"), exit_slippage=Decimal("0"),
    )
    rebate = compute_rr_net(
        side="BUY", entry=Decimal("60000"), stop=Decimal("59400"),
        take_profit_levels=(_tp("61200", "1"),),
        entry_fee_rate=Decimal("-0.0005"), exit_fee_rate=Decimal("-0.0005"),
        entry_slippage=Decimal("0"), exit_slippage=Decimal("0"),
    )
    assert rebate == no_fee


# --------------------------------------------------------------------------
# Frações de TP devem somar exatamente 1 (plano de saída completo)
# --------------------------------------------------------------------------


def test_rr_requires_tp_fractions_sum_to_one() -> None:
    """Sem um plano de saída completo (soma != 1) o RR é ambíguo. O motor
    exige plano completo em vez de estimar um runner."""
    with pytest.raises(ValueError, match="soma"):
        compute_rr_net(
            side="BUY", entry=Decimal("60000"), stop=Decimal("59400"),
            take_profit_levels=(_tp("61200", "0.5"),),  # soma 0.5
            entry_fee_rate=Decimal("0"), exit_fee_rate=Decimal("0"),
            entry_slippage=Decimal("0"), exit_slippage=Decimal("0"),
        )


def test_rr_rejects_empty_tp_levels() -> None:
    """⭐ TP vazio era a brecha: RR declarado alto + zero TP aprovava."""
    with pytest.raises(ValueError):
        compute_rr_net(
            side="BUY", entry=Decimal("60000"), stop=Decimal("59400"),
            take_profit_levels=(),
            entry_fee_rate=Decimal("0"), exit_fee_rate=Decimal("0"),
            entry_slippage=Decimal("0"), exit_slippage=Decimal("0"),
        )


# --------------------------------------------------------------------------
# TP do lado errado da entrada é incoerente
# --------------------------------------------------------------------------


def test_long_tp_below_entry_is_rejected() -> None:
    """LONG: um TP abaixo da entrada não é um alvo de lucro."""
    with pytest.raises(ValueError, match="alvo|TP|lado"):
        compute_rr_net(
            side="BUY", entry=Decimal("60000"), stop=Decimal("59400"),
            take_profit_levels=(_tp("59000", "1"),),
            entry_fee_rate=Decimal("0"), exit_fee_rate=Decimal("0"),
            entry_slippage=Decimal("0"), exit_slippage=Decimal("0"),
        )


def test_short_tp_above_entry_is_rejected() -> None:
    with pytest.raises(ValueError, match="alvo|TP|lado"):
        compute_rr_net(
            side="SELL", entry=Decimal("60000"), stop=Decimal("60600"),
            take_profit_levels=(_tp("61000", "1"),),
            entry_fee_rate=Decimal("0"), exit_fee_rate=Decimal("0"),
            entry_slippage=Decimal("0"), exit_slippage=Decimal("0"),
        )


# --------------------------------------------------------------------------
# Determinismo e contexto Decimal
# --------------------------------------------------------------------------


def test_rr_is_deterministic() -> None:
    args = dict(
        side="BUY", entry=Decimal("60000"), stop=Decimal("59400"),
        take_profit_levels=(_tp("61200", "0.5"), _tp("61800", "0.5")),
        entry_fee_rate=Decimal("0.00055"), exit_fee_rate=Decimal("0.00055"),
        entry_slippage=Decimal("5"), exit_slippage=Decimal("5"),
    )
    assert compute_rr_net(**args) == compute_rr_net(**args)


def test_rr_is_decimal() -> None:
    rr = compute_rr_net(
        side="BUY", entry=Decimal("60000"), stop=Decimal("59400"),
        take_profit_levels=(_tp("61200", "1"),),
        entry_fee_rate=Decimal("0"), exit_fee_rate=Decimal("0"),
        entry_slippage=Decimal("0"), exit_slippage=Decimal("0"),
    )
    assert isinstance(rr, Decimal)
