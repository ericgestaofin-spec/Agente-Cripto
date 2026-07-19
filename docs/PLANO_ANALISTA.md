# Plano — Analista de Mercado Robusto (antes do Operador)

**Versão:** 1.0 · **Data:** 2026-07-19
**Objetivo:** transformar o analista v0 (que já funciona ao vivo) num analista
**sólido, robusto e auditável** — antes de construir qualquer execução.
**Metodologia:** TDD estrito, mesma disciplina do Risk Engine (100% branch nos
módulos críticos, valores de referência calculados à mão, gates bloqueantes).

---

## 0. Decisão de arquitetura — WebSocket **não** é a melhor escolha para o analista

Esta é a decisão mais importante do plano, e vai contra a intuição inicial.

### O que o analista realmente precisa

O analista **não** decide a cada tick. Pela própria spec, o Claude é chamado no
**fechamento de candle**, mudança de regime ou evento relevante — não em tempo
real contínuo. Um processo determinístico monitora o preço de entrada
continuamente, mas isso é o **operador**, não o analista.

Então o requisito do analista é: **um snapshot coerente e fresco no momento da
decisão** (fechamento de candle). Não é streaming.

### Por que WebSocket é a ferramenta errada aqui

O recurso matador do WebSocket é o **livro de ofertas reconstruído por delta,
sempre atual**. Mas reconstruir o livro por delta é a parte mais complexa e
arriscada de todo o sistema (a própria tabela de riscos do plano confirma):
reconexão, heartbeat, detecção de gap de sequência (que descobrimos **não** ser
documentada), e o livro local que pode divergir silenciosamente.

E para quê? O analista usa o livro para **spread, desequilíbrio e estimativa de
slippage** — no instante da decisão. Ele não precisa de cada atualização do
livro; precisa de uma boa foto quando decide. Manter um estado ao vivo complexo
para amostrá-lo ocasionalmente é **complexidade sem retorno** para o analista.

O livro reconstruído por delta é essencial para o **operador** (o entry watcher
monitorando preço em tempo real, o slippage no momento da execução, os fills via
stream privado). É **lá** que a complexidade do WebSocket se paga.

### A arquitetura escolhida: REST alinhado ao relógio

Para o analista:

```
Relógio alinhado ao candle  →  gatilho (fechamento de candle + buffer)
        ↓
Burst REST concorrente  →  snapshot coerente (todas as fontes ~ mesmo instante)
        ↓
Feature Engine  →  snapshot v2 estruturado
        ↓
Claude  →  decisão  →  Risk Engine  →  registro (shadow)
```

**Por que é melhor para o analista:**

| Critério | REST alinhado ao relógio | WebSocket + delta |
|---|---|---|
| Frescor no fechamento de candle | ✅ sub-segundo (fetch sob demanda) | ✅ contínuo |
| Coerência do snapshot | ✅ fetch concorrente (dezenas de ms) | ~ tópicos separados também |
| Gatilho de fechamento de candle | ✅ relógio + buffer (~1s) — irrelevante p/ candle de 5m | ✅ exato via `confirm` |
| Complexidade | ✅ baixa, stateless, testável | 🔴 reconexão, delta, gap, heartbeat |
| Risco de divergência silenciosa | ✅ nenhum (sem estado local) | 🔴 livro local diverge |
| Rate limit | ✅ ~6 req/candle vs 600/5s | — |

**Custo aceito:** o REST alinhado ao relógio não reage a eventos **intra-candle**
(um spike de volatilidade no meio do candle não dispara análise off-schedule).
Isso é uma melhoria de v1+, não um requisito do analista v0. Quando for
necessário, um **gatilho leve por WebSocket** (só o stream de kline, para detectar
`confirm` e eventos, SEM reconstruir o livro) pode ser adicionado — muito mais
simples que o livro por delta.

### Onde o WebSocket entra (fase do operador)

- Livro de ofertas ao vivo (entry watcher, slippage de execução).
- Streams privados: `execution` (fills), `order`, `position`, `wallet`.
- Aí a complexidade se justifica — ver `docs/BYBIT_INTEGRACAO.md` §4.

**Resumo da decisão:** REST-first, alinhado ao relógio, para o analista.
WebSocket adiado para o operador, onde seu recurso único (estado ao vivo
contínuo) é de fato necessário. É um caso em que a arquitetura mais simples é
genuinamente a melhor para a necessidade atual.

---

## 1. Decisão de persistência — SQLite no analista, Postgres no operador

O plano original usava Postgres (Docker) desde o início. Para o **analista**, isso
é peso desnecessário e um bloqueador (Docker não instalado).

- **Analista:** `sqlite3` (biblioteca padrão do Python, sem Docker, modo WAL).
  Suficiente para o event log de decisões e consultas de auditoria/métricas.
- **Operador:** Postgres (Docker) — a reconciliação de posição precisa de robustez
  e acesso concorrente; aí o Docker se justifica.

Isso remove o bloqueio do Docker de todo o trabalho do analista.

---

## 1.5. Controle de custo de IA (embutido desde o A1)

O custo do Claude é controlado por três camadas configuráveis, sem tocar no
código:

1. **Cadência** — o timeframe de decisão define a frequência. 1h ≈ 24
   chamadas/dia (~$1,40); 15m ≈ 96 (~$5,80); 5m ≈ 288 (~$17). Config: `DECISION_TF`.
2. **Pré-filtro determinístico** — o código só chama o Claude quando o snapshot
   é "interessante" (mudança de regime, quebra de estrutura, preço perto de
   nível). Candle sem evento → NO_TRADE sem gastar token. Corta ~5–7× o custo.
   É o que a spec pede: analisar em evento relevante, não a cada candle.
3. **Teto de orçamento diário** — `MAX_DAILY_COST_USD`. Ao atingir, o analista
   para de chamar o Claude (default NO_TRADE). Sem surpresa na fatura.

Combinado: 1h + pré-filtro + teto ≈ **< $1/dia** rodando 24/7, subindo só quando
há algo real. Prompt caching (já funcionando) reduz ainda mais o input.

O pré-filtro básico (cadência + saúde dos dados) entra no A1; o pré-filtro
inteligente (regime/estrutura/nível) entra no A2, quando há sinais para filtrar.

## 2. Estado atual (ponto de partida)

Já construído e testado ao vivo (265 testes no analisador):
- `marketdata/rest.py` — cliente REST público (candles, book, ticker, instrumento)
- `features/indicators.py` — ATR, EMA, volatilidade realizada
- `features/snapshot.py` — snapshot v1 (regime por EMA, spread, funding)
- `agent/` — prompt, client (Opus 4.8), parser, orquestrador shadow
- `risk/` — Risk Engine completo (100% branch + mutation)

Lacunas honestas (o que este plano fecha):
1. REST polling sem alinhamento; snapshot não-coerente entre fontes
2. Único timeframe (5m); a spec quer 4h/1h/15m/5m
3. Indicadores mínimos; faltam estrutura, imbalance, slippage, níveis
4. Sem persistência das decisões (sem auditoria nem backtest)
5. Frescor por uma fonte só; skew de relógio rudimentar (causou `data_age` negativo)

---

## 3. Fases

Cada fase: **entregável verificável**, **testes escritos primeiro**, **critério de
saída bloqueante**. Ordem por dependência.

### Fase A1 — Fundação de dados: coerente, fresca, alinhada ao relógio

**Objetivo:** o snapshot passa a ser temporalmente coerente e com frescor honesto.

**Módulos:**
- `marketdata/clock.py` — serviço de skew de relógio (GET /v5/market/time)
- `marketdata/coherent.py` — coleta concorrente com timestamp por fonte
- `marketdata/validator.py` — validação de sanidade dos dados
- `marketdata/scheduler.py` — gatilho alinhado ao fechamento de candle

**Testes primeiro:**
```
# Clock skew
test_clock_offset_computed_from_server_time
test_clock_offset_uses_round_trip_midpoint          # (t_recv+t_send)/2 - t_server
test_clock_skew_beyond_threshold_flags_unhealthy
test_corrected_now_applies_offset

# Snapshot coerente
test_coherent_fetch_gets_all_sources_concurrently
test_each_source_is_timestamped_individually
test_data_age_uses_oldest_source                    # frescor = a fonte mais velha
test_data_age_is_never_negative                     # ⭐ o bug que o Claude achou
test_snapshot_ts_uses_server_corrected_time

# Validação
test_stale_source_is_flagged
test_crossed_book_is_rejected                        # best_bid >= best_ask
test_last_price_outside_book_is_flagged              # ⭐ o outro bug do Claude
test_negative_or_zero_price_is_rejected
test_ticker_last_below_bid_is_flagged
test_validation_produces_machine_readable_issues

# Scheduler alinhado ao candle
test_next_candle_close_computed_from_interval        # 5m fecha em :00,:05,...
test_scheduler_triggers_after_close_plus_buffer
test_scheduler_skips_when_data_unhealthy
test_scheduler_is_deterministic_given_clock
```

**Saída:** rodar 2h ao vivo; `data_age_ms` sempre positivo e < 2s; zero snapshots
com book cruzado ou last fora do book passando como VALID.

### Fase A2 — Multi-timeframe e features ricas

**Objetivo:** o snapshot vira o objeto completo da spec — contexto multi-timeframe,
estrutura de mercado, liquidez real.

**Módulos:**
- `features/structure.py` — swings (topos/fundos), break of structure
- `features/liquidity.py` — desequilíbrio do livro, slippage andando o book
- `features/levels.py` — suportes/resistências candidatos (de swings)
- `features/multi_tf.py` — tendência por timeframe e alinhamento
- `features/snapshot.py` v2 — monta tudo

**Testes primeiro:**
```
# Estrutura
test_swing_high_detection_reference_series
test_swing_low_detection_reference_series
test_break_of_structure_up / _down
test_no_bos_in_range
test_structure_stable_when_candle_does_not_change_it   # property

# Liquidez
test_book_imbalance_calculation
test_slippage_walks_the_book_reference                 # ⭐ não é constante
test_slippage_grows_with_size                          # property monotônica
test_illiquid_book_high_slippage

# Multi-timeframe
test_trend_per_timeframe_4h_1h_15m_5m
test_regime_uses_higher_tf_context                     # contexto vem do 4h/1h
test_conflicting_timeframes_yield_transition
test_candidate_levels_from_swings

# Snapshot v2
test_snapshot_v2_validates_against_schema              # contracts/snapshot_v1.json
test_snapshot_v2_multi_tf_trend_present
test_missing_indicator_is_null_not_zero                # ⭐ mantém a regra
test_snapshot_v2_is_deterministic                      # property
```

**Saída:** snapshot v2 valida contra `contracts/snapshot_v1.json` (a ser criado);
rodar ao vivo e inspecionar que estrutura/níveis/imbalance refletem o mercado real.

### Fase A3 — Persistência e observabilidade das decisões

**Objetivo:** toda decisão é registrada de forma auditável; métricas do analista.

**Módulos:**
- `persistence/decision_log.py` — event log append-only em SQLite (WAL)
- `persistence/schema.sql` — tabela de decisões
- `observability/analyst_metrics.py` — agregações

**O que cada decisão registra:**
- snapshot íntegro (JSON), timestamp, data_age por fonte
- resposta integral do Claude, hash do prompt (versão), tokens e custo
- decisão parseada, ação, veredito do Risk Engine (aprovado/rejeitado + motivos)
- RR calculado, binding constraint

**Testes primeiro:**
```
test_decision_log_is_append_only
test_decision_log_rejects_update_and_delete
test_every_decision_links_snapshot_prompt_hash_and_outcome  # ⭐ rastreabilidade
test_decision_queryable_by_id
test_decision_queryable_by_action_and_regime
test_cost_is_recorded_per_decision
test_cache_hit_rate_is_tracked
test_log_survives_restart                                   # reabre o arquivo
test_no_secret_ever_written_to_log                          # ⭐ sem credenciais
```

**Métricas mínimas:** % NO_TRADE, decisões por regime, custo/decisão, taxa de cache
hit, distribuição de reason_codes, taxa de rejeição do Risk Engine por código.

**Saída:** rodar 24h em shadow; inspecionar o log; reconstruir a série de decisões
por consulta SQL.

### Fase A4 — Loop shadow resiliente e avaliação

**Objetivo:** o analista roda continuamente, sozinho, de forma robusta; e há como
avaliar a qualidade das decisões.

**Módulos:**
- `agent/shadow_loop.py` — loop contínuo alinhado ao scheduler, resiliente a falha
- `tools/review_decisions.py` — inspeção das decisões registradas
- `tools/replay_indicators.py` — replay histórico só dos INDICADORES (barato)

**Testes primeiro:**
```
test_shadow_loop_triggers_on_schedule
test_shadow_loop_survives_transient_api_error           # rede cai, ele continua
test_shadow_loop_backs_off_on_rate_limit
test_shadow_loop_halts_analysis_on_unhealthy_data       # não decide sobre lixo
test_shadow_loop_records_every_cycle
test_replay_indicators_matches_live_values              # determinismo do replay
test_replay_never_uses_future_data                      # ⭐ anti-lookahead
```

**Nota sobre backtest do analista:** replay histórico alimentando o **Claude** é
caro (~$600 por 90 dias de candles de 15m, ver PLANO §Fase 4). Portanto:
- **Barato agora:** replay histórico só dos INDICADORES/estrutura, para validar
  que a Feature Engine produz sinais corretos em dados reais passados.
- **Avaliação do Claude:** acumular decisões do shadow ao vivo ao longo de dias e
  analisá-las — mais barato e mais fiel que replay em massa.
- Replay LLM em massa fica como opção deliberada e cara, não default.

**Saída:** loop shadow rodando 48h sem intervenção, registrando toda decisão;
relatório de métricas gerado das decisões acumuladas.

---

## 4. O que este plano NÃO faz (fronteira explícita com o operador)

Para não haver ambiguidade:
- **Não** reconstrói livro por delta via WebSocket (é do operador).
- **Não** conecta streams privados (fills, posições — do operador).
- **Não** envia nenhuma ordem (nem tem código para isso).
- **Não** usa credencial de trading (o analista é 100% dados públicos + Claude).
- **Não** usa Postgres/Docker (SQLite basta para o analista).

Quando o analista estiver sólido, o operador entra com: WebSocket (livro + streams
privados), Execution Gateway, máquina de estados, reconciliação, Postgres — e aí
os spikes (S-2 etc.) e a credencial demo.

---

## 5. Sequência e estimativa

```
A1 (dados coerentes) → A2 (features ricas) → A3 (persistência) → A4 (loop + avaliação)
   ~4 dias               ~5 dias              ~2 dias             ~3 dias
```

A1 e A2 são o coração da qualidade de decisão. A3 e A4 tornam o analista auditável
e autônomo. Total ~14 dias, tudo sem credencial de trading e sem Docker.

**Gate de qualidade (todas as fases):** TDD estrito, cobertura de branch ≥ 95% em
`features/` e `marketdata/`, lint anti-float, snapshots validados contra schema,
determinismo verificado por property tests. O mesmo rigor do Risk Engine.

---

## 6. Primeira ação (quando aprovado)

Fase A1, primeiro teste, antes de qualquer código:

```python
# tests/unit/marketdata/test_clock.py
def test_data_age_is_never_negative():
    """O bug que o Claude achou ao vivo não pode voltar. O frescor usa o
    tempo corrigido pelo skew do servidor, capturado após a coleta."""
    ...
```
