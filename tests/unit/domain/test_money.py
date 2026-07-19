"""Sprint 0 — tipos monetários.

Invariante central do projeto: dinheiro nunca é float. Nem uma vez, em
nenhum caminho. Estes testes existem para tornar isso mecanicamente
verdadeiro, não uma convenção de equipe.
"""

from decimal import Decimal

import pytest

from bybit_agent.domain.money import (
    Price,
    Quantity,
    round_down_to_step,
    round_down_to_tick,
    round_up_to_tick,
)

# --------------------------------------------------------------------------
# Construção
# --------------------------------------------------------------------------


def test_money_rejects_float_construction() -> None:
    """Dinheiro nunca nasce de float. Nem uma vez."""
    with pytest.raises(TypeError, match="float"):
        Price(0.1)


def test_quantity_rejects_float_construction() -> None:
    with pytest.raises(TypeError, match="float"):
        Quantity(0.001)


def test_money_accepts_str_int_and_decimal() -> None:
    assert Price("100.50").value == Decimal("100.50")
    assert Price(100).value == Decimal("100")
    assert Price(Decimal("100.50")).value == Decimal("100.50")


def test_money_preserves_trailing_zeros_from_string() -> None:
    """A Bybit envia '100.50'. Reserializar como '100.5' muda a string
    assinada e quebra o HMAC. A precisão declarada é significativa."""
    assert str(Price("100.50")) == "100.50"


def test_money_never_produces_nan_or_inf() -> None:
    for bad in ("NaN", "Infinity", "-Infinity", "nan", "inf"):
        with pytest.raises(ValueError, match="finito"):
            Price(bad)


def test_negative_price_is_rejected() -> None:
    with pytest.raises(ValueError, match="negativ"):
        Price("-1")


def test_negative_quantity_is_rejected() -> None:
    with pytest.raises(ValueError, match="negativ"):
        Quantity("-0.001")


def test_zero_quantity_is_allowed() -> None:
    """Posição zerada é um estado legítimo."""
    assert Quantity("0").value == Decimal("0")


def test_price_and_quantity_are_not_interchangeable() -> None:
    """Trocar preço por quantidade num sizing é um erro caro.
    O type system deve pegar; a igualdade também."""
    assert Price("1") != Quantity("1")


def test_money_is_immutable() -> None:
    p = Price("100")
    with pytest.raises((AttributeError, TypeError)):
        p.value = Decimal("200")  # type: ignore[misc]


# --------------------------------------------------------------------------
# Aritmética
# --------------------------------------------------------------------------


def test_money_arithmetic_preserves_precision() -> None:
    """0.1 + 0.2 == 0.3 exatamente. É o motivo de Decimal existir."""
    assert Price("0.1") + Price("0.2") == Price("0.3")


def test_subtraction_of_prices_yields_price() -> None:
    assert Price("100.5") - Price("0.5") == Price("100.0")


def test_arithmetic_with_float_is_rejected() -> None:
    with pytest.raises(TypeError, match="float"):
        Price("100") + 0.1  # type: ignore[operator]


def test_arithmetic_between_price_and_quantity_is_rejected() -> None:
    with pytest.raises(TypeError):
        Price("100") + Quantity("1")  # type: ignore[operator]


# --------------------------------------------------------------------------
# Arredondamento — tickSize e qtyStep
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "tick", "expected"),
    [
        ("100.567", "0.1", "100.5"),
        ("100.5", "0.1", "100.5"),  # já alinhado, não muda
        ("100.99", "0.5", "100.5"),
        ("0.0", "0.1", "0.0"),
        ("99999.99", "0.01", "99999.99"),
    ],
)
def test_round_down_to_tick(value: str, tick: str, expected: str) -> None:
    assert round_down_to_tick(Price(value), Decimal(tick)) == Price(expected)


@pytest.mark.parametrize(
    ("value", "tick", "expected"),
    [
        ("100.51", "0.1", "100.6"),
        ("100.5", "0.1", "100.5"),  # já alinhado, não muda
        ("100.01", "0.5", "100.5"),
    ],
)
def test_round_up_to_tick(value: str, tick: str, expected: str) -> None:
    assert round_up_to_tick(Price(value), Decimal(tick)) == Price(expected)


@pytest.mark.parametrize(
    ("value", "step", "expected"),
    [
        ("0.0567", "0.001", "0.056"),
        ("1.999", "0.001", "1.999"),
        ("0.0009", "0.001", "0"),  # abaixo do step vira zero — sizing deve rejeitar
        ("10", "1", "10"),
    ],
)
def test_round_down_to_step(value: str, step: str, expected: str) -> None:
    assert round_down_to_step(Quantity(value), Decimal(step)) == Quantity(expected)


def test_quantity_never_rounds_up() -> None:
    """Arredondar quantidade para cima viola o orçamento de risco.
    Esta é a regra mais importante deste módulo."""
    result = round_down_to_step(Quantity("0.0999"), Decimal("0.01"))
    assert result.value <= Decimal("0.0999")
    assert result == Quantity("0.09")


def test_rounding_rejects_non_positive_tick() -> None:
    for bad in ("0", "-0.1"):
        with pytest.raises(ValueError, match="positivo"):
            round_down_to_tick(Price("100"), Decimal(bad))


def test_rounding_result_is_exact_multiple_of_tick() -> None:
    result = round_down_to_tick(Price("100.567"), Decimal("0.05"))
    assert result.value % Decimal("0.05") == 0


# --------------------------------------------------------------------------
# Contexto decimal global
# --------------------------------------------------------------------------


def test_decimal_context_has_sufficient_precision() -> None:
    """28 dígitos cobre BTCUSDT com folga. Precisão insuficiente
    arredondaria silenciosamente no meio de um cálculo de sizing."""
    from bybit_agent.domain.money import DECIMAL_PRECISION

    assert DECIMAL_PRECISION >= 28


def test_decimal_context_traps_inexact_division_silently_never() -> None:
    """Divisão que perde precisão deve ser explícita, nunca silenciosa."""
    from decimal import DivisionByZero, InvalidOperation, localcontext

    from bybit_agent.domain.money import decimal_context

    with localcontext(decimal_context()) as ctx:
        assert ctx.traps[InvalidOperation]
        assert ctx.traps[DivisionByZero]
