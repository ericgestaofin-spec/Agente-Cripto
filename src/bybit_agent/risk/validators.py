"""Validadores de risco — as regras invioláveis, em código.

Cada regra da spec vira um validador puro. Todos rodam sempre; o
resultado é a lista COMPLETA de motivos de rejeição, com código legível
por máquina (para agregação) e detalhe legível por humano (para o log).

O motor não para no primeiro erro. Um operador que vê "rejeitado por
spread" e conserta o spread não deve descobrir só depois que também havia
conflito de posição. O relatório inteiro sai de uma vez.

Biblioteca pura: os estados (conta, contexto) são snapshots imutáveis
passados de fora. Nada de I/O aqui.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from bybit_agent.risk.policy import RiskPolicy

Side = Literal["BUY", "SELL"]

# Um basis point = 0,0001. Converte fração para bps na comparação.
_BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class AccountState:
    """Snapshot do estado da conta no momento da decisão.

    `equity` deve vir de leitura REST síncrona (D13) — nunca de cache,
    porque o PnL não realizado não gera evento no WS de wallet."""

    equity: Decimal
    daily_pnl: Decimal
    weekly_pnl: Decimal
    open_positions: int
    open_orders: int
    consecutive_losses: int
    entries_today: int
    data_age_ms: int
    spread_bps: Decimal
    estimated_slippage_bps: Decimal
    has_conflicting_position: bool
    has_conflicting_order: bool


@dataclass(frozen=True, slots=True)
class TradeContext:
    """A operação proposta, já traduzida da intenção do modelo."""

    symbol: str
    side: Side
    entry: Decimal
    stop: Decimal
    invalidation: Decimal
    liquidation: Decimal
    rr_net: Decimal
    intent_expires_at_ms: int
    now_ms: int
    take_profit_fractions: list[Decimal]
    is_averaging_down: bool
    widens_stop: bool


@dataclass(frozen=True, slots=True)
class Rejection:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    approved: bool
    rejections: list[Rejection]


# Cada validador recebe (conta, contexto, política) e retorna uma Rejection
# ou None. Assinatura uniforme para poder rodar todos em sequência.
_Validator = Callable[
    [AccountState, TradeContext, RiskPolicy], "Rejection | None"
]


def _daily_loss(a: AccountState, _c: TradeContext, p: RiskPolicy) -> Rejection | None:
    limit = -(a.equity * p.max_daily_loss)
    if a.daily_pnl <= limit:
        return Rejection("DAILY_LOSS_LIMIT", f"PnL diário {a.daily_pnl} <= limite {limit}")
    return None


def _weekly_loss(a: AccountState, _c: TradeContext, p: RiskPolicy) -> Rejection | None:
    limit = -(a.equity * p.max_weekly_loss)
    if a.weekly_pnl <= limit:
        return Rejection("WEEKLY_LOSS_LIMIT", f"PnL semanal {a.weekly_pnl} <= limite {limit}")
    return None


def _projected_daily(a: AccountState, _c: TradeContext, p: RiskPolicy) -> Rejection | None:
    """Preventivo: se este trade, no pior caso, estouraria o limite diário."""
    projected = a.daily_pnl - (a.equity * p.max_risk_per_trade)
    limit = -(a.equity * p.max_daily_loss)
    if projected < limit:
        return Rejection(
            "PROJECTED_DAILY_BREACH",
            f"perda projetada {projected} ultrapassaria o limite diário {limit}",
        )
    return None


def _max_positions(a: AccountState, _c: TradeContext, p: RiskPolicy) -> Rejection | None:
    if a.open_positions >= p.max_concurrent_positions:
        return Rejection(
            "MAX_POSITIONS",
            f"{a.open_positions} posições abertas >= máximo {p.max_concurrent_positions}",
        )
    return None


def _max_entries(a: AccountState, _c: TradeContext, p: RiskPolicy) -> Rejection | None:
    if a.entries_today >= p.max_daily_entries:
        return Rejection(
            "MAX_DAILY_ENTRIES",
            f"{a.entries_today} entradas hoje >= máximo {p.max_daily_entries}",
        )
    return None


def _cooldown(a: AccountState, _c: TradeContext, p: RiskPolicy) -> Rejection | None:
    if a.consecutive_losses >= p.max_consecutive_losses:
        return Rejection(
            "COOLDOWN",
            f"{a.consecutive_losses} perdas consecutivas >= limite {p.max_consecutive_losses}",
        )
    return None


def _symbol(_a: AccountState, c: TradeContext, p: RiskPolicy) -> Rejection | None:
    if c.symbol not in p.allowed_symbols:
        return Rejection("SYMBOL_NOT_ALLOWED", f"símbolo {c.symbol} fora da allowlist")
    return None


def _stale(a: AccountState, _c: TradeContext, p: RiskPolicy) -> Rejection | None:
    # `>` e não `>=`: max_data_age_ms é o máximo PERMITIDO, então idade
    # exatamente no limite é aceita; só acima é stale. Consistente com
    # _spread e _slippage, que são thresholds de mesma natureza.
    if a.data_age_ms > p.max_data_age_ms:
        return Rejection(
            "DATA_STALE", f"dados com {a.data_age_ms}ms > máximo {p.max_data_age_ms}ms"
        )
    return None


def _spread(a: AccountState, _c: TradeContext, p: RiskPolicy) -> Rejection | None:
    if a.spread_bps > p.max_spread_bps:
        return Rejection(
            "SPREAD_TOO_WIDE", f"spread {a.spread_bps}bps > máximo {p.max_spread_bps}bps"
        )
    return None


def _slippage(a: AccountState, _c: TradeContext, p: RiskPolicy) -> Rejection | None:
    if a.estimated_slippage_bps > p.max_slippage_bps:
        return Rejection(
            "SLIPPAGE_TOO_HIGH",
            f"slippage estimado {a.estimated_slippage_bps}bps > máximo {p.max_slippage_bps}bps",
        )
    return None


def _rr(_a: AccountState, c: TradeContext, p: RiskPolicy) -> Rejection | None:
    if c.rr_net < p.min_rr_net:
        return Rejection("RR_TOO_LOW", f"RR líquido {c.rr_net} < mínimo {p.min_rr_net}")
    return None


def _stop_side(_a: AccountState, c: TradeContext, _p: RiskPolicy) -> Rejection | None:
    if c.side == "BUY" and c.stop >= c.entry:
        return Rejection("STOP_WRONG_SIDE", f"LONG com stop {c.stop} >= entrada {c.entry}")
    if c.side == "SELL" and c.stop <= c.entry:
        return Rejection("STOP_WRONG_SIDE", f"SHORT com stop {c.stop} <= entrada {c.entry}")
    return None


def _stop_liquidation(_a: AccountState, c: TradeContext, _p: RiskPolicy) -> Rejection | None:
    """LONG: stop não pode estar em/abaixo da liquidação. SHORT: em/acima."""
    if c.side == "BUY" and c.stop <= c.liquidation:
        return Rejection(
            "STOP_BEYOND_LIQUIDATION",
            f"LONG: stop {c.stop} <= liquidação {c.liquidation}",
        )
    if c.side == "SELL" and c.stop >= c.liquidation:
        return Rejection(
            "STOP_BEYOND_LIQUIDATION",
            f"SHORT: stop {c.stop} >= liquidação {c.liquidation}",
        )
    return None


def _tp_fractions(_a: AccountState, c: TradeContext, _p: RiskPolicy) -> Rejection | None:
    total = sum(c.take_profit_fractions, Decimal("0"))
    if total > 1:
        return Rejection("TP_FRACTIONS_EXCEED_ONE", f"soma das frações de TP {total} > 1")
    return None


def _averaging_down(_a: AccountState, c: TradeContext, _p: RiskPolicy) -> Rejection | None:
    if c.is_averaging_down:
        return Rejection("AVERAGING_DOWN", "média contra posição perdedora é proibida")
    return None


def _stop_widening(_a: AccountState, c: TradeContext, _p: RiskPolicy) -> Rejection | None:
    if c.widens_stop:
        return Rejection("STOP_WIDENING", "ampliar o stop após a entrada é proibido")
    return None


def _conflicting_position(a: AccountState, _c: TradeContext, _p: RiskPolicy) -> Rejection | None:
    if a.has_conflicting_position:
        return Rejection("CONFLICTING_POSITION", "já existe posição conflitante")
    return None


def _conflicting_order(a: AccountState, _c: TradeContext, _p: RiskPolicy) -> Rejection | None:
    if a.has_conflicting_order:
        return Rejection("CONFLICTING_ORDER", "já existe ordem conflitante")
    return None


def _expired(_a: AccountState, c: TradeContext, _p: RiskPolicy) -> Rejection | None:
    if c.intent_expires_at_ms <= c.now_ms:
        return Rejection(
            "INTENT_EXPIRED",
            f"intenção expirou em {c.intent_expires_at_ms}, agora é {c.now_ms}",
        )
    return None


# Ordem estável para relatório e teste determinístico.
_VALIDATORS: tuple[_Validator, ...] = (
    _daily_loss,
    _weekly_loss,
    _projected_daily,
    _max_positions,
    _max_entries,
    _cooldown,
    _symbol,
    _stale,
    _spread,
    _slippage,
    _rr,
    _stop_side,
    _stop_liquidation,
    _tp_fractions,
    _averaging_down,
    _stop_widening,
    _conflicting_position,
    _conflicting_order,
    _expired,
)


def validate(
    account: AccountState, context: TradeContext, policy: RiskPolicy
) -> ValidationOutcome:
    """Roda TODOS os validadores e retorna o relatório completo.

    Determinístico: mesma entrada, mesma lista de rejeições, mesma ordem.
    """
    rejections = [
        r
        for validator in _VALIDATORS
        if (r := validator(account, context, policy)) is not None
    ]
    return ValidationOutcome(approved=not rejections, rejections=rejections)
