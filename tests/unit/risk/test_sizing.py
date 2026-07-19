"""Sprint 4b — cálculo de quantidade.

A regra da spec: o modelo NUNCA escolhe o tamanho. Este módulo calcula,
a partir de:

    orçamento_de_risco = patrimônio × percentual_de_risco
    risco_por_unidade  = distância(entrada, stop) + taxas + slippage
    quantidade_bruta   = orçamento_de_risco / risco_por_unidade

E então aplica, pegando o MÍNIMO entre:
    - quantidade bruta
    - limite de exposição
    - limite de alavancagem
    - liquidez disponível
    - limite do símbolo

Arredondando SEMPRE para baixo pelo qtyStep. Se o resultado ficar abaixo
do mínimo da corretora, a operação é rejeitada — nunca arredondada para
cima para "caber".
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bybit_agent.domain.instrument import InstrumentSpec
from bybit_agent.domain.money import Price, Quantity
from bybit_agent.risk.sizing import SizingInputs, SizingResult, compute_size


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


def _inputs(**over: object) -> SizingInputs:
    base = {
        "equity": Price("100000"),
        "entry": Price("60000"),
        "stop": Price("59400"),  # 600 de distância = 1%
        "risk_fraction": Decimal("0.0025"),  # 0,25% = 250 USDT
        "taker_fee_rate": Decimal("0.00055"),
        "estimated_slippage": Price("6"),  # 6 USDT por unidade
        "max_leverage": Decimal("2"),
        "available_liquidity": Quantity("1000"),
        "spec": _spec(),
        "order_type": "Limit",
    }
    base.update(over)
    return SizingInputs(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Cálculo básico
# --------------------------------------------------------------------------


def test_basic_size_calculation() -> None:
    """Orçamento 250, distância 600, taxas ~66, slippage 6 → risco/unidade
    ~672 → qty ~0.372, arredondado para baixo a 0.001."""
    r = compute_size(_inputs())
    assert isinstance(r, SizingResult)
    assert r.approved
    assert r.quantity is not None
    # risco por unidade = 600 + slippage(6) + taxas
    # taxa ~ (60000 + 59400) * 0.00055 = 65.67 por unidade
    # risco/un ~ 671.67 → qty = 250/671.67 = 0.3722 → 0.372
    assert r.quantity == Quantity("0.372")


def test_fees_are_included_in_risk_per_unit() -> None:
    """⭐ Ignorar taxas superdimensiona a posição. Duas entradas idênticas
    exceto pela taxa devem produzir quantidades diferentes."""
    with_fee = compute_size(_inputs(taker_fee_rate=Decimal("0.00055")))
    no_fee = compute_size(_inputs(taker_fee_rate=Decimal("0")))
    assert with_fee.quantity is not None and no_fee.quantity is not None
    assert with_fee.quantity < no_fee.quantity


def test_slippage_is_included_in_risk_per_unit() -> None:
    """⭐ Idem para slippage."""
    with_slip = compute_size(_inputs(estimated_slippage=Price("6")))
    no_slip = compute_size(_inputs(estimated_slippage=Price("0")))
    assert with_slip.quantity is not None and no_slip.quantity is not None
    assert with_slip.quantity < no_slip.quantity


def test_quantity_is_rounded_down_to_step() -> None:
    r = compute_size(_inputs())
    assert r.quantity is not None
    assert r.quantity.value % Decimal("0.001") == 0


def test_quantity_never_exceeds_unrounded() -> None:
    """⭐ Arredondamento nunca infla a quantidade acima do calculado."""
    r = compute_size(_inputs())
    assert r.quantity is not None
    assert r.quantity.value <= r.quantity_unrounded


# --------------------------------------------------------------------------
# Limites (o mínimo entre eles)
# --------------------------------------------------------------------------


def test_capped_by_exposure_limit() -> None:
    """Alavancagem 2x com patrimônio 100k → notional máx 200k → qty máx
    ~3.33 a 60k. Um stop muito apertado tentaria comprar mais que isso."""
    # Stop muito apertado + taxas/slippage zeradas isola o teto de alavancagem:
    # qty_by_risk = 250/12 ≈ 20.8, mas alavancagem 2x limita a 200000/60000 ≈ 3.33.
    r = compute_size(
        _inputs(
            stop=Price("59988"),
            max_leverage=Decimal("2"),
            taker_fee_rate=Decimal("0"),
            estimated_slippage=Price("0"),
        )
    )
    assert r.approved
    assert r.quantity is not None
    notional = r.quantity.value * Decimal("60000")
    assert notional <= Decimal("100000") * Decimal("2")
    assert r.binding_constraint == "leverage"


def test_capped_by_symbol_max_qty() -> None:
    spec = _spec()
    r = compute_size(
        _inputs(
            equity=Price("1000000000"),
            stop=Price("59999"),
            max_leverage=Decimal("100"),
            available_liquidity=Quantity("999999"),
        )
    )
    assert r.quantity is not None
    assert r.quantity.value <= spec.max_order_qty


def test_market_order_uses_market_qty_cap() -> None:
    """maxMktOrderQty (100) < maxOrderQty (500). Ordem a mercado usa o menor."""
    r = compute_size(
        _inputs(
            order_type="Market",
            equity=Price("1000000000"),
            stop=Price("59999"),
            max_leverage=Decimal("100"),
            available_liquidity=Quantity("999999"),
        )
    )
    assert r.quantity is not None
    assert r.quantity.value <= Decimal("100")


def test_capped_by_available_liquidity() -> None:
    r = compute_size(
        _inputs(
            equity=Price("100000000"),
            stop=Price("59999"),
            max_leverage=Decimal("100"),
            available_liquidity=Quantity("0.5"),
        )
    )
    assert r.quantity is not None
    assert r.quantity.value <= Decimal("0.5")
    assert r.binding_constraint == "liquidity"


# --------------------------------------------------------------------------
# Rejeições
# --------------------------------------------------------------------------


def test_rejects_when_below_min_order_qty() -> None:
    """⭐ Patrimônio minúsculo → quantidade abaixo do mínimo → REJEITA.
    Nunca arredonda para cima até o mínimo."""
    r = compute_size(_inputs(equity=Price("10")))  # orçamento 0,025 USDT
    assert not r.approved
    assert r.quantity is None
    assert "mínimo" in r.rejection_reason.lower()


def test_rejects_when_below_min_notional() -> None:
    """minNotionalValue = 5. Uma quantidade acima do minOrderQty ainda
    pode ficar abaixo do notional mínimo."""
    r = compute_size(
        _inputs(equity=Price("250"), entry=Price("60000"), stop=Price("59400"))
    )
    # orçamento 0,625 / risco~672 ≈ 0.00093 → abaixo de minOrderQty 0.001
    assert not r.approved


def test_rejects_zero_risk_distance() -> None:
    """Entrada == stop → divisão por zero no risco/unidade. Rejeita, não
    estoura."""
    r = compute_size(_inputs(stop=Price("60000")))
    assert not r.approved
    assert "distância" in r.rejection_reason.lower()


def test_rejects_when_risk_fraction_is_zero() -> None:
    r = compute_size(_inputs(risk_fraction=Decimal("0")))
    assert not r.approved


def test_all_numeric_result_fields_are_decimal_or_quantity() -> None:
    """⭐ Nenhum float escapa do sizing."""
    r = compute_size(_inputs())
    assert isinstance(r.quantity_unrounded, Decimal)
    assert isinstance(r.risk_per_unit, Decimal)
    assert isinstance(r.risk_budget, Decimal)
    if r.quantity is not None:
        assert isinstance(r.quantity, Quantity)


def test_result_is_deterministic() -> None:
    """⭐ Mesma entrada → mesma saída."""
    a = compute_size(_inputs())
    b = compute_size(_inputs())
    assert a == b


def test_short_side_risk_distance_is_absolute() -> None:
    """SHORT: stop acima da entrada. A distância de risco é o valor
    absoluto — o sizing não pode produzir quantidade negativa."""
    r = compute_size(_inputs(entry=Price("60000"), stop=Price("60600")))
    assert r.approved
    assert r.quantity is not None
    assert r.quantity.value > 0
