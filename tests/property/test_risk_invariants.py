"""Sprint 4d — invariantes do Risk Engine (property-based).

O motor de risco não pode ser testado só por exemplos. Exemplos cobrem
os casos que eu imaginei; propriedades cobrem os que eu não imaginei.

Cada `@given` gera 1000+ casos. Se uma dessas propriedades quebrar, é um
bug no componente onde o dinheiro vive — nada mais importa até consertar.

As duas mais importantes:
  P1 — a perda máxima projetada nunca ultrapassa o orçamento de risco.
  P5 — o motor é determinístico: mesma entrada, mesma saída, sempre.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from bybit_agent.domain.instrument import InstrumentSpec
from bybit_agent.domain.money import Price, Quantity
from bybit_agent.risk.sizing import SizingInputs, compute_size

SETTINGS = settings(
    max_examples=1000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------


def _decimals(lo: str, hi: str, places: int = 2) -> st.SearchStrategy[Decimal]:
    q = Decimal(10) ** -places
    return st.decimals(
        min_value=Decimal(lo),
        max_value=Decimal(hi),
        allow_nan=False,
        allow_infinity=False,
        places=places,
    ).map(lambda d: d.quantize(q))


_BTCUSDT_SPEC = InstrumentSpec.from_bybit(
    {
        "symbol": "BTCUSDT",
        "status": "Trading",
        "priceFilter": {"tickSize": "0.10", "minPrice": "0.10", "maxPrice": "9999999"},
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


@st.composite
def sizing_inputs(draw: st.DrawFn) -> SizingInputs:
    equity = draw(_decimals("100", "10000000"))
    entry = draw(_decimals("1000", "200000"))
    # Distância de stop entre 0,05% e 5% da entrada, de qualquer lado.
    dist_frac = draw(_decimals("0.0005", "0.05", places=4))
    distance = (entry * dist_frac).quantize(Decimal("0.1"))
    assume(distance > 0)
    side_long = draw(st.booleans())
    stop = entry - distance if side_long else entry + distance
    assume(stop > 0)

    return SizingInputs(
        equity=Price(equity),
        entry=Price(entry),
        stop=Price(stop),
        risk_fraction=draw(_decimals("0.0005", "0.02", places=4)),
        taker_fee_rate=draw(_decimals("0", "0.001", places=5)),
        estimated_slippage=Price(draw(_decimals("0", "50"))),
        max_leverage=draw(_decimals("1", "10", places=1)),
        available_liquidity=Quantity(draw(_decimals("0.001", "10000", places=3))),
        spec=_BTCUSDT_SPEC,
        order_type=draw(st.sampled_from(["Limit", "Market"])),
    )


# --------------------------------------------------------------------------
# P1 — a invariante mais importante do projeto
# --------------------------------------------------------------------------


@given(inp=sizing_inputs())
@SETTINGS
def test_P1_projected_loss_never_exceeds_risk_budget(inp: SizingInputs) -> None:
    """A perda máxima projetada (qty × risco/unidade) nunca ultrapassa o
    orçamento de risco. Esta é a promessa central do motor."""
    r = compute_size(inp)
    assume(r.approved)
    assert r.quantity is not None
    projected_loss = r.quantity.value * r.risk_per_unit
    assert projected_loss <= r.risk_budget, (
        f"perda projetada {projected_loss} > orçamento {r.risk_budget}"
    )


# --------------------------------------------------------------------------
# P2 — alinhamento ao qtyStep, sempre para baixo
# --------------------------------------------------------------------------


@given(inp=sizing_inputs())
@SETTINGS
def test_P2_quantity_is_multiple_of_step_and_never_rounds_up(inp: SizingInputs) -> None:
    r = compute_size(inp)
    assume(r.approved)
    assert r.quantity is not None
    assert r.quantity.value % inp.spec.qty_step == 0
    assert r.quantity.value <= r.quantity_unrounded


# --------------------------------------------------------------------------
# P3 — nunca abaixo do mínimo quando aprovado
# --------------------------------------------------------------------------


@given(inp=sizing_inputs())
@SETTINGS
def test_P3_approved_quantity_respects_broker_minimums(inp: SizingInputs) -> None:
    r = compute_size(inp)
    assume(r.approved)
    assert r.quantity is not None
    assert r.quantity.value >= inp.spec.min_order_qty
    assert r.quantity.value * inp.entry.value >= inp.spec.min_notional


# --------------------------------------------------------------------------
# P4 — nunca excede nenhum teto
# --------------------------------------------------------------------------


@given(inp=sizing_inputs())
@SETTINGS
def test_P4_quantity_respects_all_caps(inp: SizingInputs) -> None:
    r = compute_size(inp)
    assume(r.approved)
    assert r.quantity is not None
    q = r.quantity.value
    # exposição / alavancagem — invariante ESTRITA. Como a quantidade é
    # arredondada para baixo e o teto de alavancagem é um dos mínimos,
    # o notional nunca excede equity*max_leverage. Sem folga de qtyStep.
    assert q * inp.entry.value <= inp.equity.value * inp.max_leverage
    # liquidez
    assert q <= inp.available_liquidity.value
    # símbolo
    assert q <= inp.spec.max_qty_for(order_type=inp.order_type)


# --------------------------------------------------------------------------
# P5 — determinismo
# --------------------------------------------------------------------------


@given(inp=sizing_inputs())
@SETTINGS
def test_P5_engine_is_deterministic(inp: SizingInputs) -> None:
    """Mesma entrada → mesma saída. Sem estado oculto, sem relógio, sem
    aleatoriedade. Se isto quebrar, nenhum outro teste é confiável."""
    assert compute_size(inp) == compute_size(inp)


# --------------------------------------------------------------------------
# P6 — nenhum float escapa
# --------------------------------------------------------------------------


@given(inp=sizing_inputs())
@SETTINGS
def test_P6_no_float_in_result(inp: SizingInputs) -> None:
    r = compute_size(inp)
    assert isinstance(r.risk_per_unit, Decimal)
    assert isinstance(r.risk_budget, Decimal)
    assert isinstance(r.quantity_unrounded, Decimal)
    if r.quantity is not None:
        assert isinstance(r.quantity, Quantity)
        assert isinstance(r.quantity.value, Decimal)


# --------------------------------------------------------------------------
# P7 — quantidade sempre não-negativa (cobre LONG e SHORT)
# --------------------------------------------------------------------------


@given(inp=sizing_inputs())
@SETTINGS
def test_P7_quantity_is_never_negative(inp: SizingInputs) -> None:
    """SHORT tem stop acima da entrada; a distância é absoluta. O motor
    nunca produz quantidade negativa, independente do lado."""
    r = compute_size(inp)
    if r.quantity is not None:
        assert r.quantity.value > 0


# --------------------------------------------------------------------------
# P8 — rejeição é sempre acompanhada de motivo, aprovação nunca
# --------------------------------------------------------------------------


@given(inp=sizing_inputs())
@SETTINGS
def test_P8_rejection_always_has_reason(inp: SizingInputs) -> None:
    r = compute_size(inp)
    if r.approved:
        assert r.quantity is not None
        assert r.rejection_reason == ""
    else:
        assert r.quantity is None
        assert r.rejection_reason != ""


# --------------------------------------------------------------------------
# P9 — reduzir o risco nunca aumenta a quantidade
# --------------------------------------------------------------------------


@given(inp=sizing_inputs())
@SETTINGS
def test_P9_less_risk_never_yields_more_quantity(inp: SizingInputs) -> None:
    """Monotonicidade: metade do orçamento de risco nunca produz uma
    quantidade maior. Uma violação aqui denunciaria um bug de sinal."""
    full = compute_size(inp)
    half = compute_size(
        SizingInputs(
            equity=inp.equity,
            entry=inp.entry,
            stop=inp.stop,
            risk_fraction=inp.risk_fraction / 2,
            taker_fee_rate=inp.taker_fee_rate,
            estimated_slippage=inp.estimated_slippage,
            max_leverage=inp.max_leverage,
            available_liquidity=inp.available_liquidity,
            spec=inp.spec,
            order_type=inp.order_type,
        )
    )
    if full.quantity is not None and half.quantity is not None:
        assert half.quantity.value <= full.quantity.value
