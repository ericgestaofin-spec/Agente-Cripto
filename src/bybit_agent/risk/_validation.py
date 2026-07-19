"""Validação de fronteira em runtime — helpers compartilhados.

`Literal` e `frozen=True` são garantias de type-checker e de rebind. Não
validam VALORES em runtime nem impedem mutação de listas. Estes helpers
fecham essa lacuna: dado inválido falha alto na construção do objeto de
fronteira, antes de chegar a qualquer cálculo.

Resposta ao review externo (docs/REVISAO_ACHADOS.md, categoria A).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

_VALID_SIDES = frozenset({"BUY", "SELL"})


def require_side(side: str) -> None:
    if side not in _VALID_SIDES:
        raise ValueError(
            f"side inválido {side!r}; esperado um de {sorted(_VALID_SIDES)}"
        )


def require_finite(value: Decimal, *, name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} deve ser Decimal, recebido {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{name} deve ser finito, recebido {value!r}")


def require_finite_positive(value: Decimal, *, name: str) -> None:
    require_finite(value, name=name)
    if value <= 0:
        raise ValueError(f"{name} deve ser positivo, recebido {value!r}")


def require_finite_non_negative(value: Decimal, *, name: str) -> None:
    require_finite(value, name=name)
    if value < 0:
        raise ValueError(f"{name} deve ser >= 0, recebido {value!r}")


def require_non_negative_int(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} deve ser int, recebido {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} deve ser >= 0, recebido {value}")


def require_non_empty_symbol(symbol: str) -> None:
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError(f"símbolo inválido {symbol!r}")


def validate_take_profit_fractions(
    fractions: Sequence[Decimal],
) -> tuple[Decimal, ...]:
    """Valida cada fração (0 < f <= 1) e devolve uma tupla imutável.

    `[-0.5, 1.5]` somava exatamente 1 e passava no validador de soma —
    mas cada fração individual precisa ser um pedaço válido da posição.
    A soma continua sendo checada separadamente pelo validador _tp_fractions.
    """
    out: list[Decimal] = []
    for i, f in enumerate(fractions):
        if not isinstance(f, Decimal):
            raise TypeError(f"fração de TP[{i}] deve ser Decimal")
        if not f.is_finite():
            raise ValueError(f"fração de TP[{i}] deve ser finita, recebido {f!r}")
        if not (0 < f <= 1):
            raise ValueError(
                f"fração de TP[{i}] deve estar em (0, 1], recebido {f!r}"
            )
        out.append(f)
    return tuple(out)
