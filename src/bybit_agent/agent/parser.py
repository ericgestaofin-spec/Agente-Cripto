"""Parser da decisão do Claude → TradeIntent.

A decisão chega como JSON garantido pelo schema (output_config.format).
Este módulo a converte para os tipos de domínio. É puro — não fala com a
API nem com a Bybit.

O `estimated_rr_net` que o modelo declara vira `model_claimed_rr_net`
(diagnóstico); o Risk Engine recalcula o RR dos preços de TP. O modelo não
decide risco.

Nota sobre `liquidation` (achado B2 do review, ainda pendente): a
`TradeIntent` exige um preço de liquidação, mas a decisão do modelo não o
traz (e não deveria — depende do tamanho, que só existe após o sizing).
Até o B2 estimar isso com risk tier, o orquestrador injeta uma estimativa
conservadora via `estimate_liquidation`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from bybit_agent.risk.engine import TradeIntent
from bybit_agent.risk.reward import TakeProfitLevel

Action = Literal[
    "NO_TRADE", "WATCH", "OPEN_LONG", "OPEN_SHORT",
    "ADJUST_STOP", "TAKE_PARTIAL", "CLOSE_POSITION", "HALT_TRADING",
]

_OPENING = {"OPEN_LONG": "BUY", "OPEN_SHORT": "SELL"}

# Fração conservadora da distância entrada→liquidação usada como estimativa
# interina, até o B2 usar o risk tier real da Bybit.
_INTERIM_LIQ_FRACTION = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class ParsedDecision:
    action: Action
    intent: TradeIntent | None


def _dec(value: Any, *, field: str) -> Decimal:
    """Converte string decimal para Decimal, rejeitando float e None."""
    if value is None:
        raise ValueError(f"campo obrigatório ausente: {field}")
    if isinstance(value, float):
        raise TypeError(f"{field}: número float na decisão; deve ser string decimal")
    if not isinstance(value, str | int | Decimal):
        raise TypeError(f"{field}: tipo inválido {type(value).__name__}")
    return Decimal(value)


def estimate_liquidation(side: str, entry: Decimal, max_leverage: Decimal) -> Decimal:
    """Estimativa conservadora e INTERINA do preço de liquidação.

    LONG liquida abaixo, SHORT acima. Usa uma fração da distância que a
    alavancagem permite — deliberadamente pessimista (mais perto da entrada
    que a liquidação real em cross margin), para o check stop-vs-liquidação
    errar do lado seguro. Substituída no B2 pelo cálculo com risk tier.
    """
    move = entry / max_leverage * _INTERIM_LIQ_FRACTION
    return entry - move if side == "BUY" else entry + move


def parse_decision(
    decision: dict[str, Any],
    *,
    now_ms: int,
    max_leverage: Decimal = Decimal("2"),
) -> ParsedDecision:
    """Converte a decisão do modelo. Ações sem operação retornam intent=None.

    Levanta ValueError/TypeError se uma ação de abertura vier incoerente
    (sem stop, sem entrada, sem TP, preço como float).
    """
    action: Action = decision["action"]

    if action not in _OPENING:
        return ParsedDecision(action=action, intent=None)

    side = _OPENING[action]
    entry_block = decision["entry"]
    risk_plan = decision["risk_plan"]

    entry = _dec(entry_block.get("price"), field="entrada.price")
    stop = _dec(risk_plan.get("stop_loss"), field="stop_loss")
    invalidation = _dec(risk_plan.get("invalidation_price"), field="invalidation_price")

    tp_raw = risk_plan.get("take_profit_levels") or []
    if not tp_raw:
        raise ValueError("ação de abertura sem take-profit")
    tp_levels = tuple(
        TakeProfitLevel(
            price=_dec(lvl.get("price"), field="tp.price"),
            fraction=_dec(lvl.get("close_fraction"), field="tp.close_fraction"),
        )
        for lvl in tp_raw
    )

    claimed_rr = risk_plan.get("estimated_rr_net")
    model_rr = None if claimed_rr is None else _dec(claimed_rr, field="estimated_rr_net")

    expires_at = entry_block.get("expires_at")
    # Sem prazo explícito, dá uma janela curta a partir de agora.
    expires_ms = now_ms + 3_600_000 if expires_at is None else _parse_iso_ms(expires_at)

    intent = TradeIntent(
        decision_id=str(decision["decision_id"]),
        symbol=str(decision["symbol"]),
        side=side,  # type: ignore[arg-type]
        entry=entry,
        stop=stop,
        invalidation=invalidation,
        liquidation=estimate_liquidation(side, entry, max_leverage),
        intent_expires_at_ms=expires_ms,
        take_profit_levels=tp_levels,
        is_averaging_down=False,
        widens_stop=False,
        model_claimed_rr_net=model_rr,
        order_type="Limit" if entry_block.get("type") == "LIMIT" else "Market",
    )
    return ParsedDecision(action=action, intent=intent)


def _parse_iso_ms(iso: str) -> int:
    """ISO-8601 → epoch ms. Aceita o 'Z' de UTC."""
    from datetime import datetime

    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)
