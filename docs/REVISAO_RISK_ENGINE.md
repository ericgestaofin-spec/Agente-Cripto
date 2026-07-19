# Pacote de Revisão — Risk Engine

> **Para o revisor (IA ou humano):** este documento é autocontido. Contém
> todo o código-fonte do motor de risco de um agente de trading de
> criptomoedas, seus testes, e o contexto de design. Sua tarefa é uma
> análise crítica de **correção, segurança e completude**. Leia a seção
> "O que revisar" ao final — ela tem perguntas específicas.

## Contexto do sistema

Um agente automatizado de trading opera na Bybit (futuros perpétuos
lineares, BTCUSDT). A arquitetura separa **análise** de **autoridade**:

```
Dados de mercado -> Feature Engine -> Claude Opus 4.8 (analista)
                                            |
                              INTENÇÃO de operação (direção, níveis, tese)
                                            v
                             >>> RISK ENGINE (este código) <<<
                                            |
                              DECISÃO (aprovada + quantidade, ou rejeitada)
                                            v
                                    Execution Gateway -> Bybit
```

**O princípio central:** o modelo de linguagem (Claude) produz uma
*intenção* — direção, preço de entrada, stop, alvos, tese. Ele **nunca**
escolhe o tamanho da posição, a alavancagem, ou qualquer limite de risco.
O Risk Engine é a autoridade determinística e final. Se o Risk Engine
rejeita, a operação não acontece — independentemente do que o modelo
"quer".

Este é o componente onde o dinheiro vive. Um bug aqui não é um bug de
funcionalidade — é uma perda financeira.

## Regras de engenharia (contexto para a revisão)

1. **Dinheiro é sempre `Decimal`, nunca `float`.** `float` não representa
   `0.1` exatamente; um erro de 1e-17 propagado por um cálculo de sizing
   vira uma quantidade que a corretora rejeita ou aceita errada. Um lint
   AST customizado bloqueia `float` em todo o pacote de risco.

2. **Biblioteca pura.** O Risk Engine não faz I/O — sem rede, sem banco,
   sem relógio interno. Todos os estados (conta, mercado, tempo) são
   snapshots imutáveis passados de fora. Isso o torna determinístico e
   testável com property-based testing.

3. **A política é imutável.** `frozen=True` + `slots=True`. Carregada de
   arquivo em disco, nunca de variável de ambiente nem do prompt do
   modelo. Um `policy_hash` (SHA-256) é gravado em cada decisão para
   auditoria da versão da política.

4. **A quantidade nunca vem da intenção.** `TradeIntent` (a entrada do
   engine) não tem campo de quantidade, tamanho ou alavancagem. É
   estruturalmente impossível o modelo influenciar o dimensionamento.

## Invariantes verificadas (property-based testing, 1000+ casos cada)

- **P1** — a perda máxima projetada (qty × risco/unidade) nunca ultrapassa
  o orçamento de risco (patrimônio × fração de risco).
- **P2** — a quantidade é sempre múltipla do `qtyStep` e nunca arredonda
  para cima.
- **P4** — a quantidade nunca excede nenhum teto (alavancagem, liquidez,
  símbolo).
- **P5** — determinismo: mesma entrada, mesma saída, sempre.
- **P7** — quantidade nunca negativa (cobre LONG e SHORT).
- **P9** — reduzir o orçamento de risco nunca aumenta a quantidade.

## Qualidade atual

- 100% de cobertura de branch em `risk/`.
- 100% de mutation score (52/52 mutantes mortos) — mutation testing já
  encontrou e corrigiu um bug real de fronteira (`>=` onde deveria ser `>`
  num check de frescor de dados).
- ~190 funções de teste.

---

# CÓDIGO-FONTE


## `src/bybit_agent/domain/money.py`

```python
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

```

## `src/bybit_agent/domain/instrument.py`

```python
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

```

## `src/bybit_agent/risk/policy.py`

```python
"""Política de risco — a autoridade do sistema.

O requisito central da especificação: **o modelo não pode modificar estes
valores.** Nem por prompt, nem por ferramenta, nem por variável de ambiente.

Três garantias mecânicas:
  1. `frozen=True` + `slots=True` — impossível mutar ou injetar atributo.
  2. Carregada de arquivo em disco, nunca de env var nem da API.
  3. `policy_hash` (SHA-256) vai no event log de toda decisão — se a
     política mudar, é auditável qual decisão usou qual versão.

A imutabilidade não é conveniência de design; é o controle que impede que
um bug (ou um prompt malicioso) afrouxe um limite de risco em runtime.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

# Teto de sanidade: acima disso, é erro de digitação, não estratégia.
# 0,25 no lugar de 0,0025 é o erro que apaga a conta num único trade.
_MAX_PLAUSIBLE_RISK_PER_TRADE = Decimal("0.05")


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """Limites de risco imutáveis. Ver docs/PLANO.md §5.2."""

    max_risk_per_trade: Decimal
    max_total_risk: Decimal
    max_daily_loss: Decimal
    max_weekly_loss: Decimal
    max_concurrent_positions: int
    max_leverage: Decimal
    min_rr_net: Decimal
    max_consecutive_losses: int
    max_daily_entries: int
    max_spread_bps: Decimal
    max_slippage_bps: Decimal
    max_data_age_ms: int
    allowed_symbols: frozenset[str]

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if self.max_risk_per_trade <= 0:
            raise ValueError("max_risk_per_trade deve ser positivo")
        if self.max_risk_per_trade > _MAX_PLAUSIBLE_RISK_PER_TRADE:
            raise ValueError(
                f"max_risk_per_trade implausível ({self.max_risk_per_trade}); "
                f"teto de sanidade é {_MAX_PLAUSIBLE_RISK_PER_TRADE}. "
                f"Verifique se não confundiu 0,25% com 25%."
            )
        if self.max_risk_per_trade > self.max_total_risk:
            raise ValueError(
                "risco por operação não pode exceder o risco total simultâneo"
            )
        if self.max_daily_loss > self.max_weekly_loss:
            raise ValueError("perda diária não pode exceder a perda semanal")
        if self.max_leverage < 1:
            raise ValueError("alavancagem máxima deve ser >= 1")
        if self.min_rr_net < 1:
            raise ValueError("relação risco/retorno mínima deve ser >= 1")
        if self.max_concurrent_positions < 1:
            raise ValueError("max_concurrent_positions deve ser >= 1")
        if not self.allowed_symbols:
            raise ValueError("a lista de símbolos permitidos não pode ser vazia")

    @property
    def policy_hash(self) -> str:
        """SHA-256 determinístico do conjunto de regras.

        Ordenado por nome de campo para ser estável entre execuções.
        Gravado em cada decisão para rastreabilidade da versão da política.
        """
        payload = {
            f.name: sorted(getattr(self, f.name))
            if f.name == "allowed_symbols"
            else str(getattr(self, f.name))
            for f in sorted(fields(self), key=lambda x: x.name)
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def replace(self, **changes: Any) -> RiskPolicy:
        """Retorna uma nova política com os campos alterados.

        A política original permanece intacta — é imutável. Revalida os
        invariantes na construção da nova instância.
        """
        return replace(self, **changes)

    @classmethod
    def conservative_v0(cls) -> RiskPolicy:
        """Valores iniciais conservadores da spec — parâmetros de engenharia
        para validação, não recomendação financeira."""
        return cls(
            max_risk_per_trade=Decimal("0.0025"),
            max_total_risk=Decimal("0.0050"),
            max_daily_loss=Decimal("0.0100"),
            max_weekly_loss=Decimal("0.0300"),
            max_concurrent_positions=1,
            max_leverage=Decimal("2"),
            min_rr_net=Decimal("2.0"),
            max_consecutive_losses=2,
            max_daily_entries=3,
            max_spread_bps=Decimal("5"),
            max_slippage_bps=Decimal("10"),
            max_data_age_ms=5000,
            allowed_symbols=frozenset({"BTCUSDT"}),
        )


def load_policy(path: Path) -> RiskPolicy:
    """Carrega a política de um arquivo JSON em disco.

    Valores numéricos DEVEM ser strings no arquivo — um número JSON vira
    float no parse, e 0.0025 já entraria com erro de representação. A
    detecção de float é explícita e bloqueante.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))

    def dec(key: str) -> Decimal:
        val = raw[key]
        if isinstance(val, float):
            raise TypeError(
                f"política: '{key}' é float ({val}); use string decimal no arquivo"
            )
        return Decimal(str(val))

    return RiskPolicy(
        max_risk_per_trade=dec("max_risk_per_trade"),
        max_total_risk=dec("max_total_risk"),
        max_daily_loss=dec("max_daily_loss"),
        max_weekly_loss=dec("max_weekly_loss"),
        max_concurrent_positions=int(raw["max_concurrent_positions"]),
        max_leverage=dec("max_leverage"),
        min_rr_net=dec("min_rr_net"),
        max_consecutive_losses=int(raw["max_consecutive_losses"]),
        max_daily_entries=int(raw["max_daily_entries"]),
        max_spread_bps=dec("max_spread_bps"),
        max_slippage_bps=dec("max_slippage_bps"),
        max_data_age_ms=int(raw["max_data_age_ms"]),
        allowed_symbols=frozenset(raw["allowed_symbols"]),
    )

```

## `src/bybit_agent/risk/sizing.py`

```python
"""Cálculo de quantidade — o modelo nunca faz isto; este módulo faz.

A quantidade final é o MÍNIMO entre a quantidade permitida pelo risco e
todos os tetos (exposição, alavancagem, liquidez, símbolo), arredondada
SEMPRE para baixo pelo qtyStep. Abaixo do mínimo da corretora → rejeita.

Biblioteca pura: sem I/O, sem estado. `compute_size` é uma função
determinística de suas entradas.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from bybit_agent.domain.instrument import InstrumentSpec, OrderType
from bybit_agent.domain.money import Price, Quantity, round_down_to_step

BindingConstraint = Literal[
    "risk_budget", "leverage", "liquidity", "symbol_max", "none"
]


@dataclass(frozen=True, slots=True)
class SizingInputs:
    equity: Price
    entry: Price
    stop: Price
    risk_fraction: Decimal
    taker_fee_rate: Decimal
    estimated_slippage: Price
    max_leverage: Decimal
    available_liquidity: Quantity
    spec: InstrumentSpec
    order_type: OrderType = "Limit"


@dataclass(frozen=True, slots=True)
class SizingResult:
    approved: bool
    quantity: Quantity | None
    quantity_unrounded: Decimal
    risk_per_unit: Decimal
    risk_budget: Decimal
    binding_constraint: BindingConstraint
    rejection_reason: str = ""


def _rejected(reason: str, *, risk_per_unit: Decimal, budget: Decimal) -> SizingResult:
    return SizingResult(
        approved=False,
        quantity=None,
        quantity_unrounded=Decimal("0"),
        risk_per_unit=risk_per_unit,
        risk_budget=budget,
        binding_constraint="none",
        rejection_reason=reason,
    )


def compute_size(inp: SizingInputs) -> SizingResult:
    """Calcula a quantidade máxima permitida pelo risco e pelos tetos.

    Retorna sempre um SizingResult — aprovação ou rejeição com motivo.
    Nunca levanta exceção por entrada de mercado válida; erros de
    configuração (spec inconsistente) podem levantar.
    """
    budget = inp.equity.value * inp.risk_fraction
    if budget <= 0:
        return _rejected(
            "orçamento de risco é zero ou negativo",
            risk_per_unit=Decimal("0"),
            budget=budget,
        )

    # Distância entrada→stop, sempre absoluta (cobre LONG e SHORT).
    distance = abs(inp.entry.value - inp.stop.value)
    if distance <= 0:
        return _rejected(
            "distância entre entrada e stop é zero",
            risk_per_unit=Decimal("0"),
            budget=budget,
        )

    # Taxa incide na entrada e na saída (ida e volta).
    fee_per_unit = (inp.entry.value + inp.stop.value) * inp.taker_fee_rate
    risk_per_unit = distance + fee_per_unit + inp.estimated_slippage.value

    # Guarda defensiva: com distance > 0 (garantido acima) e taxas/slippage
    # de mercado não-negativas, risk_per_unit é sempre > 0. Só atingível com
    # um rebate de taxa artificialmente negativo — mantida por segurança num
    # cálculo de dinheiro, mas não representa entrada de mercado válida.
    if risk_per_unit <= 0:  # pragma: no cover
        return _rejected(
            "risco por unidade é zero ou negativo",
            risk_per_unit=risk_per_unit,
            budget=budget,
        )

    qty_by_risk = budget / risk_per_unit

    # Tetos — a quantidade final é o menor de todos.
    max_notional = inp.equity.value * inp.max_leverage
    qty_by_leverage = max_notional / inp.entry.value
    qty_by_liquidity = inp.available_liquidity.value
    qty_by_symbol = inp.spec.max_qty_for(order_type=inp.order_type)

    caps: list[tuple[BindingConstraint, Decimal]] = [
        ("risk_budget", qty_by_risk),
        ("leverage", qty_by_leverage),
        ("liquidity", qty_by_liquidity),
        ("symbol_max", qty_by_symbol),
    ]
    binding, unrounded = min(caps, key=lambda c: c[1])

    rounded = round_down_to_step(Quantity(unrounded), inp.spec.qty_step)

    # Abaixo do mínimo da corretora → rejeita. NUNCA arredonda para cima.
    if rounded.value < inp.spec.min_order_qty:
        return _rejected(
            f"quantidade {rounded.value} abaixo do mínimo da corretora "
            f"({inp.spec.min_order_qty})",
            risk_per_unit=risk_per_unit,
            budget=budget,
        )

    notional = rounded.value * inp.entry.value
    if notional < inp.spec.min_notional:
        return _rejected(
            f"notional {notional} abaixo do mínimo ({inp.spec.min_notional})",
            risk_per_unit=risk_per_unit,
            budget=budget,
        )

    return SizingResult(
        approved=True,
        quantity=rounded,
        quantity_unrounded=unrounded,
        risk_per_unit=risk_per_unit,
        risk_budget=budget,
        binding_constraint=binding,
    )

```

## `src/bybit_agent/risk/validators.py`

```python
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

```

## `src/bybit_agent/risk/engine.py`

```python
"""Risk Engine — a autoridade final sobre toda operação.

Recebe uma `TradeIntent` (traduzida da decisão do modelo) e o estado da
conta, e produz uma `RiskDecision`: aprovada com quantidade calculada, ou
rejeitada com a lista completa de motivos.

Duas garantias que definem a arquitetura:

  1. **A quantidade nunca vem da intenção.** `TradeIntent` não tem campo
     de quantidade, tamanho ou alavancagem — não há como o modelo
     influenciar o dimensionamento. O tamanho é sempre calculado aqui.

  2. **Validação antes de sizing.** Se qualquer regra rejeita, o sizing
     não roda. Um trade proibido não recebe dimensionamento.

Biblioteca pura: sem I/O, sem estado, sem relógio interno (o `now_ms` é
injetado). Determinística por construção.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from bybit_agent.domain.instrument import InstrumentSpec, OrderType
from bybit_agent.domain.money import Price, Quantity
from bybit_agent.risk.policy import RiskPolicy
from bybit_agent.risk.sizing import BindingConstraint, SizingInputs, compute_size
from bybit_agent.risk.validators import (
    AccountState,
    Rejection,
    Side,
    TradeContext,
    validate,
)


@dataclass(frozen=True, slots=True)
class TradeIntent:
    """A intenção do modelo, já traduzida para tipos de domínio.

    Repare no que NÃO existe aqui: quantidade, tamanho, alavancagem,
    margem. O modelo propõe direção, níveis e tese — nunca o tamanho.
    """

    decision_id: str
    symbol: str
    side: Side
    entry: Decimal
    stop: Decimal
    invalidation: Decimal
    liquidation: Decimal
    rr_net: Decimal
    intent_expires_at_ms: int
    take_profit_fractions: list[Decimal]
    is_averaging_down: bool
    widens_stop: bool
    order_type: OrderType = "Limit"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    quantity: Quantity | None
    rejections: list[Rejection]
    policy_hash: str
    risk_budget: Decimal | None = None
    risk_per_unit: Decimal | None = None
    binding_constraint: BindingConstraint | None = None


def evaluate(
    intent: TradeIntent,
    account: AccountState,
    *,
    spec: InstrumentSpec,
    policy: RiskPolicy,
    taker_fee_rate: Decimal,
    estimated_slippage: Decimal,
    available_liquidity: Decimal,
    now_ms: int,
) -> RiskDecision:
    """Avalia a intenção contra as regras e calcula a quantidade.

    Retorna sempre uma RiskDecision — nunca levanta por intenção de
    mercado válida.
    """
    # 1. Validação primeiro. Trade proibido não recebe dimensionamento.
    context = TradeContext(
        symbol=intent.symbol,
        side=intent.side,
        entry=intent.entry,
        stop=intent.stop,
        invalidation=intent.invalidation,
        liquidation=intent.liquidation,
        rr_net=intent.rr_net,
        intent_expires_at_ms=intent.intent_expires_at_ms,
        now_ms=now_ms,
        take_profit_fractions=intent.take_profit_fractions,
        is_averaging_down=intent.is_averaging_down,
        widens_stop=intent.widens_stop,
    )
    outcome = validate(account, context, policy)
    if not outcome.approved:
        return RiskDecision(
            approved=False,
            quantity=None,
            rejections=outcome.rejections,
            policy_hash=policy.policy_hash,
        )

    # 2. Sizing. A quantidade é calculada aqui, nunca fornecida.
    sizing = compute_size(
        SizingInputs(
            equity=Price(account.equity),
            entry=Price(intent.entry),
            stop=Price(intent.stop),
            risk_fraction=policy.max_risk_per_trade,
            taker_fee_rate=taker_fee_rate,
            estimated_slippage=Price(estimated_slippage),
            max_leverage=policy.max_leverage,
            available_liquidity=Quantity(available_liquidity),
            spec=spec,
            order_type=intent.order_type,
        )
    )
    if not sizing.approved:
        return RiskDecision(
            approved=False,
            quantity=None,
            rejections=[Rejection("UNSIZABLE", sizing.rejection_reason)],
            policy_hash=policy.policy_hash,
        )

    return RiskDecision(
        approved=True,
        quantity=sizing.quantity,
        rejections=[],
        policy_hash=policy.policy_hash,
        risk_budget=sizing.risk_budget,
        risk_per_unit=sizing.risk_per_unit,
        binding_constraint=sizing.binding_constraint,
    )

```


---

# TESTES


Os testes documentam o comportamento esperado. Um revisor pode usá-los para entender a intenção — e para achar o que eles NÃO cobrem.


## `tests/unit/risk/test_policy_and_instrument.py`

```python
"""Sprint 4a — política de risco e especificação do instrumento.

A política é a autoridade do sistema. O requisito central da spec é que
o modelo não possa modificá-la — nem por prompt, nem por ferramenta, nem
por variável de ambiente. Estes testes tornam isso mecânico.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from bybit_agent.domain.instrument import InstrumentSpec
from bybit_agent.risk.policy import RiskPolicy, load_policy

# --------------------------------------------------------------------------
# InstrumentSpec — espelha /v5/market/instruments-info
# --------------------------------------------------------------------------


def _bybit_payload() -> dict[str, object]:
    """Resposta real da Bybit para BTCUSDT linear (formato verificado)."""
    return {
        "symbol": "BTCUSDT",
        "status": "Trading",
        "priceFilter": {"tickSize": "0.10", "minPrice": "0.10", "maxPrice": "999999.00"},
        "lotSizeFilter": {
            "qtyStep": "0.001",
            "minOrderQty": "0.001",
            "maxOrderQty": "500.000",
            "maxMktOrderQty": "100.000",
            "minNotionalValue": "5",
        },
        "leverageFilter": {"minLeverage": "1", "maxLeverage": "100.00"},
    }


def test_parses_bybit_instruments_info_payload() -> None:
    spec = InstrumentSpec.from_bybit(_bybit_payload())
    assert spec.symbol == "BTCUSDT"
    assert spec.tick_size == Decimal("0.10")
    assert spec.qty_step == Decimal("0.001")
    assert spec.min_order_qty == Decimal("0.001")
    assert spec.min_notional == Decimal("5")
    assert spec.max_leverage == Decimal("100.00")


def test_market_and_limit_max_qty_are_distinct() -> None:
    """maxOrderQty (limit) e maxMktOrderQty (market) são limites diferentes.
    Confundi-los faz o sizing propor quantidade que a Bybit rejeita."""
    spec = InstrumentSpec.from_bybit(_bybit_payload())
    assert spec.max_order_qty == Decimal("500.000")
    assert spec.max_market_order_qty == Decimal("100.000")
    assert spec.max_qty_for(order_type="Limit") == Decimal("500.000")
    assert spec.max_qty_for(order_type="Market") == Decimal("100.000")


def test_all_numeric_fields_are_decimal() -> None:
    spec = InstrumentSpec.from_bybit(_bybit_payload())
    for name in (
        "tick_size",
        "qty_step",
        "min_order_qty",
        "max_order_qty",
        "max_market_order_qty",
        "min_notional",
        "max_leverage",
    ):
        assert isinstance(getattr(spec, name), Decimal), name


def test_instrument_is_immutable() -> None:
    spec = InstrumentSpec.from_bybit(_bybit_payload())
    with pytest.raises((FrozenInstanceError, AttributeError)):
        spec.tick_size = Decimal("1")  # type: ignore[misc]


def test_non_trading_status_is_rejected() -> None:
    """Símbolo suspenso não pode virar spec utilizável — a spec da Bybit
    continua vindo, mas operar nela é o circuit breaker 110074."""
    payload = _bybit_payload()
    payload["status"] = "Closed"
    with pytest.raises(ValueError, match="não está negociando"):
        InstrumentSpec.from_bybit(payload)


def test_missing_nested_field_fails_loudly() -> None:
    """Campo ausente vira erro explícito, nunca default silencioso.
    Um qty_step default de 0.001 num símbolo que usa 0.01 produz
    quantidades rejeitadas pela corretora."""
    payload = _bybit_payload()
    del payload["lotSizeFilter"]  # type: ignore[arg-type]
    with pytest.raises((KeyError, ValueError)):
        InstrumentSpec.from_bybit(payload)


# --------------------------------------------------------------------------
# RiskPolicy — imutabilidade é o requisito central
# --------------------------------------------------------------------------


def test_policy_has_conservative_defaults_from_spec() -> None:
    p = RiskPolicy.conservative_v0()
    assert p.max_risk_per_trade == Decimal("0.0025")  # 0,25%
    assert p.max_total_risk == Decimal("0.0050")
    assert p.max_daily_loss == Decimal("0.0100")
    assert p.max_weekly_loss == Decimal("0.0300")
    assert p.max_concurrent_positions == 1
    assert p.max_leverage == Decimal("2")
    assert p.min_rr_net == Decimal("2.0")
    assert p.max_consecutive_losses == 2
    assert p.max_daily_entries == 3
    assert p.allowed_symbols == frozenset({"BTCUSDT"})


def test_policy_is_frozen() -> None:
    """⭐ Nenhum caminho de código pode mutar a política em runtime."""
    p = RiskPolicy.conservative_v0()
    with pytest.raises((FrozenInstanceError, AttributeError)):
        p.max_risk_per_trade = Decimal("0.10")  # type: ignore[misc]


def test_policy_rejects_new_attributes() -> None:
    """slots=True impede injetar atributo novo para burlar validação."""
    p = RiskPolicy.conservative_v0()
    with pytest.raises((AttributeError, TypeError)):
        p.bypass_all_limits = True  # type: ignore[attr-defined]


def test_policy_allowed_symbols_is_immutable_collection() -> None:
    """set mutável permitiria adicionar símbolo em runtime."""
    p = RiskPolicy.conservative_v0()
    assert isinstance(p.allowed_symbols, frozenset)
    with pytest.raises(AttributeError):
        p.allowed_symbols.add("ETHUSDT")  # type: ignore[attr-defined]


def test_policy_hash_identifies_the_ruleset() -> None:
    """⭐ O hash vai no event log de toda decisão. Se a política mudar,
    é auditável qual decisão usou qual versão."""
    p = RiskPolicy.conservative_v0()
    assert len(p.policy_hash) == 64  # sha256 hex
    assert p.policy_hash == RiskPolicy.conservative_v0().policy_hash


def test_policy_hash_changes_when_any_limit_changes() -> None:
    base = RiskPolicy.conservative_v0()
    tighter = base.replace(max_risk_per_trade=Decimal("0.0010"))
    assert tighter.policy_hash != base.policy_hash


def test_replace_returns_new_instance_without_mutating() -> None:
    base = RiskPolicy.conservative_v0()
    original = base.max_risk_per_trade
    tighter = base.replace(max_risk_per_trade=Decimal("0.0010"))
    assert base.max_risk_per_trade == original
    assert tighter.max_risk_per_trade == Decimal("0.0010")


# -- validação de sanidade dos próprios limites ----------------------------


def test_rejects_risk_per_trade_above_total_risk() -> None:
    with pytest.raises(ValueError, match="risco por opera"):
        RiskPolicy.conservative_v0().replace(
            max_risk_per_trade=Decimal("0.02"), max_total_risk=Decimal("0.01")
        )


def test_rejects_daily_loss_above_weekly_loss() -> None:
    with pytest.raises(ValueError, match="di"):
        RiskPolicy.conservative_v0().replace(
            max_daily_loss=Decimal("0.05"), max_weekly_loss=Decimal("0.03")
        )


def test_rejects_non_positive_risk() -> None:
    with pytest.raises(ValueError):
        RiskPolicy.conservative_v0().replace(max_risk_per_trade=Decimal("0"))


def test_rejects_absurd_risk_per_trade() -> None:
    """Teto rígido de 5% — acima disso é erro de digitação, não estratégia.
    0,25 em vez de 0,0025 é o erro que apaga a conta."""
    with pytest.raises(ValueError, match="implausível"):
        RiskPolicy.conservative_v0().replace(max_risk_per_trade=Decimal("0.25"))


def test_rejects_leverage_below_one() -> None:
    with pytest.raises(ValueError):
        RiskPolicy.conservative_v0().replace(max_leverage=Decimal("0.5"))


def test_rejects_rr_below_one() -> None:
    """RR < 1 significa arriscar mais do que se busca ganhar."""
    with pytest.raises(ValueError, match="risco/retorno"):
        RiskPolicy.conservative_v0().replace(min_rr_net=Decimal("0.8"))


def test_rejects_empty_symbol_allowlist() -> None:
    with pytest.raises(ValueError, match="símbolo"):
        RiskPolicy.conservative_v0().replace(allowed_symbols=frozenset())


def test_rejects_zero_concurrent_positions() -> None:
    """max_concurrent_positions < 1 tornaria o sistema incapaz de operar."""
    with pytest.raises(ValueError, match="max_concurrent_positions"):
        RiskPolicy.conservative_v0().replace(max_concurrent_positions=0)


# -- carregamento -----------------------------------------------------------


def test_load_policy_reads_from_file(tmp_path: object) -> None:
    """A política vem de arquivo em disco, não de env var."""
    import json
    from pathlib import Path

    path = Path(str(tmp_path)) / "policy.json"
    path.write_text(
        json.dumps(
            {
                "max_risk_per_trade": "0.0025",
                "max_total_risk": "0.0050",
                "max_daily_loss": "0.0100",
                "max_weekly_loss": "0.0300",
                "max_concurrent_positions": 1,
                "max_leverage": "2",
                "min_rr_net": "2.0",
                "max_consecutive_losses": 2,
                "max_daily_entries": 3,
                "max_spread_bps": "5",
                "max_slippage_bps": "10",
                "max_data_age_ms": 5000,
                "allowed_symbols": ["BTCUSDT"],
            }
        ),
        encoding="utf-8",
    )
    policy = load_policy(path)
    assert policy.max_risk_per_trade == Decimal("0.0025")
    assert policy.allowed_symbols == frozenset({"BTCUSDT"})


def test_load_policy_rejects_float_in_file() -> None:
    """⭐ Números JSON viram float no parse. A política tem que usar
    strings decimais, senão 0.0025 já entra com erro de representação."""
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "policy.json"
        path.write_text(json.dumps({"max_risk_per_trade": 0.0025}), encoding="utf-8")
        with pytest.raises((TypeError, ValueError)):
            load_policy(path)

```

## `tests/unit/risk/test_sizing.py`

```python
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

```

## `tests/unit/risk/test_validators.py`

```python
"""Sprint 4c — validadores de risco.

Cada validador é uma regra de rejeição da spec (§ REGRAS INVIOLÁVEIS e
§ Motor determinístico de risco). Testados isolados e depois compostos.

Princípio: TODOS os validadores rodam mesmo quando o primeiro falha —
o relatório de rejeição é completo, não para no primeiro erro. Isso dá
ao operador (e ao event log) a lista inteira de motivos.
"""

from __future__ import annotations

from decimal import Decimal

from bybit_agent.risk.policy import RiskPolicy
from bybit_agent.risk.validators import (
    AccountState,
    TradeContext,
    ValidationOutcome,
    validate,
)


def _account(**over: object) -> AccountState:
    base: dict[str, object] = {
        "equity": Decimal("100000"),
        "daily_pnl": Decimal("0"),
        "weekly_pnl": Decimal("0"),
        "open_positions": 0,
        "open_orders": 0,
        "consecutive_losses": 0,
        "entries_today": 0,
        "data_age_ms": 100,
        "spread_bps": Decimal("2"),
        "estimated_slippage_bps": Decimal("4"),
        "has_conflicting_position": False,
        "has_conflicting_order": False,
    }
    base.update(over)
    return AccountState(**base)  # type: ignore[arg-type]


def _ctx(**over: object) -> TradeContext:
    base: dict[str, object] = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "entry": Decimal("60000"),
        "stop": Decimal("59400"),
        "invalidation": Decimal("59500"),
        "liquidation": Decimal("30000"),
        "rr_net": Decimal("2.5"),
        "intent_expires_at_ms": 9_999_999_999_999,
        "now_ms": 1_000_000_000_000,
        "take_profit_fractions": [Decimal("0.5"), Decimal("0.5")],
        "is_averaging_down": False,
        "widens_stop": False,
    }
    base.update(over)
    return TradeContext(**base)  # type: ignore[arg-type]


def _codes(outcome: ValidationOutcome) -> set[str]:
    return {r.code for r in outcome.rejections}


# --------------------------------------------------------------------------
# Caminho feliz
# --------------------------------------------------------------------------


def test_valid_trade_passes_all_validators() -> None:
    out = validate(_account(), _ctx(), RiskPolicy.conservative_v0())
    assert out.approved
    assert out.rejections == []


# --------------------------------------------------------------------------
# Limites de perda
# --------------------------------------------------------------------------


def test_reject_when_daily_loss_limit_reached() -> None:
    out = validate(
        _account(daily_pnl=Decimal("-1000")),  # -1% de 100k
        _ctx(),
        RiskPolicy.conservative_v0(),
    )
    assert not out.approved
    assert "DAILY_LOSS_LIMIT" in _codes(out)


def test_reject_when_weekly_loss_limit_reached() -> None:
    out = validate(
        _account(weekly_pnl=Decimal("-3000")),  # -3%
        _ctx(),
        RiskPolicy.conservative_v0(),
    )
    assert "WEEKLY_LOSS_LIMIT" in _codes(out)


def test_reject_when_projected_loss_would_breach_daily_limit() -> None:
    """⭐ Preventivo: se a perda projetada deste trade estouraria o limite
    diário, rejeita ANTES de enviar, não depois."""
    # já perdeu 0,9%; um trade arriscando 0,25% levaria a 1,15% > 1%
    out = validate(
        _account(daily_pnl=Decimal("-900")),
        _ctx(),
        RiskPolicy.conservative_v0(),
    )
    assert "PROJECTED_DAILY_BREACH" in _codes(out)


# --------------------------------------------------------------------------
# Exposição e frequência
# --------------------------------------------------------------------------


def test_reject_when_max_positions_reached() -> None:
    out = validate(_account(open_positions=1), _ctx(), RiskPolicy.conservative_v0())
    assert "MAX_POSITIONS" in _codes(out)


def test_reject_when_daily_entry_count_exceeded() -> None:
    out = validate(_account(entries_today=3), _ctx(), RiskPolicy.conservative_v0())
    assert "MAX_DAILY_ENTRIES" in _codes(out)


def test_reject_when_consecutive_losses_trigger_cooldown() -> None:
    out = validate(
        _account(consecutive_losses=2), _ctx(), RiskPolicy.conservative_v0()
    )
    assert "COOLDOWN" in _codes(out)


# --------------------------------------------------------------------------
# Símbolo, dados, mercado
# --------------------------------------------------------------------------


def test_reject_when_symbol_not_in_allowlist() -> None:
    out = validate(_account(), _ctx(symbol="ETHUSDT"), RiskPolicy.conservative_v0())
    assert "SYMBOL_NOT_ALLOWED" in _codes(out)


def test_reject_when_data_is_stale() -> None:
    out = validate(
        _account(data_age_ms=6000), _ctx(), RiskPolicy.conservative_v0()
    )
    assert "DATA_STALE" in _codes(out)


def test_reject_when_spread_above_limit() -> None:
    out = validate(
        _account(spread_bps=Decimal("10")), _ctx(), RiskPolicy.conservative_v0()
    )
    assert "SPREAD_TOO_WIDE" in _codes(out)


def test_reject_when_slippage_above_limit() -> None:
    out = validate(
        _account(estimated_slippage_bps=Decimal("20")),
        _ctx(),
        RiskPolicy.conservative_v0(),
    )
    assert "SLIPPAGE_TOO_HIGH" in _codes(out)


# --------------------------------------------------------------------------
# Qualidade do trade
# --------------------------------------------------------------------------


def test_reject_when_rr_net_below_minimum() -> None:
    out = validate(
        _account(), _ctx(rr_net=Decimal("1.5")), RiskPolicy.conservative_v0()
    )
    assert "RR_TOO_LOW" in _codes(out)


def test_reject_when_stop_on_wrong_side_for_long() -> None:
    """LONG com stop acima da entrada é incoerente."""
    out = validate(
        _account(),
        _ctx(side="BUY", entry=Decimal("60000"), stop=Decimal("60500")),
        RiskPolicy.conservative_v0(),
    )
    assert "STOP_WRONG_SIDE" in _codes(out)


def test_reject_when_stop_on_wrong_side_for_short() -> None:
    out = validate(
        _account(),
        _ctx(
            side="SELL",
            entry=Decimal("60000"),
            stop=Decimal("59500"),
            invalidation=Decimal("60500"),
            liquidation=Decimal("90000"),
        ),
        RiskPolicy.conservative_v0(),
    )
    assert "STOP_WRONG_SIDE" in _codes(out)


def test_reject_when_stop_beyond_liquidation() -> None:
    """⭐ Um stop além do preço de liquidação é ficção — a posição é
    liquidada antes de o stop disparar."""
    out = validate(
        _account(),
        _ctx(side="BUY", entry=Decimal("60000"), stop=Decimal("29000"),
             liquidation=Decimal("30000"), invalidation=Decimal("29500")),
        RiskPolicy.conservative_v0(),
    )
    assert "STOP_BEYOND_LIQUIDATION" in _codes(out)


def test_reject_when_short_stop_beyond_liquidation() -> None:
    """SHORT: stop acima do preço de liquidação é ficção."""
    out = validate(
        _account(),
        _ctx(side="SELL", entry=Decimal("60000"), stop=Decimal("91000"),
             liquidation=Decimal("90000"), invalidation=Decimal("60500")),
        RiskPolicy.conservative_v0(),
    )
    assert "STOP_BEYOND_LIQUIDATION" in _codes(out)


def test_reject_when_take_profit_fractions_exceed_one() -> None:
    out = validate(
        _account(),
        _ctx(take_profit_fractions=[Decimal("0.6"), Decimal("0.6")]),
        RiskPolicy.conservative_v0(),
    )
    assert "TP_FRACTIONS_EXCEED_ONE" in _codes(out)


# --------------------------------------------------------------------------
# Anti-martingale
# --------------------------------------------------------------------------


def test_reject_when_averaging_down_detected() -> None:
    """⭐ Média contra posição perdedora é proibida pela spec."""
    out = validate(
        _account(), _ctx(is_averaging_down=True), RiskPolicy.conservative_v0()
    )
    assert "AVERAGING_DOWN" in _codes(out)


def test_reject_when_stop_widening_detected() -> None:
    """⭐ Ampliar o stop após a entrada aumenta a perda máxima. Proibido."""
    out = validate(
        _account(), _ctx(widens_stop=True), RiskPolicy.conservative_v0()
    )
    assert "STOP_WIDENING" in _codes(out)


# --------------------------------------------------------------------------
# Conflitos e validade
# --------------------------------------------------------------------------


def test_reject_when_conflicting_position_exists() -> None:
    out = validate(
        _account(has_conflicting_position=True), _ctx(), RiskPolicy.conservative_v0()
    )
    assert "CONFLICTING_POSITION" in _codes(out)


def test_reject_when_conflicting_order_exists() -> None:
    out = validate(
        _account(has_conflicting_order=True), _ctx(), RiskPolicy.conservative_v0()
    )
    assert "CONFLICTING_ORDER" in _codes(out)


def test_reject_when_intent_expired() -> None:
    out = validate(
        _account(),
        _ctx(intent_expires_at_ms=999, now_ms=1000),
        RiskPolicy.conservative_v0(),
    )
    assert "INTENT_EXPIRED" in _codes(out)


# --------------------------------------------------------------------------
# Relatório completo
# --------------------------------------------------------------------------


def test_all_validators_run_even_when_first_fails() -> None:
    """⭐ Múltiplas violações simultâneas produzem TODOS os códigos, não
    só o primeiro. O relatório é completo."""
    out = validate(
        _account(
            daily_pnl=Decimal("-2000"),
            open_positions=1,
            spread_bps=Decimal("50"),
        ),
        _ctx(symbol="DOGEUSDT", rr_net=Decimal("0.5")),
        RiskPolicy.conservative_v0(),
    )
    codes = _codes(out)
    assert {
        "DAILY_LOSS_LIMIT",
        "MAX_POSITIONS",
        "SPREAD_TOO_WIDE",
        "SYMBOL_NOT_ALLOWED",
        "RR_TOO_LOW",
    } <= codes


def test_rejection_reason_is_machine_readable_code() -> None:
    out = validate(_account(open_positions=1), _ctx(), RiskPolicy.conservative_v0())
    for r in out.rejections:
        assert r.code.isupper()
        assert " " not in r.code
        assert r.detail  # mensagem legível para humano acompanha o código


def test_validate_is_deterministic() -> None:
    acc, ctx, pol = _account(open_positions=1), _ctx(), RiskPolicy.conservative_v0()
    assert validate(acc, ctx, pol) == validate(acc, ctx, pol)

```

## `tests/unit/risk/test_boundaries.py`

```python
"""Sprint 4 — testes de fronteira (limites exatos).

Mutation testing revelou que a suite verificava os dois lados de cada
limite, mas nunca o valor EXATAMENTE no limite. É ali que moram os bugs
de off-by-one — e num motor de risco, um off-by-one num limite é a
diferença entre aceitar e recusar uma operação.

Cada teste aqui pina uma decisão de política:
  - Limites de MAGNITUDE (risco, spread, slippage, data_age, RR): o valor
    da política é o MÁXIMO/MÍNIMO PERMITIDO. Exatamente no limite → aceita;
    além → rejeita.
  - Limites de CONTAGEM (posições, entradas, perdas): o valor é o teto
    atingível. Exatamente no teto → rejeita (você está cheio).

Estes testes também são o que mantém o mutation score do `risk/` alto.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bybit_agent.domain.instrument import InstrumentSpec
from bybit_agent.domain.money import Price, Quantity
from bybit_agent.risk.policy import RiskPolicy
from bybit_agent.risk.sizing import SizingInputs, compute_size
from bybit_agent.risk.validators import AccountState, TradeContext, validate


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


def _account(**over: object) -> AccountState:
    base: dict[str, object] = {
        "equity": Decimal("100000"),
        "daily_pnl": Decimal("0"),
        "weekly_pnl": Decimal("0"),
        "open_positions": 0,
        "open_orders": 0,
        "consecutive_losses": 0,
        "entries_today": 0,
        "data_age_ms": 100,
        "spread_bps": Decimal("2"),
        "estimated_slippage_bps": Decimal("4"),
        "has_conflicting_position": False,
        "has_conflicting_order": False,
    }
    base.update(over)
    return AccountState(**base)  # type: ignore[arg-type]


def _ctx(**over: object) -> TradeContext:
    base: dict[str, object] = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "entry": Decimal("60000"),
        "stop": Decimal("59400"),
        "invalidation": Decimal("59500"),
        "liquidation": Decimal("30000"),
        "rr_net": Decimal("2.5"),
        "intent_expires_at_ms": 9_999_999_999_999,
        "now_ms": 1_000_000_000_000,
        "take_profit_fractions": [Decimal("0.5"), Decimal("0.5")],
        "is_averaging_down": False,
        "widens_stop": False,
    }
    base.update(over)
    return TradeContext(**base)  # type: ignore[arg-type]


def _codes(acc: AccountState, ctx: TradeContext) -> set[str]:
    return {r.code for r in validate(acc, ctx, RiskPolicy.conservative_v0()).rejections}


P = RiskPolicy.conservative_v0()


# ==========================================================================
# Validadores — limites de MAGNITUDE: no limite aceita, além rejeita
# ==========================================================================


def test_daily_loss_exactly_at_limit_rejects() -> None:
    """PnL exatamente no limite diário JÁ rejeita (<=)."""
    limit = -(Decimal("100000") * P.max_daily_loss)  # -1000
    assert "DAILY_LOSS_LIMIT" in _codes(_account(daily_pnl=limit), _ctx())


def test_daily_loss_one_cent_above_limit_passes() -> None:
    limit = -(Decimal("100000") * P.max_daily_loss)
    assert "DAILY_LOSS_LIMIT" not in _codes(
        _account(daily_pnl=limit + Decimal("0.01")), _ctx()
    )


def test_weekly_loss_exactly_at_limit_rejects() -> None:
    limit = -(Decimal("100000") * P.max_weekly_loss)  # -3000
    assert "WEEKLY_LOSS_LIMIT" in _codes(_account(weekly_pnl=limit), _ctx())


def test_spread_exactly_at_max_is_allowed() -> None:
    """Spread == máximo é aceito; só ACIMA rejeita (>)."""
    assert "SPREAD_TOO_WIDE" not in _codes(
        _account(spread_bps=P.max_spread_bps), _ctx()
    )


def test_spread_one_bp_above_max_rejects() -> None:
    assert "SPREAD_TOO_WIDE" in _codes(
        _account(spread_bps=P.max_spread_bps + Decimal("0.01")), _ctx()
    )


def test_slippage_exactly_at_max_is_allowed() -> None:
    assert "SLIPPAGE_TOO_HIGH" not in _codes(
        _account(estimated_slippage_bps=P.max_slippage_bps), _ctx()
    )


def test_slippage_above_max_rejects() -> None:
    assert "SLIPPAGE_TOO_HIGH" in _codes(
        _account(estimated_slippage_bps=P.max_slippage_bps + Decimal("0.01")), _ctx()
    )


def test_data_age_exactly_at_max_is_allowed() -> None:
    """⭐ Bug corrigido: idade == máximo é aceita (>), não rejeitada (>=).
    A mensagem já dizia '> máximo' — o código discordava dela."""
    assert "DATA_STALE" not in _codes(_account(data_age_ms=P.max_data_age_ms), _ctx())


def test_data_age_one_ms_above_max_rejects() -> None:
    assert "DATA_STALE" in _codes(_account(data_age_ms=P.max_data_age_ms + 1), _ctx())


def test_rr_exactly_at_minimum_is_allowed() -> None:
    """RR == mínimo é aceito; só ABAIXO rejeita (<). min_rr_net=2.0."""
    assert "RR_TOO_LOW" not in _codes(_account(), _ctx(rr_net=P.min_rr_net))


def test_rr_just_below_minimum_rejects() -> None:
    assert "RR_TOO_LOW" in _codes(
        _account(), _ctx(rr_net=P.min_rr_net - Decimal("0.01"))
    )


# ==========================================================================
# Validadores — limites de CONTAGEM: no teto rejeita
# ==========================================================================


def test_positions_exactly_at_max_rejects() -> None:
    assert "MAX_POSITIONS" in _codes(
        _account(open_positions=P.max_concurrent_positions), _ctx()
    )


def test_entries_exactly_at_max_rejects() -> None:
    assert "MAX_DAILY_ENTRIES" in _codes(
        _account(entries_today=P.max_daily_entries), _ctx()
    )


def test_consecutive_losses_exactly_at_max_rejects() -> None:
    assert "COOLDOWN" in _codes(
        _account(consecutive_losses=P.max_consecutive_losses), _ctx()
    )


# ==========================================================================
# Stop — fronteiras de igualdade (stop == entrada, stop == liquidação)
# ==========================================================================


def test_long_stop_exactly_at_entry_rejects() -> None:
    """Stop igual à entrada é incoerente — risco zero, não é operação."""
    assert "STOP_WRONG_SIDE" in _codes(
        _account(), _ctx(side="BUY", entry=Decimal("60000"), stop=Decimal("60000"))
    )


def test_short_stop_exactly_at_entry_rejects() -> None:
    assert "STOP_WRONG_SIDE" in _codes(
        _account(),
        _ctx(side="SELL", entry=Decimal("60000"), stop=Decimal("60000"),
             invalidation=Decimal("60500"), liquidation=Decimal("90000")),
    )


def test_long_valid_stop_below_entry_does_not_trigger_wrong_side() -> None:
    """Contraparte: LONG com stop abaixo da entrada é válido — garante que
    o operador de lado (side == 'BUY') não foi trocado."""
    assert "STOP_WRONG_SIDE" not in _codes(
        _account(), _ctx(side="BUY", entry=Decimal("60000"), stop=Decimal("59000"))
    )


def test_short_valid_stop_above_entry_does_not_trigger_wrong_side() -> None:
    assert "STOP_WRONG_SIDE" not in _codes(
        _account(),
        _ctx(side="SELL", entry=Decimal("60000"), stop=Decimal("61000"),
             invalidation=Decimal("60500"), liquidation=Decimal("90000")),
    )


def test_long_stop_exactly_at_liquidation_rejects() -> None:
    assert "STOP_BEYOND_LIQUIDATION" in _codes(
        _account(),
        _ctx(side="BUY", entry=Decimal("60000"), stop=Decimal("30000"),
             liquidation=Decimal("30000"), invalidation=Decimal("30500")),
    )


def test_short_stop_exactly_at_liquidation_rejects() -> None:
    assert "STOP_BEYOND_LIQUIDATION" in _codes(
        _account(),
        _ctx(side="SELL", entry=Decimal("60000"), stop=Decimal("90000"),
             liquidation=Decimal("90000"), invalidation=Decimal("60500")),
    )


def test_long_stop_just_above_liquidation_is_allowed() -> None:
    assert "STOP_BEYOND_LIQUIDATION" not in _codes(
        _account(),
        _ctx(side="BUY", entry=Decimal("60000"), stop=Decimal("30001"),
             liquidation=Decimal("30000"), invalidation=Decimal("30500")),
    )


# ==========================================================================
# TP fractions — soma exatamente 1 é permitida
# ==========================================================================


def test_intent_expiring_exactly_now_rejects() -> None:
    """Intenção que expira EXATAMENTE no instante da avaliação já expirou
    (<=). Um sinal na fronteira temporal não pode ser tratado como válido."""
    assert "INTENT_EXPIRED" in _codes(
        _account(), _ctx(intent_expires_at_ms=1_000_000_000_000,
                          now_ms=1_000_000_000_000)
    )


def test_intent_expiring_one_ms_ahead_is_valid() -> None:
    assert "INTENT_EXPIRED" not in _codes(
        _account(), _ctx(intent_expires_at_ms=1_000_000_000_001,
                          now_ms=1_000_000_000_000)
    )


def test_tp_fractions_summing_exactly_one_is_allowed() -> None:
    """Soma == 1 (fechar 100% da posição) é válido; só ACIMA de 1 rejeita."""
    assert "TP_FRACTIONS_EXCEED_ONE" not in _codes(
        _account(), _ctx(take_profit_fractions=[Decimal("0.5"), Decimal("0.5")])
    )


def test_tp_fractions_above_one_rejects() -> None:
    assert "TP_FRACTIONS_EXCEED_ONE" in _codes(
        _account(), _ctx(take_profit_fractions=[Decimal("0.5"), Decimal("0.51")])
    )


# ==========================================================================
# Projected daily breach — fronteira
# ==========================================================================


def test_projected_exactly_at_limit_is_allowed() -> None:
    """Se o pior caso do trade leva EXATAMENTE ao limite diário, ainda é
    permitido; só ULTRAPASSAR rejeita (projected < limit)."""
    # projected = daily_pnl - equity*max_risk_per_trade
    # limit = -(equity*max_daily_loss)
    equity = Decimal("100000")
    risk_budget = equity * P.max_risk_per_trade  # 250
    limit = equity * P.max_daily_loss  # 1000
    # daily_pnl tal que projected == -limit exatamente
    daily = -(limit) + risk_budget  # -750; projected = -750-250 = -1000 == limit
    assert "PROJECTED_DAILY_BREACH" not in _codes(
        _account(daily_pnl=daily), _ctx()
    )


def test_projected_one_cent_past_limit_rejects() -> None:
    equity = Decimal("100000")
    risk_budget = equity * P.max_risk_per_trade
    limit = equity * P.max_daily_loss
    daily = -(limit) + risk_budget - Decimal("0.01")
    assert "PROJECTED_DAILY_BREACH" in _codes(_account(daily_pnl=daily), _ctx())


# ==========================================================================
# Política — fronteiras dos validadores de sanidade
# ==========================================================================


def test_risk_exactly_at_sanity_ceiling_is_allowed() -> None:
    """max_risk == teto de sanidade (5%) é aceito; só ACIMA rejeita."""
    RiskPolicy.conservative_v0().replace(
        max_risk_per_trade=Decimal("0.05"), max_total_risk=Decimal("0.05")
    )  # não levanta


def test_risk_just_above_sanity_ceiling_rejects() -> None:
    with pytest.raises(ValueError, match="implausível"):
        RiskPolicy.conservative_v0().replace(
            max_risk_per_trade=Decimal("0.0501"), max_total_risk=Decimal("0.0501")
        )


def test_risk_exactly_equal_to_total_is_allowed() -> None:
    """max_risk == max_total é aceito; só EXCEDER rejeita."""
    RiskPolicy.conservative_v0().replace(
        max_risk_per_trade=Decimal("0.005"), max_total_risk=Decimal("0.005")
    )


def test_daily_exactly_equal_to_weekly_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(
        max_daily_loss=Decimal("0.03"), max_weekly_loss=Decimal("0.03")
    )


def test_leverage_exactly_one_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(max_leverage=Decimal("1"))


def test_min_rr_exactly_one_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(min_rr_net=Decimal("1"))


def test_concurrent_positions_exactly_one_is_allowed() -> None:
    RiskPolicy.conservative_v0().replace(max_concurrent_positions=1)


# ==========================================================================
# Sizing — fronteiras de rejeição
# ==========================================================================


def _sizing(**over: object) -> SizingInputs:
    base: dict[str, object] = {
        "equity": Price("100000"),
        "entry": Price("60000"),
        "stop": Price("59400"),
        "risk_fraction": Decimal("0.0025"),
        "taker_fee_rate": Decimal("0"),
        "estimated_slippage": Price("0"),
        "max_leverage": Decimal("2"),
        "available_liquidity": Quantity("1000"),
        "spec": _spec(),
        "order_type": "Limit",
    }
    base.update(over)
    return SizingInputs(**base)  # type: ignore[arg-type]


def test_zero_risk_fraction_rejects_at_budget_check() -> None:
    """Orçamento exatamente zero rejeita NO check de orçamento, com o
    motivo certo — não cai adiante e rejeita por 'quantidade mínima'."""
    r = compute_size(_sizing(risk_fraction=Decimal("0")))
    assert not r.approved
    assert "orçamento" in r.rejection_reason.lower()


def test_quantity_exactly_at_min_order_qty_is_approved() -> None:
    """Quantidade que cai EXATAMENTE no mínimo da corretora é aceita;
    só ABAIXO rejeita (rounded < min_order_qty)."""
    # Ajusta o orçamento para render exatamente 0.001 a 60000 com dist 600:
    # qty = budget/600 = 0.001 -> budget = 0.6 -> equity*rf = 0.6
    # equity=240, rf=0.0025 -> budget=0.6 -> qty=0.001
    r = compute_size(
        _sizing(equity=Price("240"), risk_fraction=Decimal("0.0025"),
                stop=Price("59400"))
    )
    assert r.approved
    assert r.quantity == Quantity("0.001")


def test_leverage_cap_produces_exact_quantity() -> None:
    """Quando a alavancagem é o binding, a quantidade é exatamente
    equity*max_leverage/entry (arredondada). Fixa o cálculo do notional
    máximo — um sinal trocado (/ no lugar de *) seria pego aqui."""
    # equity 100k, leverage 2 -> notional máx 200k; a 60000 -> 3.333...
    # stop apertado + sem taxas isola a alavancagem como binding
    r = compute_size(
        _sizing(
            equity=Price("100000"),
            entry=Price("60000"),
            stop=Price("59988"),  # dist 12 -> qty_by_risk enorme
            risk_fraction=Decimal("0.0025"),
            max_leverage=Decimal("2"),
        )
    )
    assert r.approved
    assert r.binding_constraint == "leverage"
    assert r.quantity is not None
    # 200000/60000 = 3.3333... arredondado para baixo ao step 0.001
    assert r.quantity == Quantity("3.333")


def test_notional_exactly_at_min_is_approved() -> None:
    """Notional exatamente no mínimo (5 USDT) é aceito; só ABAIXO rejeita."""
    # qty=0.001 a entry=5000 -> notional=5.0 == min_notional
    spec = _spec()
    # budget para qty 0.001 com dist tal que qty=0.001
    r = compute_size(
        _sizing(
            entry=Price("5000"), stop=Price("4900"),  # dist 100
            equity=Price("40"), risk_fraction=Decimal("0.0025"),  # budget 0.1 -> qty 0.001
            spec=spec,
        )
    )
    assert r.approved
    assert r.quantity is not None
    assert r.quantity.value * Decimal("5000") >= spec.min_notional

```

## `tests/unit/risk/test_engine.py`

```python
"""Sprint 4e — orquestração do Risk Engine.

O engine é a autoridade final: recebe uma intenção do modelo, roda os
validadores E o sizing, e devolve uma decisão de risco — aprovada com
quantidade calculada, ou rejeitada com a lista de motivos.

Regra de ouro: validação primeiro. Se qualquer regra rejeita, o sizing
nem roda — não faz sentido calcular quantidade para um trade proibido.
E a quantidade NUNCA vem da intenção; é sempre calculada aqui.
"""

from __future__ import annotations

from decimal import Decimal

from bybit_agent.domain.instrument import InstrumentSpec
from bybit_agent.risk.engine import RiskDecision, TradeIntent, evaluate
from bybit_agent.risk.policy import RiskPolicy
from bybit_agent.risk.validators import AccountState


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


def _account(**over: object) -> AccountState:
    base: dict[str, object] = {
        "equity": Decimal("100000"),
        "daily_pnl": Decimal("0"),
        "weekly_pnl": Decimal("0"),
        "open_positions": 0,
        "open_orders": 0,
        "consecutive_losses": 0,
        "entries_today": 0,
        "data_age_ms": 100,
        "spread_bps": Decimal("2"),
        "estimated_slippage_bps": Decimal("4"),
        "has_conflicting_position": False,
        "has_conflicting_order": False,
    }
    base.update(over)
    return AccountState(**base)  # type: ignore[arg-type]


def _intent(**over: object) -> TradeIntent:
    base: dict[str, object] = {
        "decision_id": "5f1e4d3c-2b1a-4098-8765-43210fedcba9",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "entry": Decimal("60000"),
        "stop": Decimal("59400"),
        "invalidation": Decimal("59500"),
        "liquidation": Decimal("30000"),
        "rr_net": Decimal("2.5"),
        "intent_expires_at_ms": 9_999_999_999_999,
        "take_profit_fractions": [Decimal("0.5"), Decimal("0.5")],
        "is_averaging_down": False,
        "widens_stop": False,
        "order_type": "Limit",
    }
    base.update(over)
    return TradeIntent(**base)  # type: ignore[arg-type]


def _config() -> dict[str, object]:
    return {
        "spec": _spec(),
        "policy": RiskPolicy.conservative_v0(),
        "taker_fee_rate": Decimal("0.00055"),
        "estimated_slippage": Decimal("6"),
        "available_liquidity": Decimal("1000"),
        "now_ms": 1_000_000_000_000,
    }


# --------------------------------------------------------------------------
# Aprovação
# --------------------------------------------------------------------------


def test_valid_intent_is_approved_with_computed_quantity() -> None:
    d = evaluate(_intent(), _account(), **_config())
    assert isinstance(d, RiskDecision)
    assert d.approved
    assert d.quantity is not None
    assert d.quantity.value > 0
    assert d.rejections == []


def test_approved_decision_records_policy_hash() -> None:
    """⭐ Rastreabilidade: qual versão da política aprovou este trade."""
    policy = RiskPolicy.conservative_v0()
    d = evaluate(_intent(), _account(), **{**_config(), "policy": policy})
    assert d.policy_hash == policy.policy_hash


def test_approved_decision_carries_risk_breakdown() -> None:
    d = evaluate(_intent(), _account(), **_config())
    assert d.risk_budget is not None
    assert d.risk_per_unit is not None
    assert d.binding_constraint is not None


# --------------------------------------------------------------------------
# A quantidade NUNCA vem da intenção
# --------------------------------------------------------------------------


def test_intent_has_no_quantity_field() -> None:
    """⭐ Garantia estrutural: TradeIntent não tem como carregar quantidade.
    Se tivesse, o modelo poderia influenciar o tamanho."""
    assert not hasattr(_intent(), "quantity")
    assert not hasattr(_intent(), "qty")
    assert not hasattr(_intent(), "size")
    assert not hasattr(_intent(), "leverage")


# --------------------------------------------------------------------------
# Rejeição — validação antes do sizing
# --------------------------------------------------------------------------


def test_rejected_intent_has_no_quantity() -> None:
    d = evaluate(_intent(), _account(open_positions=1), **_config())
    assert not d.approved
    assert d.quantity is None
    assert any(r.code == "MAX_POSITIONS" for r in d.rejections)


def test_sizing_does_not_run_when_validation_fails() -> None:
    """Se a validação rejeita, não há quantidade — o sizing é curto-
    circuitado. Um trade proibido não recebe dimensionamento."""
    d = evaluate(_intent(symbol="ETHUSDT"), _account(), **_config())
    assert not d.approved
    assert d.quantity is None
    assert d.binding_constraint is None


def test_valid_rules_but_unsizable_is_rejected() -> None:
    """Passa nos validadores mas o patrimônio é pequeno demais para
    qualquer quantidade válida → rejeitado pelo sizing."""
    d = evaluate(_intent(), _account(equity=Decimal("10")),
                 **{**_config(), "policy": RiskPolicy.conservative_v0()})
    assert not d.approved
    assert d.quantity is None
    assert any(r.code == "UNSIZABLE" for r in d.rejections)


# --------------------------------------------------------------------------
# Determinismo — a invariante que sustenta o resto
# --------------------------------------------------------------------------


def test_engine_is_deterministic() -> None:
    intent, account, cfg = _intent(), _account(), _config()
    assert evaluate(intent, account, **cfg) == evaluate(intent, account, **cfg)


def test_short_intent_is_sized_correctly() -> None:
    d = evaluate(
        _intent(
            side="SELL",
            entry=Decimal("60000"),
            stop=Decimal("60600"),
            invalidation=Decimal("60500"),
            liquidation=Decimal("90000"),
        ),
        _account(),
        **_config(),
    )
    assert d.approved
    assert d.quantity is not None
    assert d.quantity.value > 0

```

## `tests/property/test_risk_invariants.py`

```python
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
    # exposição / alavancagem
    assert q * inp.entry.value <= inp.equity.value * inp.max_leverage + inp.spec.qty_step * inp.entry.value
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

```


---

# O QUE REVISAR

Análise crítica focada. Priorize **correção e segurança financeira** sobre
estilo.

## Perguntas dirigidas

### Cálculo de quantidade (`sizing.py`)
1. O `risk_per_unit` soma distância + taxas (ida e volta) + slippage. Isso
   captura corretamente a perda por unidade no pior caso? Falta algum
   custo (funding, taxa de fechamento diferente da de abertura)?
2. O teto de alavancagem usa `equity × max_leverage / entry`. Isso é
   correto para contratos lineares? A margem inicial real da Bybit bate
   com isso?
3. `qty_by_leverage` usa `entry` como preço. Deveria usar `mark price`
   (que a Bybit usa para margem e liquidação)?
4. O arredondamento para baixo pode zerar a quantidade. A rejeição por
   "abaixo do mínimo" cobre todos os casos, ou há um caminho em que uma
   quantidade inválida escapa?

### Validadores (`validators.py`)
5. O validador `_projected_daily` estima a perda do pior caso como
   `equity × max_risk_per_trade`. Mas a perda real pode exceder isso por
   slippage/gap além do stop. A estimativa é conservadora o suficiente?
6. As fronteiras: limites de **magnitude** (spread, slippage, data_age,
   RR) aceitam o valor exato no limite; limites de **contagem** (posições,
   entradas, perdas) rejeitam no teto. Essa distinção está correta e
   consistente? Há algum limite classificado no grupo errado?
7. `_stop_beyond_liquidation` compara o stop com um preço de liquidação
   *fornecido*. Se esse preço estiver errado (calculado por outro
   componente), o validador dá falsa segurança. Deveria o engine recalcular
   a liquidação internamente?
8. Falta algum validador? A spec lista: martingale, média contra posição,
   ampliação de stop, horários bloqueados, volatilidade anormal. Todos
   estão cobertos? Algum é fácil de burlar?

### Política (`policy.py`)
9. O teto de sanidade de 5% (`_MAX_PLAUSIBLE_RISK_PER_TRADE`) pega o erro
   "0,25 em vez de 0,0025". Há outros erros de digitação plausíveis que
   passariam pela validação?
10. O `policy_hash` serializa os campos ordenados. É determinístico entre
    execuções e versões de Python? `frozenset` de símbolos é ordenado
    antes do hash?

### Motor (`engine.py`)
11. A ordem é: validar → dimensionar. Um trade que passa nos validadores
    mas é "unsizable" é rejeitado. Existe algum caminho em que um trade
    aprovado receba quantidade zero ou inválida?
12. `evaluate` recebe `now_ms`, `taker_fee_rate`, `estimated_slippage`,
    `available_liquidity` de fora. Se qualquer um vier errado (ex.:
    slippage subestimado), o engine confia cegamente. Onde deveria haver
    validação de sanidade desses inputs?

### Geral
13. **Concorrência:** o engine é puro, mas será chamado num sistema
    assíncrono. Há algum estado compartilhado implícito (contexto Decimal
    global?) que quebraria sob concorrência?
14. **Precisão Decimal:** o contexto usa 28 dígitos com trap de
    `InvalidOperation`/`DivisionByZero`. Há alguma operação que poderia
    perder precisão silenciosamente ou levantar inesperadamente?
15. **O que mais falharia com dinheiro real** que os testes não cobrem?

## Formato da resposta esperada

Para cada problema encontrado:
- **Severidade:** crítico (perde dinheiro) / alto / médio / baixo.
- **Localização:** arquivo e função.
- **Cenário concreto:** entradas específicas que produzem o comportamento
  errado.
- **Correção sugerida.**
