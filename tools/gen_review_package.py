"""Gera um pacote autocontido do Risk Engine para revisão externa.

Concatena contexto + código-fonte + testes num único markdown que outra
IA (ou revisor humano) pode analisar sem acesso ao repositório. Monta a
partir do disco, garantindo fidelidade ao que está commitado.

Uso:
    python -m tools.gen_review_package
    -> escreve docs/REVISAO_RISK_ENGINE.md
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "REVISAO_RISK_ENGINE.md"

SOURCE_FILES = [
    "src/bybit_agent/domain/money.py",
    "src/bybit_agent/domain/instrument.py",
    "src/bybit_agent/risk/policy.py",
    "src/bybit_agent/risk/sizing.py",
    "src/bybit_agent/risk/validators.py",
    "src/bybit_agent/risk/engine.py",
]

TEST_FILES = [
    "tests/unit/risk/test_policy_and_instrument.py",
    "tests/unit/risk/test_sizing.py",
    "tests/unit/risk/test_validators.py",
    "tests/unit/risk/test_boundaries.py",
    "tests/unit/risk/test_engine.py",
    "tests/property/test_risk_invariants.py",
]

HEADER = """# Pacote de Revisão — Risk Engine

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

"""

FOOTER = """
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
"""


def _fence_lang(path: str) -> str:
    return "python"


def build() -> str:
    parts = [HEADER]
    for rel in SOURCE_FILES:
        content = (ROOT / rel).read_text(encoding="utf-8")
        parts.append(f"## `{rel}`\n\n```python\n{content}\n```\n")

    parts.append("\n---\n\n# TESTES\n\n")
    parts.append(
        "Os testes documentam o comportamento esperado. Um revisor pode "
        "usá-los para entender a intenção — e para achar o que eles NÃO "
        "cobrem.\n\n"
    )
    for rel in TEST_FILES:
        content = (ROOT / rel).read_text(encoding="utf-8")
        parts.append(f"## `{rel}`\n\n```python\n{content}\n```\n")

    parts.append(FOOTER)
    return "\n".join(parts)


def main() -> int:
    OUT.write_text(build(), encoding="utf-8")
    size = OUT.stat().st_size
    print(f"escrito: {OUT.relative_to(ROOT)} ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
