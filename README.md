# Bybit Risk-Constrained Trading Agent

Agente de trading de criptomoedas onde **Claude Opus 4.8 é o analista** e um
**motor determinístico de risco é a autoridade final**. O modelo produz
*intenções* de operação; ele nunca escolhe tamanho de posição, alavancagem
ou limite de risco.

```
Bybit Market Data → Coletor → Feature Engine → Claude Opus 4.8 (análise)
                                                      ↓
                                    Motor de Risco (autoridade) → Gateway → Bybit
                                                      ↓
                                   Reconciliação · Auditoria · Circuit breakers
```

## Documentação

| Documento | Papel |
|---|---|
| [`docs/PLANO.md`](docs/PLANO.md) | Roadmap (12 sprints), metodologia TDD, decisões travadas |
| [`docs/BYBIT_INTEGRACAO.md`](docs/BYBIT_INTEGRACAO.md) | **Normativo.** Contrato verificado da API Bybit V5 |
| `contracts/*.json` | Schemas versionados |
| `docs/spikes/` | Resultados dos spikes empíricos |

## Estado atual — Sprint 0 concluído

| Componente | Estado | Testes |
|---|---|---|
| `domain/money.py` — Decimal, tick/step | ✅ | 31 |
| Lint anti-`float` (gate de CI) | ✅ | 16 |
| Spike S-2 (script pronto, **não executado**) | ⏳ | 14 |
| `contracts/decision_v1.json` | ✅ | 17 |
| Market data, Risk Engine, Gateway, Agente | ❌ | — |

**78 testes passando.** Suite unit roda em < 1s.

## Setup

```bash
pip install -e ".[dev]"
$env:PYTHONPATH = "src;."     # PowerShell
export PYTHONPATH=src:.       # bash

pytest tests -q               # todos os testes
python -m tools.lint.no_float # gate anti-float
```

Postgres (necessário a partir do Sprint 5):
```bash
docker compose up -d
```

## Os dois princípios que governam o código

**1. Dinheiro nunca é float.** `float` não representa `0.1` exatamente. Um erro
de 1e-17 propagado por um cálculo de sizing vira uma quantidade que a corretora
rejeita — ou pior, aceita errada. Todo valor monetário é `Decimal`, construído
de `str`/`int`/`Decimal`. Construir de `float` levanta `TypeError` por design.

Isso não é convenção de equipe — é um gate de CI:
```bash
python -m tools.lint.no_float   # exit 1 se houver float em domain/risk/execution/marketdata/features
```

**2. O modelo propõe, o motor dispõe.** O schema de decisão **não tem campo de
quantidade, tamanho ou alavancagem**. Se tivesse, a separação entre analista e
motor de risco estaria comprometida no contrato, antes do código. Há um teste
que garante isso: `test_schema_has_no_qty_or_leverage_field`.

## ⚠️ Antes de escrever o Execution Gateway

O **spike S-2 precisa ser executado**. Ele responde se a Bybit realmente
deduplica `orderLinkId` — a documentação **não garante isso**, e o plano
original assumia que sim.

Esse é o controle que impede um retry após timeout de duplicar uma posição.
Hoje ele repousa numa suposição não verificada.

```powershell
# Chaves criadas DENTRO do modo Demo Trading de uma conta mainnet (UID separado).
# Não use chaves de testnet nem de produção.
$env:BYBIT_DEMO_KEY = "..."
$env:BYBIT_DEMO_SECRET = "..."
python -m tools.spikes.s2_order_link_id_dedup          # T1-T4, ~2 min
python -m tools.spikes.s2_order_link_id_dedup --long   # + T5 (1h), T6 (24h)
```

O script envia ordens reais e **se recusa a rodar contra qualquer host que não
seja `api-demo.bybit.com`** — verificação em código, com teste. Resultado em
`docs/spikes/S-2-resultado.md`.

Os outros 7 spikes (S-1, S-3…S-8) estão especificados em
[`BYBIT_INTEGRACAO.md` §9](docs/BYBIT_INTEGRACAO.md#9-spikes-obrigatórios).

## Próximo passo

Sprint 1 (contratos restantes: `snapshot_v1`, `trade_intent_v1`,
`risk_policy_v1`) e Sprint 4 (Risk Engine) podem começar em paralelo — o Risk
Engine é biblioteca pura, sem I/O, e é o componente que mais se beneficia de
tempo e revisão.

**Sprint 6 (Execution Gateway) não começa com spike pendente.**

---

## Aviso

Software experimental de trading automatizado. Os valores de risco no plano
(0,25% por operação, 1% de perda diária) são parâmetros de engenharia para
validação, **não recomendação financeira**. Nenhuma linha deste sistema deve
tocar capital real antes de concluir a Fase 5 (demo trading) com todos os
critérios de saída atendidos.
