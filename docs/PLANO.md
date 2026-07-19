# Plano de Desenvolvimento — Bybit Risk-Constrained Trading Agent

**Versão:** 1.0
**Data:** 2026-07-19
**Stack:** Python 3.12 + asyncio · Docker Compose + PostgreSQL 16 · MCP stdio local · Claude Opus 4.8
**Metodologia:** TDD estrito (red → green → refactor), com gates de qualidade bloqueantes por fase.

---

## 0. Correções técnicas na especificação original

Três pontos da sua spec estão desatualizados ou tecnicamente frágeis. Corrigi-los agora evita retrabalho na Fase 3.

### 0.1 O modelo NÃO deve "retornar JSON válido" por instrução de prompt

Sua spec pede: *"Retorne somente um objeto JSON válido. Não use Markdown."* e lista `Modelo produz JSON inválido repetidamente` como circuit breaker.

Isso é um problema resolvido no nível da API. O Opus 4.8 suporta **structured outputs**: você passa `output_config={"format": {"type": "json_schema", "schema": {...}}}` e a API **garante** que a saída valida contra o schema. Não é uma instrução — é uma restrição de decodificação.

Consequências:
- O circuit breaker "JSON inválido" vira um alerta de infraestrutura (falha de API), não um comportamento esperado do modelo.
- A seção `FORMATO DE SAÍDA` do system prompt encolhe para uma linha. Menos tokens, menos superfície de erro.
- O schema JSON vira um artefato versionado em `contracts/decision_v1.json`, testável isoladamente.

**Limitações do JSON Schema aceito pela API** (importam ao desenhar o contrato):
- Suportado: `object`, `array`, `string`, `integer`, `number`, `boolean`, `null`, `enum`, `const`, `anyOf`, `allOf`, `$ref`/`$defs`, formatos (`date-time`, `uuid`), `additionalProperties: false` (obrigatório em todo objeto).
- **Não** suportado: schemas recursivos, `minimum`/`maximum`/`multipleOf`, `minLength`/`maxLength`, constraints complexos de array.

Ou seja: `close_fraction` entre 0 e 1 **não** pode ser expresso no schema. Isso é validado pelo Risk Engine, em código, com `Decimal`. Isso é bom — reforça que a autoridade é o motor determinístico.

**Preços como string decimal:** o schema declara `"type": "string"`. O parser converte com `Decimal(...)` e rejeita `NaN`/`Infinity` explicitamente.

### 0.2 Configuração do modelo (Opus 4.8)

```python
response = client.messages.create(
    model="claude-opus-4-8",
    max_tokens=8000,
    thinking={"type": "adaptive"},        # OBRIGATÓRIO ser explícito — off por padrão no 4.8
    output_config={
        "effort": "high",
        "format": {"type": "json_schema", "schema": DECISION_SCHEMA_V1},
    },
    system=[...],                          # com cache_control
    messages=[...],
    tools=[...],                           # todas com strict: True
)
```

Regras que quebram (400) se ignoradas:
| Erro | Correção |
|---|---|
| `temperature`, `top_p`, `top_k` | **Removidos** no Opus 4.8 — retornam 400. Determinismo se busca via `effort: "low"` + prompt, não via `temperature=0`. |
| `thinking={"type":"enabled","budget_tokens":N}` | Removido — 400. Usar `{"type": "adaptive"}`. |
| Prefill na última mensagem `assistant` | Removido — 400. Substituído por `output_config.format`. |
| Omitir `thinking` | Não é erro, mas roda **sem** thinking. Para análise de mercado isso degrada a qualidade. Setar explicitamente. |

`thinking.display` default é `"omitted"` — os blocos de thinking chegam com texto vazio. Para auditoria (que sua spec exige), setar `display: "summarized"` e persistir o resumo no event log.

### 0.3 Prompt caching: o system prompt precisa ter ≥ 4096 tokens

No Opus 4.8, o prefixo mínimo cacheável é **4096 tokens**. Prefixos menores simplesmente não cacheiam — sem erro, apenas `cache_creation_input_tokens: 0`.

Seu system prompt v0.1 tem ~2.500 tokens. Duas saídas:
1. Aceitar que não cacheia (custo: ~$0.0125 por chamada só de input do prompt — irrelevante no volume esperado de ~50-200 chamadas/dia).
2. Cachear **tools + system juntos** — a ordem de renderização é `tools` → `system` → `messages`, e as 9 definições de ferramentas com schemas estritos e descrições detalhadas devem somar ~2.000+ tokens. Com o breakpoint no último bloco do system, ambos cacheiam juntos e o total passa de 4096.

**Adotamos (2).** Isso impõe uma restrição de arquitetura: **o conjunto de ferramentas nunca muda entre chamadas** (ferramentas renderizam na posição 0 — qualquer mudança invalida tudo). Nada de "ferramentas condicionais por estado". Todas as 9 sempre presentes; o Risk Engine rejeita as inválidas para o estado atual.

**Invalidadores silenciosos a evitar no system prompt:**
- Nada de `datetime.now()`, `uuid4()`, ID de sessão ou saldo da conta interpolados no system.
- Serialização de tools determinística: ordenar por `name`, `json.dumps(..., sort_keys=True)`.
- Políticas de risco entram no **snapshot** (mensagem do usuário), não no system.

Verificação obrigatória: `usage.cache_read_input_tokens > 0` a partir da 2ª chamada. Vira um teste de integração.

### 0.4 Correções menores

| Item da spec | Correção |
|---|---|
| "Uma ferramenta por ciclo mutável" | Reforçar com `disable_parallel_tool_use: true` no `tool_choice`. Instrução no prompt não é garantia; o parâmetro é. |
| `HALT_TRADING` como ação do modelo | Manter, mas o halt real é **sempre** disparado pelo Risk Engine. A ação do modelo é um *sinal* que o motor avalia. O modelo nunca escreve no estado de halt diretamente. |
| `quality_score: 0` no schema | Sem `minimum`/`maximum` no schema. Definir como `integer` e validar 0–100 no parser. |
| Alavancagem 2x com risco 0,25% | Coerente, mas registre: com stop de 1% e risco 0,25%, o notional é 25% do patrimônio — abaixo de 2x. A alavancagem quase nunca será o binding constraint. Ela existe como teto de segurança contra liquidação, não como alavanca de retorno. |

---

## 1. Decisões travadas (não reabrir sem ADR)

| # | Decisão | Racional |
|---|---|---|
| D1 | Python 3.12 + asyncio | Ecossistema quant, `Decimal` nativo, pytest+hypothesis. |
| D2 | PostgreSQL 16 via Docker Compose | Paridade dev/prod desde o dia 1. `NUMERIC` para dinheiro. |
| D3 | MCP server local via stdio | Ferramentas testáveis sem rede. Reutilizáveis em Claude Desktop/Code. |
| D4 | Orquestrador chama Messages API diretamente | O loop de decisão é código nosso, determinístico e testável. Sem tool_runner autônomo. |
| D5 | **Todo dinheiro é `Decimal`** | `float` é proibido em qualquer caminho de preço/quantidade/PnL. Lint bloqueia. |
| D6 | Símbolo único BTCUSDT, linear perpetual, **one-way mode** | Hedge mode dobra a complexidade de reconciliação sem benefício na v0. |
| D7 | Event sourcing append-only | Estado derivado de eventos. Reconciliação e auditoria dependem disso. |
| D8 | Risk Engine é biblioteca pura, sem I/O | Testável com property-based testing. Zero mocks. |
| D9 | Ferramentas MCP fixas (9), sempre presentes | Preserva o prompt cache (ver §0.3). |
| D10 | **[REVISADA]** `orderLinkId` determinístico **+ consulta de estado obrigatória antes de todo reenvio** | A dedup de `orderLinkId` pela Bybit **não é documentada** (ver `BYBIT_INTEGRACAO.md` §5.2). A idempotência real vem da camada 2, sob nosso controle. Máx. 36 chars, `[A-Za-z0-9_-]`. |
| D11 | `tpslMode = "Full"` na v0 | Cobre fill parcial automaticamente. Partial tem risco documentado de ordem órfã. Ver `BYBIT_INTEGRACAO.md` §5.6. |
| D12 | Depth `orderbook.50.BTCUSDT` | Depth 500 não existe. 50 níveis @20ms bastam para o sizing da política. |
| D13 | Patrimônio lido via REST **síncrono** antes de toda decisão de risco | WS de wallet não emite snapshot inicial nem atualiza PnL não realizado. Ver `BYBIT_INTEGRACAO.md` §6. |
| D14 | `RateLimitGovernor` centralizado obrigatório | Limite de **600 req/5s por IP → ban de 10 min**. Ban com posição aberta = perda descontrolada. Ver `BYBIT_INTEGRACAO.md` §7. |

> **Contrato de integração verificado:** todos os detalhes de endpoint, autenticação, formato de mensagem e código de erro estão em [`BYBIT_INTEGRACAO.md`](./BYBIT_INTEGRACAO.md). Esse documento é normativo — a camada de adaptação Bybit não implementa nada que não esteja lá com fonte, ou que não tenha passado por um spike.

---

## 2. Estrutura do repositório

```
cripto/
├── docker-compose.yml
├── pyproject.toml
├── contracts/
│   ├── decision_v1.json           # JSON Schema da resposta do Claude
│   ├── snapshot_v1.json           # JSON Schema do market snapshot
│   ├── trade_intent_v1.json
│   └── risk_policy_v1.json
├── prompts/
│   ├── system_v0.1.md             # versionado; hash em cada decisão persistida
│   └── CHANGELOG.md
├── src/bybit_agent/
│   ├── domain/                    # tipos puros, zero I/O
│   │   ├── money.py               # Decimal, tick/step rounding
│   │   ├── instrument.py
│   │   ├── snapshot.py
│   │   ├── intent.py
│   │   ├── decision.py
│   │   └── state.py               # máquina de estados
│   ├── marketdata/
│   │   ├── ws_client.py
│   │   ├── orderbook.py           # snapshot + delta, detecção de gap de seq
│   │   ├── candles.py
│   │   └── validator.py           # staleness, conflitos, sanidade
│   ├── features/
│   │   ├── indicators.py          # ATR, EMA, RV — puros
│   │   ├── structure.py           # swings, BOS
│   │   ├── liquidity.py           # spread, imbalance, slippage estimado
│   │   └── engine.py              # monta o snapshot
│   ├── risk/                      # ⭐ núcleo — biblioteca pura
│   │   ├── policy.py              # carrega política imutável
│   │   ├── sizing.py              # cálculo de quantidade
│   │   ├── validators.py          # 20+ regras compostas
│   │   ├── breakers.py            # circuit breakers
│   │   └── engine.py              # orquestra: intent → decisão aprovada/rejeitada
│   ├── execution/
│   │   ├── gateway.py             # único componente com a API key
│   │   ├── bybit_rest.py
│   │   ├── bybit_ws_private.py
│   │   ├── reconciler.py
│   │   └── idempotency.py
│   ├── agent/
│   │   ├── client.py              # wrapper Anthropic
│   │   ├── prompt.py              # montagem com cache_control
│   │   ├── parser.py              # decision JSON → domain
│   │   └── orchestrator.py        # loop de decisão
│   ├── mcp/
│   │   ├── server.py              # servidor stdio
│   │   └── tools/                 # 9 ferramentas, uma por arquivo
│   ├── persistence/
│   │   ├── events.py              # append-only
│   │   ├── projections.py         # estado derivado
│   │   └── migrations/            # alembic
│   ├── statemachine/
│   │   └── machine.py
│   └── observability/
│       ├── metrics.py
│       └── audit.py
├── tests/
│   ├── unit/                      # rápidos, puros, sem I/O
│   ├── property/                  # hypothesis — invariantes de risco
│   ├── contract/                  # schemas + gravações da API Bybit
│   ├── integration/               # docker: postgres + mock exchange
│   ├── simulation/                # replay histórico + injeção de falhas
│   ├── chaos/                     # desconexão, latência, respostas parciais
│   └── fixtures/
│       ├── bybit/                 # respostas reais gravadas (VCR)
│       └── scenarios/             # cenários de mercado curados
└── tools/
    ├── mock_bybit/                # exchange fake: REST + WS
    └── replay/                    # motor de replay histórico
```

---

## 3. Metodologia TDD

### 3.1 A regra

**Nenhuma linha de código de produção sem um teste falhando que a exija.**

Ciclo por unidade de trabalho:
1. **RED** — escreve o teste, roda, vê falhar pelo motivo certo (não por `ImportError`).
2. **GREEN** — implementação mínima para passar. Feio é aceitável.
3. **REFACTOR** — limpa com os testes verdes como rede.
4. **COMMIT** — um ciclo completo por commit. Mensagem descreve o comportamento, não o código.

### 3.2 Pirâmide de testes e orçamento de tempo

| Camada | Qtd alvo | Tempo máx. suite | O que cobre |
|---|---|---|---|
| Unit | ~400 | < 10s | Lógica pura: indicadores, sizing, validadores, parsing. |
| Property | ~40 | < 60s | Invariantes que devem valer para *todas* as entradas. |
| Contract | ~60 | < 20s | Schemas JSON, formatos de resposta Bybit (gravados). |
| Integration | ~80 | < 3min | Postgres real + mock exchange. Fluxos ponta a ponta. |
| Simulation | ~30 | < 10min | Replay histórico com custos, latência, fills parciais. |
| Chaos | ~25 | < 5min | Falhas injetadas. Cada circuit breaker tem ≥1 teste. |

Se a suite unit passar de 10s, algo virou integração disfarçada. Investigar.

### 3.3 Testes property-based são obrigatórios no Risk Engine

O Risk Engine não pode ser testado só por exemplos. As invariantes abaixo viram propriedades Hypothesis, cada uma com ≥1000 casos gerados:

```python
# tests/property/test_risk_invariants.py

@given(account=accounts(), intent=intents(), policy=policies())
def test_risco_nunca_excede_politica(account, intent, policy):
    """P1: A perda máxima projetada nunca ultrapassa o orçamento de risco."""
    result = risk_engine.evaluate(account, intent, policy)
    assume(result.approved)
    perda_max = (result.qty * abs(intent.entry - result.stop_loss)
                 + result.fees_estimated + result.slippage_estimated)
    assert perda_max <= account.equity * policy.max_risk_per_trade

@given(...)
def test_quantidade_sempre_multipla_de_qty_step(account, intent, policy, instrument):
    """P2: Quantidade sempre alinhada ao qtyStep, arredondada para BAIXO."""
    result = risk_engine.evaluate(...)
    assume(result.approved)
    assert result.qty % instrument.qty_step == 0
    assert result.qty <= result.qty_unrounded   # nunca arredonda para cima

@given(...)
def test_stop_do_lado_correto(intent, result):
    """P3: LONG → stop < entrada. SHORT → stop > entrada. Sempre."""
    if intent.side == Side.BUY:  assert result.stop_loss < intent.entry
    else:                        assert result.stop_loss > intent.entry

@given(...)
def test_rr_liquido_respeita_minimo(account, intent, policy):
    """P4: RR líquido (pós taxas e slippage) ≥ política. Nunca o RR bruto."""
    result = risk_engine.evaluate(account, intent, policy)
    assume(result.approved)
    assert result.rr_net >= policy.min_rr_net

@given(...)
def test_engine_e_deterministico(account, intent, policy):
    """P5: Mesma entrada → mesma saída. Sempre. Sem estado oculto."""
    a = risk_engine.evaluate(account, intent, policy)
    b = risk_engine.evaluate(account, intent, policy)
    assert a == b

@given(...)
def test_nenhum_float_no_resultado(result):
    """P6: Nenhum campo numérico do resultado é float."""
    for field in numeric_fields(result):
        assert isinstance(field, Decimal), f"{field!r} não é Decimal"

@given(policy=policies(), tampered=tampered_policies())
def test_politica_e_imutavel(policy, tampered):
    """P7: Nenhum caminho de código consegue mutar a política em runtime."""
    with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
        policy.max_risk_per_trade = tampered.max_risk_per_trade

@given(state=states(), action=actions())
def test_transicoes_invalidas_sempre_rejeitadas(state, action):
    """P8: A máquina de estados nunca aceita transição fora do grafo."""
    if (state, action) not in VALID_TRANSITIONS:
        with pytest.raises(InvalidTransition):
            machine.apply(state, action)

@given(equity=..., daily_pnl=..., weekly_pnl=...)
def test_drawdown_bloqueia_antes_de_estourar(equity, daily_pnl, weekly_pnl):
    """P9: Se a perda projetada do trade estouraria o limite diário,
       o trade é rejeitado ANTES de ser enviado — não depois."""
    ...
```

**P1 e P5 são os testes mais importantes do projeto.** Se eles quebram, nada mais importa.

### 3.4 Definição de Pronto (por módulo)

Um módulo só é "pronto" com todos os itens:

- [ ] Testes escritos antes do código (verificável no histórico git: commit de teste precede o de implementação)
- [ ] Cobertura de branch ≥ 95% em `risk/`, `execution/`, `statemachine/`; ≥ 85% no restante
- [ ] Property tests passando com 1000+ exemplos onde aplicável
- [ ] Mutation testing (`mutmut`) em `risk/` com score ≥ 90%
- [ ] Zero `float` em caminho de dinheiro (verificado por lint custom)
- [ ] Todos os caminhos de erro têm teste explícito
- [ ] Docstrings nas funções públicas com invariantes declaradas
- [ ] `mypy --strict` limpo
- [ ] Nenhum `# type: ignore` sem justificativa em comentário

### 3.5 Gates de CI (bloqueantes)

```yaml
# Ordem de execução — falha rápido no barato
1. ruff check + ruff format --check
2. mypy --strict
3. lint-no-float          # AST check custom: proibido float em domain/risk/execution
4. pytest tests/unit      # < 10s
5. pytest tests/property  # < 60s
6. pytest tests/contract
7. pytest tests/integration   # docker compose up
8. pytest tests/chaos
9. mutmut run --paths-to-mutate src/bybit_agent/risk/   # score ≥ 90%
10. coverage report --fail-under=95 --include='*/risk/*,*/execution/*'
```

Adicionalmente, um **teste de guarda arquitetural**:

```python
def test_apenas_o_gateway_conhece_a_api_key():
    """Nenhum módulo fora de execution/ importa credenciais."""
    for module in all_modules_except("execution.gateway", "config"):
        assert "BYBIT_API_SECRET" not in module.source

def test_risk_engine_nao_tem_io():
    """risk/ não importa nada de rede, disco ou banco."""
    forbidden = {"httpx", "aiohttp", "asyncpg", "requests", "open", "websockets"}
    assert not (imports_of("bybit_agent.risk") & forbidden)
```

---

## 4. Roadmap — 9 sprints

Cada sprint tem: **entregável verificável**, **lista de testes escritos primeiro**, e **critério de saída bloqueante**.

---

### Sprint -1 — Spikes de verificação (3 dias, paralelizável)

**Entregável:** os 8 itens não verificados da API Bybit resolvidos empiricamente, com dados brutos.

Este sprint não produz código de produção. Produz **conhecimento** que outros sprints assumem. Rodar antes torna o resto do plano honesto; pular transforma suposição em dívida técnica no componente mais crítico.

Detalhamento completo em [`BYBIT_INTEGRACAO.md`](./BYBIT_INTEGRACAO.md) §9.

| Spike | Pergunta | Bloqueia |
|---|---|---|
| S-1 | `recv_window` máximo | Cliente REST |
| **S-2** 🔴 | **Janela de dedup do `orderLinkId`** | **D10 / Sprint 6** |
| S-3 | Frequência e significado de gap de `u` | Sprint 2 |
| S-4 | Timeout de auth do WS privado | Cliente WS |
| S-5 | `tpslMode=Partial` com fill parcial | Confirma D11 |
| S-6 | Duplicatas/reordenação nos streams privados | Deduplicador |
| S-7 | Latência envio → fill (p50/p95/p99) | Modelo do backtest |
| S-8 | Taxas e funding reais | Modelo de custos do sizing |

**Pré-requisito:** conta Demo Trading criada, chaves geradas, conectividade validada.

**Saída:** `docs/spikes/S-N-resultado.md` para cada um, com dados brutos anexados. Spike sem dado bruto não conta como concluído.

**S-2 é o mais importante do projeto.** Se a Bybit não deduplicar `orderLinkId` de forma confiável, a única defesa contra ordem duplicada é a consulta de estado — e isso precisa ser sabido antes de escrever o gateway, não depois.

---

### Sprint 0 — Fundação (3 dias)

**Entregável:** repositório com CI verde, docker compose subindo, tipos monetários funcionando.

**Testes primeiro:**
```
test_money_rounds_down_to_tick_size
test_money_rounds_down_to_qty_step
test_money_rejects_float_construction
test_money_arithmetic_preserves_precision
test_money_never_produces_nan_or_inf
test_decimal_context_is_configured_globally      # prec=28, ROUND_DOWN
test_docker_compose_postgres_accepts_connection
test_alembic_migrations_apply_and_rollback
test_ci_pipeline_fails_on_float_in_risk_module   # meta-teste do próprio lint
```

**Implementação:** `domain/money.py`, `domain/instrument.py`, docker-compose, alembic, o lint AST anti-float, pipeline CI.

**Saída:** CI verde. `pytest` roda em < 5s. O lint anti-float pega um `float` plantado de propósito.

---

### Sprint 1 — Contratos e schemas (2 dias)

**Entregável:** todos os contratos JSON versionados e validados nos dois sentidos.

**Testes primeiro:**
```
test_decision_schema_is_valid_json_schema
test_decision_schema_has_additional_properties_false_everywhere
test_decision_schema_uses_no_unsupported_constraints   # ⭐ minimum/maxLength etc.
test_decision_schema_is_not_recursive
test_valid_decision_payload_parses_to_domain
test_decision_with_extra_field_is_rejected
test_decision_with_nan_price_is_rejected
test_decision_with_negative_close_fraction_is_rejected  # validado em código, não no schema
test_close_fractions_must_sum_to_at_most_one
test_snapshot_schema_roundtrips
test_risk_policy_is_frozen_dataclass
test_risk_policy_cannot_be_loaded_from_env_at_runtime   # só do arquivo assinado
```

**Ponto crítico:** `test_decision_schema_uses_no_unsupported_constraints` — varre o schema procurando `minimum`, `maximum`, `minLength`, `maxLength`, `multipleOf`, `minItems`. Se encontrar, falha com mensagem apontando o campo. Isso previne a classe inteira de bugs "o schema parece certo mas a API ignora a constraint".

---

### Sprint 2 — Market Data (5 dias)

**Entregável:** livro de ofertas local correto, candles multi-timeframe, validação de frescor.

**Testes primeiro:**
```
# Orderbook — regras VERIFICADAS da Bybit
test_orderbook_applies_snapshot
test_orderbook_applies_delta_insert_update_delete             # 3 casos documentados
test_orderbook_removes_level_when_size_is_zero
test_orderbook_rebuilds_completely_on_new_snapshot           # ⭐ regra explícita
test_orderbook_resets_on_update_id_equal_one                 # ⭐ u==1 = restart do serviço
test_orderbook_gap_marks_suspect_not_immediate_resync        # ⭐ heurística, não regra
test_orderbook_suspect_is_validated_against_rest_snapshot    # ⭐
test_orderbook_confirmed_divergence_triggers_data_stale
test_orderbook_repeated_update_id_is_not_treated_as_gap      # ⭐ depth 1 repete u
test_orderbook_best_bid_always_below_best_ask                # property
test_orderbook_is_never_crossed                              # property
test_orderbook_marks_stale_after_threshold

# Ticker — incremental em linear
test_ticker_snapshot_initializes_state
test_ticker_delta_merge_preserves_absent_fields              # ⭐ ausente ≠ zerado
test_ticker_delta_before_snapshot_is_rejected

# Candles
test_kline_rest_is_reversed_before_use                       # ⭐ REST vem invertido
test_kline_ws_confirm_true_means_closed_candle               # ⭐ gatilho de decisão
test_candle_aggregation_1m_to_5m
test_candle_aggregation_handles_missing_minute
test_candle_close_triggers_event_exactly_once
test_partial_candle_is_never_used_for_indicators             # ⭐

# WebSocket
test_ws_ping_sent_every_20_seconds
test_ws_pong_detected_on_public_channel                      # ⭐ ret_msg=="pong", op=="ping"
test_ws_pong_detected_on_private_channel                     # ⭐ op=="pong"
test_ws_reconnects_with_exponential_backoff
test_ws_resubscribes_after_reconnect
test_ws_reconnect_triggers_full_rest_resync                  # ⭐ não há replay
test_ws_heartbeat_timeout_marks_connection_dead
test_ws_private_auth_signs_get_realtime_plus_expires         # ⭐ formato exato

# Sincronização de relógio
test_clock_offset_measured_against_server_time
test_clock_ahead_by_more_than_1000ms_is_fatal                # ⭐ assimetria da janela
test_clock_drift_beyond_threshold_triggers_halt

# Validação
test_data_older_than_threshold_is_marked_stale
test_conflicting_last_price_vs_mark_price_is_flagged
test_zero_or_negative_price_is_rejected
test_spread_wider_than_limit_is_flagged
test_clock_skew_beyond_threshold_triggers_halt
```

**Ferramenta necessária:** `tools/mock_bybit/` — servidor WS + REST fake, controlável por script, capaz de emitir gaps de sequência, desconexões e dados corrompidos sob demanda. Construir isso agora paga em todos os sprints seguintes.

**Saída:** rodar 24h contra a Bybit **testnet** em modo somente-leitura. Zero divergências entre o livro local e um snapshot REST tirado a cada 5 min.

---

### Sprint 3 — Feature Engine (4 dias)

**Entregável:** snapshot de mercado completo, determinístico, validado contra o schema.

**Testes primeiro:**
```
# Indicadores — valores de referência calculados à mão em fixtures
test_atr_matches_reference_values
test_atr_with_insufficient_candles_returns_none              # ⭐ nunca improvisa
test_ema_matches_reference_values
test_realized_volatility_matches_reference
test_volatility_percentile_over_lookback_window

# Estrutura
test_swing_high_detection_with_known_series
test_swing_low_detection_with_known_series
test_break_of_structure_up
test_break_of_structure_down
test_no_bos_in_ranging_market
test_structure_is_stable_when_new_candle_does_not_change_it  # property

# Liquidez
test_spread_bps_calculation
test_orderbook_imbalance_calculation
test_estimated_slippage_walks_the_book                       # ⭐ não é constante
test_estimated_slippage_grows_with_size                      # property monotônica
test_illiquid_book_produces_high_slippage_estimate

# Snapshot
test_snapshot_validates_against_schema
test_snapshot_is_deterministic_for_same_input                # property
test_snapshot_includes_data_age_ms
test_snapshot_with_missing_indicator_sets_field_null_not_zero  # ⭐
test_snapshot_regime_classification_trending_up
test_snapshot_regime_classification_range
test_snapshot_regime_unknown_when_data_insufficient
```

O teste `..._sets_field_null_not_zero` é crítico: `0` e "não sei" são coisas diferentes, e o modelo tratará `0` como um valor real. Nunca preencher com zero.

---

### Sprint 4 — Risk Engine (7 dias) ⭐ SPRINT MAIS IMPORTANTE

**Entregável:** biblioteca pura que decide aprovado/rejeitado e calcula a quantidade. Zero I/O.

Este sprint tem o dobro de densidade de testes dos outros. É onde o dinheiro vive.

**Testes primeiro — Sizing:**
```
test_position_size_basic_calculation
test_position_size_includes_fees_in_risk_per_unit            # ⭐
test_position_size_includes_slippage_in_risk_per_unit        # ⭐
test_position_size_rounds_down_to_qty_step
test_position_size_capped_by_max_exposure
test_position_size_capped_by_max_leverage
test_position_size_capped_by_available_liquidity
test_position_size_capped_by_symbol_limit
test_position_size_below_min_qty_rejects_trade               # ⭐ não arredonda para cima
test_position_size_with_tiny_stop_distance_hits_exposure_cap
test_position_size_zero_when_risk_budget_exhausted
```

**Testes primeiro — Validadores (cada um isolado, depois compostos):**
```
test_reject_when_daily_loss_limit_reached
test_reject_when_weekly_loss_limit_reached
test_reject_when_projected_loss_would_breach_daily_limit     # ⭐ preventivo
test_reject_when_max_positions_reached
test_reject_when_symbol_not_in_allowlist
test_reject_when_rr_net_below_minimum
test_reject_when_spread_above_limit
test_reject_when_estimated_slippage_above_limit
test_reject_when_data_is_stale
test_reject_when_stop_on_wrong_side_of_entry
test_reject_when_stop_beyond_liquidation_price               # ⭐
test_reject_when_entry_too_far_from_invalidation
test_reject_when_conflicting_position_exists
test_reject_when_conflicting_order_exists
test_reject_when_consecutive_losses_trigger_cooldown
test_reject_when_daily_entry_count_exceeded
test_reject_when_intent_expired
test_reject_when_intent_expiry_in_the_past
test_reject_when_take_profit_fractions_exceed_one
test_reject_when_averaging_down_detected                     # ⭐ anti-martingale
test_reject_when_stop_widening_detected                      # ⭐
test_reject_during_blocked_hours
test_reject_when_volatility_regime_abnormal
test_all_validators_run_even_when_first_fails                # ⭐ relatório completo
test_rejection_reason_is_machine_readable_code
```

**Testes primeiro — Circuit breakers (cada um do §4 da spec):**
```
test_breaker_daily_loss_exceeded
test_breaker_weekly_loss_exceeded
test_breaker_position_mismatch_local_vs_exchange
test_breaker_duplicate_order_detected
test_breaker_private_ws_disconnected_too_long
test_breaker_market_data_stale
test_breaker_spread_out_of_bounds
test_breaker_slippage_out_of_bounds
test_breaker_filled_qty_exceeds_expected
test_breaker_open_position_without_tp_sl                     # ⭐
test_breaker_api_error_rate_exceeded
test_breaker_equity_or_margin_inconsistent
test_breaker_model_output_invalid_repeatedly
test_breaker_abnormal_volatility_change
test_breaker_symbol_suspended_or_spec_changed
test_breaker_server_clock_desync
test_halt_cancels_pending_entries
test_halt_preserves_open_positions_per_policy
test_halt_requires_manual_release
test_halt_is_idempotent
test_halt_emits_alert_exactly_once
```

**Property tests:** as 9 propriedades do §3.3, todas neste sprint.

**Saída bloqueante:**
- Cobertura de branch em `risk/` = **100%**. Sem exceções.
- Mutation score ≥ 90% (`mutmut run --paths-to-mutate src/bybit_agent/risk/`).
- Teste arquitetural confirma zero imports de I/O.
- Revisão manual linha a linha de `sizing.py` e `validators.py` por alguém que não os escreveu.

---

### Sprint 5 — Máquina de estados + Persistência (4 dias)

**Testes primeiro:**
```
test_all_valid_transitions_are_accepted
test_all_invalid_transitions_are_rejected                    # property, grafo completo
test_state_is_persisted_before_side_effect                   # ⭐ write-ahead
test_crash_between_state_write_and_side_effect_is_recoverable
test_restart_rebuilds_state_from_events
test_restart_queries_exchange_and_reconciles                 # ⭐ nunca confia no local
test_reconciliation_detects_position_mismatch
test_reconciliation_detects_orphan_order
test_reconciliation_detects_unexpected_position
test_reconciliation_mismatch_triggers_halt
test_event_log_is_append_only
test_event_log_rejects_update_and_delete
test_projection_is_rebuildable_from_scratch
test_projection_matches_incremental_after_full_rebuild       # property
test_cooldown_expires_after_configured_duration
test_data_stale_state_blocks_all_mutations
```

---

### Sprint 6 — Execution Gateway (6 dias)

**Testes primeiro:**
```
# Idempotência (D10 revisada — duas camadas)
test_order_link_id_is_deterministic_from_decision_id
test_order_link_id_fits_36_chars_and_allowed_charset         # ⭐ limite real da Bybit
test_same_intent_submitted_twice_creates_one_order           # ⭐
test_retry_NEVER_happens_without_state_query_first           # ⭐⭐ camada 2, normativa
test_inconclusive_state_query_triggers_reconciliation_not_retry  # ⭐
test_retcode_110072_is_treated_as_probable_success_not_error # ⭐ duplicata ≠ falha
test_duplicate_order_link_id_triggers_reconciliation

# Rate limiting (D14)
test_all_rest_calls_go_through_governor                      # ⭐ teste arquitetural
test_governor_enforces_global_ip_budget_at_50pct_of_limit
test_governor_reserves_emergency_budget_for_exit_operations  # ⭐
test_read_call_is_refused_before_exit_call_is_blocked        # ⭐
test_governor_adjusts_from_response_headers
test_retcode_10018_ip_ban_triggers_halt
test_retcode_10006_backs_off_exponentially

# Frescor de patrimônio (D13)
test_equity_is_fetched_via_rest_before_every_risk_decision   # ⭐⭐
test_cached_equity_is_never_used_for_risk_evaluation         # ⭐
test_equity_polling_accelerates_when_position_is_open
test_wallet_ws_absence_of_snapshot_is_handled_at_boot        # ⭐

# Fluxo de ordem
test_gateway_revalidates_all_limits_before_send              # ⭐ segunda checagem
test_gateway_queries_position_and_orders_before_send
test_order_acceptance_is_not_treated_as_fill                 # ⭐
test_fill_confirmed_only_via_private_ws
test_fill_handler_is_idempotent_per_order_id                 # ⭐⭐ Filled pode vir 2x
test_duplicate_filled_event_does_not_double_count_position   # ⭐
test_cancelled_status_with_executed_qty_is_handled           # ⭐ Cancelled ≠ nada
test_execution_stream_filters_non_trade_exec_types           # ⭐ Funding/Settle
test_funding_exec_type_is_recorded_as_position_cost
test_adl_or_bust_trade_triggers_immediate_halt               # ⭐ liquidação
test_execution_dedup_by_exec_id
test_tp_sl_uses_full_mode_covering_whole_position            # ⭐ D11
test_partial_fill_is_covered_by_full_mode_tp_sl              # ⭐
test_tp_sl_installed_after_fill_confirmation
test_position_without_confirmed_tp_sl_triggers_halt
test_position_list_tpsl_mode_field_is_ignored                # ⭐ deprecado, sempre "Full"
test_position_parses_entry_price_not_avg_price               # ⭐ nome real do campo
test_close_position_uses_reduce_only
test_reduce_position_respects_max_reduction

# Falhas
test_api_timeout_does_not_resend_blindly
test_api_5xx_retries_with_backoff
test_api_4xx_does_not_retry
test_rate_limit_respects_retry_after
test_ambiguous_response_triggers_reconciliation
test_ws_disconnect_during_fill_recovers_via_rest
test_exchange_rejects_order_records_reason

# Segurança
test_api_secret_never_appears_in_logs                        # ⭐ varre todos os logs
test_api_secret_never_appears_in_exception_traceback
test_gateway_refuses_to_start_without_ip_allowlist_configured
test_demo_and_prod_credentials_cannot_be_mixed
```

**Ferramenta:** estender `tools/mock_bybit/` para simular: aceite-sem-fill, fill parcial, fill duplicado, timeout, 429, desconexão no meio do fill.

**Saída:** 500 ordens contra o mock, com falhas injetadas aleatoriamente em 20% das chamadas. Zero ordens duplicadas. Zero posições sem TP/SL.

---

### Sprint 7 — Agente Claude + MCP (5 dias)

**Testes primeiro — Cliente e prompt:**
```
test_model_id_is_claude_opus_4_8
test_thinking_is_explicitly_adaptive                         # ⭐ off por padrão
test_no_temperature_or_top_p_in_request                      # ⭐ 400 se presente
test_no_assistant_prefill_in_messages                        # ⭐ 400 se presente
test_output_config_format_uses_decision_schema
test_all_tools_have_strict_true
test_tool_choice_disables_parallel_tool_use
test_system_prompt_hash_is_recorded_with_each_decision       # ⭐ versionamento
test_system_prompt_contains_no_dynamic_values                # ⭐ cache
test_tools_are_serialized_deterministically                  # sort_keys, ordem estável
test_cache_control_is_on_last_system_block
test_second_call_reports_cache_read_tokens_greater_than_zero # ⭐ integração
test_snapshot_goes_in_user_message_not_system                # ⭐ cache
test_risk_policy_goes_in_user_message_not_system

# Parser
test_valid_decision_parses_to_domain_object
test_prices_parse_as_decimal_never_float
test_unknown_action_is_rejected
test_refusal_stop_reason_is_handled                          # ⭐ não lê content
test_max_tokens_stop_reason_is_handled
test_pause_turn_is_handled
test_api_error_does_not_crash_orchestrator
test_repeated_api_failure_triggers_halt

# Prompt injection
test_market_data_containing_instructions_is_ignored          # ⭐
test_symbol_name_with_injected_text_is_sanitized
test_tool_result_is_never_treated_as_instruction

# Orquestrador
test_agent_is_called_on_candle_close_not_on_tick             # ⭐
test_agent_is_called_on_regime_change
test_agent_is_not_called_when_data_is_stale
test_agent_is_not_called_when_halted
test_at_most_one_mutating_tool_call_per_cycle                # ⭐
test_decision_is_persisted_before_being_acted_on
test_full_response_is_logged_verbatim                        # auditoria
```

**Testes primeiro — Ferramentas MCP (por ferramenta):**
```
test_get_market_snapshot_returns_validated_snapshot
test_get_market_snapshot_fails_loudly_when_stale
test_get_account_risk_state_never_exposes_credentials
test_submit_trade_intent_delegates_to_risk_engine            # ⭐ não decide nada
test_submit_trade_intent_returns_rejection_with_reason_codes
test_submit_trade_intent_rejects_qty_field_in_input          # ⭐ modelo não escolhe qty
test_close_position_always_sets_reduce_only
test_halt_trading_is_idempotent
test_every_tool_call_is_audited
test_every_tool_has_strict_schema
test_tool_input_is_validated_before_execution
test_tool_output_is_sanitized                                # sem instruções embutidas
test_mcp_server_never_forwards_received_tokens               # ⭐ spec MCP
test_mcp_rate_limits_per_tool
test_mcp_tool_timeout_is_enforced
```

O teste `test_submit_trade_intent_rejects_qty_field_in_input` é a expressão em código da regra central da sua spec: o modelo produz *intenção*, nunca *tamanho*. Se o schema da ferramenta aceitasse `qty`, toda a arquitetura estaria comprometida.

---

### Sprint 8 — Shadow mode + Backtest (6 dias)

**Entregável:** o sistema roda ponta a ponta gerando decisões, sem enviar ordens. Backtest realista.

**Testes primeiro:**
```
# Shadow
test_shadow_mode_never_calls_execution_gateway               # ⭐ teste arquitetural
test_shadow_mode_persists_snapshot_and_full_response
test_shadow_mode_simulates_fill_with_realistic_costs
test_shadow_mode_tracks_hypothetical_pnl
test_shadow_mode_records_rejection_reasons

# Backtest — realismo
test_backtest_does_not_fill_at_candle_close                  # ⭐ regra da sua spec
test_backtest_applies_maker_and_taker_fees_correctly
test_backtest_applies_spread_to_entry_and_exit
test_backtest_applies_slippage_model
test_backtest_simulates_partial_fills
test_backtest_simulates_order_latency
test_backtest_handles_price_gaps
test_backtest_applies_funding_at_funding_times
test_backtest_simulates_order_cancellation
test_backtest_handles_incomplete_data_periods
test_backtest_handles_simulated_restart
test_backtest_handles_simulated_disconnection
test_backtest_limit_order_fills_only_if_price_traded_through # ⭐
test_backtest_stop_can_be_gapped_through                     # ⭐ pior caso, não melhor
test_backtest_is_deterministic_given_seed                    # property
test_backtest_never_uses_future_data                         # ⭐ anti-lookahead
```

`test_backtest_never_uses_future_data` merece implementação específica: o motor de replay expõe apenas dados com timestamp ≤ o "agora" simulado. Um teste tenta acessar candle futuro e espera `LookaheadError`.

**Saída:** relatório de 90 dias em shadow mode com todas as métricas do §9 da spec.

---

### Sprint 9 — Demo trading (4 dias + 2 semanas de operação)

**Entregável:** operação real no ambiente Demo Trading da Bybit.

**Testes primeiro:**
```
test_demo_environment_uses_separate_credentials
test_demo_credentials_cannot_reach_prod_endpoint             # ⭐
test_prod_mode_requires_explicit_confirmation_flag
test_startup_reconciliation_runs_before_any_trading
test_startup_halts_if_reconciliation_fails
```

**Critérios de saída (todos obrigatórios, medidos em 2 semanas):**

| Critério | Alvo |
|---|---|
| Ordens duplicadas | 0 |
| Posições sem TP/SL confirmado na corretora | 0 |
| Divergências posição local vs. Bybit após reinício | 0 |
| Circuit breakers testados em produção-demo | ≥ 8 dos 16, disparados de propósito |
| Violações de limite de risco | 0 |
| Eventos sem registro de auditoria | 0 |
| Reinícios não planejados recuperados corretamente | 100% |
| Erro de cálculo de quantidade (vs. recálculo manual) | 0 |

**Nenhum destes é negociável para avançar à Fase 6.**

---

### Sprint 10 — Observabilidade e alertas (3 dias)

**Lacuna do plano v1.0:** o §9 da spec lista métricas, mas nenhum sprint era dono delas. Um circuit breaker que dispara sem ninguém saber é equivalente a não ter circuit breaker.

**Entregável:** métricas, alertas acionáveis e runbook.

**Testes primeiro:**
```
test_every_circuit_breaker_emits_an_alert                    # ⭐ 16/16
test_alert_is_delivered_even_when_database_is_down           # ⭐ caminho independente
test_alert_delivery_failure_is_itself_alerted
test_halt_alert_has_highest_severity_and_no_rate_limit       # ⭐ nunca suprimido
test_metrics_never_contain_api_credentials
test_audit_log_is_queryable_by_decision_id
test_every_decision_links_to_snapshot_prompt_hash_and_outcome  # ⭐ rastreabilidade
test_daily_report_reconciles_local_pnl_against_exchange       # ⭐
```

**Métricas mínimas** (§9 da spec + operacionais):
- Negócio: expectativa/trade, profit factor, drawdown máx. e intradiário, retorno por unidade de risco, resultado por regime e por setup.
- Execução: slippage previsto vs. real, taxas sobre lucro bruto, % de sinais expirados, % de fills parciais.
- Controle: trades bloqueados pelo motor de risco (por código de rejeição), divergências local vs. Bybit, disparos de circuit breaker.
- Infra: utilização do orçamento de rate limit, latência de decisão, idade dos dados, uptime do WS, taxa de cache hit do prompt, custo de API por decisão.

**Alertas que exigem ação humana imediata:** halt, divergência de posição, posição sem TP/SL, ban de IP, drift de relógio, WS privado caído > 30s.

**Runbook obrigatório:** o que fazer em cada halt, como liberar manualmente, como fechar posição de emergência sem o sistema, como renovar chaves de demo (expiram em 7 dias).

---

### Sprint 11 — Produção com aprovação humana (Fase 6 da spec)

**O plano v1.0 parava no demo.** Esta é a ponte para capital real.

**Testes primeiro:**
```
test_prod_mode_requires_explicit_confirmation_flag
test_prod_mode_requires_human_approval_before_every_entry    # ⭐
test_approval_expires_if_not_given_within_window             # ⭐ mercado se move
test_approval_is_for_a_specific_decision_id_only             # ⭐ não é blanket
test_expired_approval_cannot_be_reused
test_exit_and_protection_never_require_approval              # ⭐⭐ assimetria
test_halt_never_requires_approval
test_prod_risk_limits_are_stricter_than_demo
```

**A assimetria é o ponto central:** aprovação humana é exigida para **abrir** risco, nunca para **reduzir** risco. Encerramento, redução, ajuste protetivo de stop e halt executam sozinhos. Um humano indisponível nunca pode impedir a saída de uma posição.

**Critérios de entrada:** todos os do Sprint 9 + Sprint 10 concluído + runbook exercitado ao menos uma vez em simulação.

**Capital inicial:** o mínimo que a corretora permite operar. O objetivo desta fase não é lucro — é provar que o comportamento em demo se reproduz com dinheiro real, onde o livro reage às nossas ordens e a psicologia do operador entra em jogo.

**Fase 7 (autonomia limitada) permanece fora do escopo deste plano.** Só faz sentido especificá-la com os dados operacionais das Fases 5 e 6 em mãos.

---

## 5. Contratos-chave

### 5.1 Schema de decisão (resumo — completo em `contracts/decision_v1.json`)

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["decision_id","timestamp","symbol","action","data_quality",
               "market_regime","setup","entry","risk_plan",
               "cancellation_conditions","reason_codes","summary"],
  "properties": {
    "decision_id": {"type":"string","format":"uuid"},
    "timestamp":   {"type":"string","format":"date-time"},
    "symbol":      {"type":"string","enum":["BTCUSDT"]},
    "action":      {"type":"string","enum":["NO_TRADE","WATCH","OPEN_LONG",
                    "OPEN_SHORT","ADJUST_STOP","TAKE_PARTIAL",
                    "CLOSE_POSITION","HALT_TRADING"]},
    "risk_plan": {
      "type":"object","additionalProperties":false,
      "required":["invalidation_price","stop_loss","take_profit_levels",
                  "estimated_rr_gross","estimated_rr_net","maximum_slippage_bps"],
      "properties": {
        "stop_loss": {"type":["string","null"]},
        "take_profit_levels": {
          "type":"array",
          "items":{"type":"object","additionalProperties":false,
                   "required":["price","close_fraction","reason"],
                   "properties":{"price":{"type":"string"},
                                 "close_fraction":{"type":"string"},
                                 "reason":{"type":"string"}}}
        }
      }
    }
  }
}
```

**Nota:** `enum` no símbolo trava o modelo em BTCUSDT no nível da decodificação. `close_fraction` é `string` (decimal) — a faixa 0–1 é validada em código.

### 5.2 Política de risco (imutável, carregada de arquivo assinado)

```python
@dataclass(frozen=True, slots=True)
class RiskPolicy:
    max_risk_per_trade: Decimal      # 0.0025
    max_total_risk: Decimal          # 0.0050
    max_daily_loss: Decimal          # 0.0100
    max_weekly_loss: Decimal         # 0.0300
    max_concurrent_positions: int    # 1
    max_leverage: Decimal            # 2
    min_rr_net: Decimal              # 2.0
    max_consecutive_losses: int      # 2
    max_daily_entries: int           # 3
    max_spread_bps: Decimal
    max_slippage_bps: Decimal
    max_data_age_ms: int
    allowed_symbols: frozenset[str]  # {"BTCUSDT"}
    policy_hash: str                 # SHA-256 do arquivo, gravado em cada decisão
```

Carregada uma vez no boot, de arquivo em disco. **Nunca** de variável de ambiente, nunca do prompt, nunca da API. O `policy_hash` vai no event log de toda decisão — se a política mudar, é auditável quando e qual decisão usou qual versão.

---

## 6. Riscos do projeto e mitigações

| Risco | Prob. | Impacto | Mitigação |
|---|---|---|---|
| Livro de ofertas local diverge silenciosamente | Alta | Alto | Snapshot REST de reconciliação a cada 5 min; gap de seq força resync. Sprint 2. |
| Fill parcial deixa TP/SL com tamanho errado | Média | **Crítico** | TP/SL só instalado após confirmação de fill, com a qty efetivamente executada. Teste dedicado. Sprint 6. |
| Retry após timeout duplica ordem | Média | **Crítico** | **Dedup de `orderLinkId` não é garantida pela doc.** Defesa real = consulta de estado obrigatória antes de todo reenvio. Spike S-2 mede a camada 1. Sprint 6. |
| **Ban de IP (600 req/5s) com posição aberta** | Baixa | **Crítico** | Sem capacidade de enviar ordem de saída por 10 min = perda descontrolada. `RateLimitGovernor` com teto em 50% e reserva de emergência para saídas. Sprint 6. |
| Evento `Filled` duplicado infla posição contabilizada | Média | Alto | Handler idempotente por `orderId`; dedup por `execId`. Comportamento documentado pela Bybit. Sprint 6. |
| Decisão de risco com patrimônio defasado | Média | **Crítico** | WS de wallet não atualiza PnL não realizado. REST síncrono antes de toda decisão (D13). Sprint 6. |
| Merge incorreto de `tickers` zera funding/OI | Média | Alto | Linear é snapshot+delta; campo ausente = inalterado. Teste dedicado. Sprint 2. |
| Assinatura HMAC falha de forma intermitente | Média | Médio | Assinar e enviar a **mesma string literal**; nunca re-serializar. Property test sobre ordem de chaves. Sprint 6. |
| Modelo produz decisão coerente mas errada | Alta | Médio | Risk Engine é a autoridade. Shadow mode de 90 dias antes de qualquer ordem real. |
| Prompt injection via dados de mercado | Baixa | Alto | Sanitização de outputs de ferramenta + regra explícita no prompt + teste dedicado. Sprint 7. |
| Cache do prompt não funciona → custo alto | Média | Baixo | Teste de integração verifica `cache_read_input_tokens > 0`. Sprint 7. |
| Backtest otimista demais → falsa confiança | **Alta** | Alto | Regras de fill pessimistas por padrão; teste anti-lookahead; comparação shadow vs. backtest no mesmo período. Sprint 8. |
| Deriva entre política escrita e política executada | Média | Alto | `policy_hash` em cada decisão; política imutável; property test P7. |
| Mudança de especificação do instrumento pela Bybit | Baixa | Alto | Circuit breaker de spec alterada; verificação no boot e a cada 1h. |

---

## 7. Métricas de progresso do desenvolvimento

Acompanhe semanalmente:

| Métrica | Alvo |
|---|---|
| Cobertura de branch em `risk/` | 100% |
| Mutation score em `risk/` | ≥ 90% |
| Tempo da suite unit | < 10s |
| Testes escritos antes do código (amostragem de 20 commits) | 100% |
| Circuit breakers com teste de disparo real | 16/16 |
| Property tests com 1000+ exemplos | ≥ 9 |
| Ocorrências de `float` em caminho de dinheiro | 0 |
| `# type: ignore` sem justificativa | 0 |

---

## 8. Sequência de execução recomendada

```
Sprint -1 (spikes) ──► Sprint 0 ──► Sprint 1 ──┬──► Sprint 2 ──► Sprint 3 ──┐
   3d                     3d          2d       │      5d          4d        │
                                               │                            ├──► Sprint 5 ──► Sprint 6 ──► Sprint 7 ──► Sprint 8 ──► Sprint 9 ──► Sprint 10 ──► Sprint 11
                                               └──► Sprint 4 ───────────────┘       4d           6d           5d           6d           4d          3d
                                                       7d (paralelo — lib pura)                                                      +2 semanas
```

**Duas paralelizações valem a pena:**
- **Sprint -1 (spikes)** pode começar imediatamente, em paralelo com o Sprint 0 — só precisa de uma conta demo, não do código.
- **Sprint 4 (Risk Engine)** roda em paralelo com 2 e 3. É biblioteca pura, sem dependência de dados reais, e é o componente que mais se beneficia de tempo e revisão.

**Dependência dura:** o Sprint 6 (Execution Gateway) **não começa** com spike pendente. Especialmente o S-2.

Total estimado: **~52 dias de desenvolvimento** (46 do plano v1.0 + 3 de spikes + 3 de observabilidade), + 2 semanas de demo trading, + Sprint 11 antes de qualquer capital significativo.

---

## 9. Primeira ação

Duas frentes, em paralelo.

**Frente A — Spike S-2 (o mais importante):** criar conta Demo Trading, gerar chaves, e responder empiricamente se a Bybit deduplica `orderLinkId`. Um script de ~50 linhas. O resultado determina se a idempotência de ordens tem uma ou duas camadas reais de defesa — e isso precisa ser sabido **antes** de escrever o gateway.

**Frente B — Sprint 0, primeiro teste:**

```python
# tests/unit/domain/test_money.py
def test_money_rejects_float_construction():
    """Dinheiro nunca nasce de float. Nem uma vez."""
    with pytest.raises(TypeError, match="float"):
        Price(0.1)
```

Roda. Falha com `ImportError`. Aí sim escrevemos `domain/money.py`.

---

## 10. Índice de documentos

| Documento | Papel |
|---|---|
| `PLANO.md` (este) | Roadmap, metodologia TDD, decisões travadas |
| [`BYBIT_INTEGRACAO.md`](./BYBIT_INTEGRACAO.md) | **Normativo.** Contrato verificado da API Bybit V5 |
| `contracts/*.json` | Schemas versionados (decisão, snapshot, intenção, política) |
| `prompts/system_v0.1.md` | System prompt versionado; hash gravado em cada decisão |
| `docs/spikes/S-N-resultado.md` | Resultado de cada spike, com dados brutos |
| `docs/RUNBOOK.md` | Sprint 10 — procedimentos de halt, liberação, saída de emergência |
