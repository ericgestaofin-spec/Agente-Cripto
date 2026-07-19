"""Tipos monetários — a fundação de correção do sistema.

Regra inviolável: **dinheiro nunca é float**. `float` não representa
`0.1` exatamente; um erro de 1e-17 propagado por um cálculo de sizing
vira uma quantidade que a corretora rejeita — ou pior, aceita errada.

Todo valor de preço, quantidade e PnL neste sistema é `Decimal`,
construído a partir de `str`, `int` ou `Decimal`. A construção a partir
de `float` levanta `TypeError` por design, não por descuido.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    localcontext,
)
from typing import Final

DECIMAL_PRECISION: Final[int] = 28
"""28 dígitos significativos. BTCUSDT usa no máximo ~13 (preço 5 + qty 8);
a folga cobre cálculos intermediários de sizing sem arredondamento silencioso."""


def decimal_context() -> Context:
    """Contexto decimal do sistema.

    `InvalidOperation` e `DivisionByZero` são armadilhas (levantam exceção)
    em vez de produzir `NaN`/`Infinity` silenciosamente. Um `NaN` que chega
    ao motor de risco compara `False` com tudo — inclusive com os limites —
    e passaria por qualquer validação escrita de forma ingênua.
    """
    return Context(
        prec=DECIMAL_PRECISION,
        traps=[InvalidOperation, DivisionByZero],
    )


def _coerce(value: str | int | Decimal, *, field: str) -> Decimal:
    """Converte para Decimal, rejeitando float e valores não finitos."""
    if isinstance(value, bool):
        raise TypeError(f"{field}: bool não é um valor monetário válido")
    if isinstance(value, float):
        raise TypeError(
            f"{field}: float é proibido em valores monetários "
            f"(recebido {value!r}). Use str, int ou Decimal."
        )
    if not isinstance(value, str | int | Decimal):
        raise TypeError(f"{field}: tipo não suportado {type(value).__name__}")

    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field}: valor decimal inválido {value!r}") from exc

    if not result.is_finite():
        raise ValueError(f"{field}: valor deve ser finito, recebido {value!r}")

    return result


@dataclass(frozen=True, slots=True)
class Price:
    """Preço em moeda de cotação (USDT). Sempre >= 0 e finito."""

    value: Decimal

    def __init__(self, value: str | int | Decimal) -> None:
        coerced = _coerce(value, field="Price")
        if coerced < 0:
            raise ValueError(f"Price: valor negativo não permitido ({value!r})")
        object.__setattr__(self, "value", coerced)

    def __str__(self) -> str:
        return str(self.value)

    def __add__(self, other: Price) -> Price:
        _require_same_type(self, other, "+")
        return Price(self.value + other.value)

    def __sub__(self, other: Price) -> Price:
        _require_same_type(self, other, "-")
        return Price(self.value - other.value)

    def __lt__(self, other: Price) -> bool:
        _require_same_type(self, other, "<")
        return self.value < other.value

    def __le__(self, other: Price) -> bool:
        _require_same_type(self, other, "<=")
        return self.value <= other.value

    def __gt__(self, other: Price) -> bool:
        _require_same_type(self, other, ">")
        return self.value > other.value

    def __ge__(self, other: Price) -> bool:
        _require_same_type(self, other, ">=")
        return self.value >= other.value


@dataclass(frozen=True, slots=True)
class Quantity:
    """Quantidade de contratos. Sempre >= 0 e finita.

    Zero é legítimo: representa posição encerrada.
    """

    value: Decimal

    def __init__(self, value: str | int | Decimal) -> None:
        coerced = _coerce(value, field="Quantity")
        if coerced < 0:
            raise ValueError(f"Quantity: valor negativo não permitido ({value!r})")
        object.__setattr__(self, "value", coerced)

    def __str__(self) -> str:
        return str(self.value)

    def __add__(self, other: Quantity) -> Quantity:
        _require_same_type(self, other, "+")
        return Quantity(self.value + other.value)

    def __sub__(self, other: Quantity) -> Quantity:
        _require_same_type(self, other, "-")
        return Quantity(self.value - other.value)

    def __lt__(self, other: Quantity) -> bool:
        _require_same_type(self, other, "<")
        return self.value < other.value

    def __le__(self, other: Quantity) -> bool:
        _require_same_type(self, other, "<=")
        return self.value <= other.value

    def __gt__(self, other: Quantity) -> bool:
        _require_same_type(self, other, ">")
        return self.value > other.value

    def __ge__(self, other: Quantity) -> bool:
        _require_same_type(self, other, ">=")
        return self.value >= other.value


def _require_same_type(left: object, right: object, op: str) -> None:
    """Impede aritmética entre tipos monetários diferentes.

    `Price + Quantity` é sempre um bug de sizing. Falhar alto é melhor
    que produzir um número plausível.
    """
    if isinstance(right, float):
        raise TypeError(
            f"{type(left).__name__} {op} float é proibido — float não é um valor monetário"
        )
    if type(left) is not type(right):
        raise TypeError(
            f"operação inválida: {type(left).__name__} {op} {type(right).__name__}"
        )


def _require_positive_increment(increment: Decimal, *, name: str) -> None:
    if not isinstance(increment, Decimal):
        raise TypeError(f"{name} deve ser Decimal, recebido {type(increment).__name__}")
    if not increment.is_finite() or increment <= 0:
        raise ValueError(f"{name} deve ser positivo e finito, recebido {increment!r}")


def round_down_to_tick(price: Price, tick_size: Decimal) -> Price:
    """Alinha o preço ao `tickSize` da corretora, arredondando para baixo.

    A direção é explícita e obrigatória — não há default. Arredondar um
    stop na direção errada muda o risco da operação, e um default silencioso
    esconderia isso.
    """
    _require_positive_increment(tick_size, name="tick_size")
    with localcontext(decimal_context()):
        steps = (price.value / tick_size).to_integral_value(rounding=ROUND_FLOOR)
        return Price(steps * tick_size)


def round_up_to_tick(price: Price, tick_size: Decimal) -> Price:
    """Alinha o preço ao `tickSize`, arredondando para cima."""
    _require_positive_increment(tick_size, name="tick_size")
    with localcontext(decimal_context()):
        steps = (price.value / tick_size).to_integral_value(rounding=ROUND_CEILING)
        return Price(steps * tick_size)


def round_down_to_step(quantity: Quantity, qty_step: Decimal) -> Quantity:
    """Alinha a quantidade ao `qtyStep`, **sempre para baixo**.

    Não existe `round_up_to_step` neste módulo, e isso é deliberado:
    arredondar quantidade para cima excede o orçamento de risco calculado.
    Se o resultado for zero, o sizing deve rejeitar a operação — nunca
    compensar subindo para o mínimo.
    """
    _require_positive_increment(qty_step, name="qty_step")
    with localcontext(decimal_context()):
        steps = (quantity.value / qty_step).to_integral_value(rounding=ROUND_FLOOR)
        return Quantity(steps * qty_step)
