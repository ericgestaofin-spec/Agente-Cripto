"""Liquidez do book — imbalance, profundidade e slippage por impacto.

O spread sozinho não diz o custo de executar. Estas funções leem o book
inteiro para estimar:

  - IMBALANCE: pressão relativa comprador/vendedor no topo do book,
    normalizada em [-1, 1] (>0 = mais bid, viés de alta).
  - PROFUNDIDADE: tamanho disponível dentro de X bps do mid — quanto dá
    para executar sem afastar muito o preço.
  - SLIPPAGE: caminhando o book, o custo médio de preencher uma quantidade
    versus o melhor preço do lado (o impacto além do toque). `None` quando o
    book é raso demais para preencher — honesto, nunca finge liquidez.

Puro e em Decimal. Alimenta o snapshot e o sizing (proxy de slippage v0 do
Risk Engine será substituído por isto).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from bybit_agent.domain.money import decimal_context
from bybit_agent.marketdata.rest import OrderBook


@dataclass(frozen=True, slots=True)
class LiquiditySummary:
    imbalance: Decimal
    bid_depth: Decimal
    ask_depth: Decimal


def book_imbalance(ob: OrderBook, *, levels: int = 10) -> Decimal:
    """Pressão relativa no topo do book, em [-1, 1].

    (bidVol − askVol) / (bidVol + askVol) sobre os `levels` melhores níveis.
    +1 = só bids; −1 = só asks; 0 = equilíbrio ou book vazio.
    """
    with localcontext(decimal_context()):
        bid = sum((lvl.size for lvl in ob.bids[:levels]), Decimal("0"))
        ask = sum((lvl.size for lvl in ob.asks[:levels]), Decimal("0"))
        total = bid + ask
        if total == 0:
            return Decimal("0")
        return (bid - ask) / total


def depth_within_bps(ob: OrderBook, *, bps: Decimal, side: str) -> Decimal:
    """Tamanho acumulado dentro de `bps` do mid, num lado (BID ou ASK)."""
    if side not in ("BID", "ASK"):
        raise ValueError(f"side inválido: {side!r} (use BID ou ASK)")
    with localcontext(decimal_context()):
        mid = ob.mid()
        limit = mid * bps / Decimal("10000")
        if side == "BID":
            return sum((lvl.size for lvl in ob.bids if mid - lvl.price <= limit),
                       Decimal("0"))
        return sum((lvl.size for lvl in ob.asks if lvl.price - mid <= limit),
                   Decimal("0"))


def estimate_slippage_bps(
    ob: OrderBook, *, side: str, quantity: Decimal
) -> Decimal | None:
    """Slippage de impacto ao preencher `quantity` a mercado, em bps.

    Caminha o lado relevante (asks para BUY, bids para SELL), acumulando o
    custo até preencher. Compara o preço médio de preenchimento com o melhor
    preço do lado (o toque). `None` se o book não tem tamanho suficiente —
    liquidez insuficiente é dito, nunca escondido.
    """
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side inválido: {side!r} (use BUY ou SELL)")
    if quantity <= 0:
        raise ValueError("quantity deve ser positiva")

    levels = ob.asks if side == "BUY" else ob.bids
    with localcontext(decimal_context()):
        ref = levels[0].price
        remaining = quantity
        cost = Decimal("0")
        filled = Decimal("0")
        for lvl in levels:
            take = min(remaining, lvl.size)
            cost += take * lvl.price
            filled += take
            remaining -= take
            if remaining <= 0:
                break
        if remaining > 0 or filled == 0:
            return None  # book raso demais
        avg = cost / filled
        diff = (avg - ref) if side == "BUY" else (ref - avg)
        return diff / ref * Decimal("10000")


def summarize_liquidity(ob: OrderBook, *, levels: int = 10,
                        depth_bps: Decimal = Decimal("10")) -> LiquiditySummary:
    """Resumo compacto para o snapshot."""
    return LiquiditySummary(
        imbalance=book_imbalance(ob, levels=levels),
        bid_depth=depth_within_bps(ob, bps=depth_bps, side="BID"),
        ask_depth=depth_within_bps(ob, bps=depth_bps, side="ASK"),
    )
