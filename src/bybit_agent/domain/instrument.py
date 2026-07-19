"""Especificação do instrumento — espelha /v5/market/instruments-info.

O aninhamento dos campos segue exatamente o documentado em
`docs/BYBIT_INTEGRACAO.md` §3:

    priceFilter    -> tickSize
    lotSizeFilter  -> qtyStep, minOrderQty, maxOrderQty,
                      maxMktOrderQty, minNotionalValue
    leverageFilter -> maxLeverage

Nenhum campo tem default. Um `qty_step` presumido de 0.001 num símbolo
que usa 0.01 produz quantidades que a corretora rejeita — falhar na
leitura da spec é infinitamente melhor que falhar no envio da ordem.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

OrderType = Literal["Limit", "Market"]


def _dec(container: dict[str, Any], key: str, *, where: str) -> Decimal:
    """Extrai um campo obrigatório como Decimal, sem default."""
    if key not in container:
        raise KeyError(f"campo obrigatório ausente em {where}: {key}")
    raw = container[key]
    if isinstance(raw, float):
        raise TypeError(f"{where}.{key}: float não é aceito, use string decimal")
    return Decimal(str(raw))


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Restrições da corretora para um símbolo. Imutável."""

    symbol: str
    tick_size: Decimal
    qty_step: Decimal
    min_order_qty: Decimal
    max_order_qty: Decimal
    max_market_order_qty: Decimal
    min_notional: Decimal
    max_leverage: Decimal

    @classmethod
    def from_bybit(cls, payload: dict[str, Any]) -> InstrumentSpec:
        status = payload.get("status")
        if status != "Trading":
            raise ValueError(
                f"{payload.get('symbol')}: não está negociando (status={status})"
            )

        price = payload.get("priceFilter")
        lot = payload.get("lotSizeFilter")
        lev = payload.get("leverageFilter")
        for name, node in (("priceFilter", price), ("lotSizeFilter", lot),
                           ("leverageFilter", lev)):
            if not isinstance(node, dict):
                raise ValueError(f"bloco obrigatório ausente ou inválido: {name}")

        assert isinstance(price, dict) and isinstance(lot, dict) and isinstance(lev, dict)

        return cls(
            symbol=str(payload["symbol"]),
            tick_size=_dec(price, "tickSize", where="priceFilter"),
            qty_step=_dec(lot, "qtyStep", where="lotSizeFilter"),
            min_order_qty=_dec(lot, "minOrderQty", where="lotSizeFilter"),
            max_order_qty=_dec(lot, "maxOrderQty", where="lotSizeFilter"),
            max_market_order_qty=_dec(lot, "maxMktOrderQty", where="lotSizeFilter"),
            min_notional=_dec(lot, "minNotionalValue", where="lotSizeFilter"),
            max_leverage=_dec(lev, "maxLeverage", where="leverageFilter"),
        )

    def max_qty_for(self, *, order_type: OrderType) -> Decimal:
        """Teto de quantidade conforme o tipo de ordem.

        A Bybit impõe limites distintos: `maxOrderQty` para limit e
        `maxMktOrderQty` para market. O sizing precisa do correto.
        """
        return self.max_market_order_qty if order_type == "Market" else self.max_order_qty
