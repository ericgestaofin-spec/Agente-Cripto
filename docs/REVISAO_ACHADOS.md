# Achados da Revisão Externa — Risk Engine

**Data:** 2026-07-19 · **Revisor:** IA externa · **Status:** em correção

Uma revisão externa do Risk Engine encontrou problemas reais. Todos os
cenários testáveis foram **confirmados empiricamente** antes de entrar
nesta lista (ver `scratchpad/confirm_findings.py`). Este documento tria os
achados por natureza e rastreia a correção.

## Correção de rota na linguagem dos documentos

O README e o pacote de revisão afirmavam que o Risk Engine é "a autoridade
final". **Isso era um overclaim.** O que está construído é a autoridade
sobre o **dimensionamento de um trade isolado, dado um snapshot honesto**.
A autoridade sobre portfólio, concorrência, execução real e inputs
adversariais exige a maquinaria dos Sprints 5–6, ainda não construída. Os
documentos foram ajustados para refletir isso.

---

## Triagem

### Categoria A — bugs reais no código atual (corrigir AGORA)

Cenários confirmados onde o código existente está demonstravelmente errado:
levanta exceção, aceita lixo, ou passa silenciosamente.

| # | Achado | Confirmado | Status |
|---|---|---|---|
| 5a | `side` inválido burla validação de stop e é aprovado | ✅ | corrigir |
| 5b | `entry=0` levanta `DivisionByZero` (viola contrato) | ✅ | corrigir |
| 5c | `NaN` entra em campo `Decimal` cru | ✅ | corrigir |
| 12 | `InstrumentSpec` aceita `tickSize=NaN`, `maxMktOrderQty=-1` | ✅ | corrigir |
| 13 | `frozen` é superficial: `list` mutável pode ser alterada | ✅ | corrigir |
| 11 | Política aceita limites absurdos (total 50%, leverage 1000x) | ✅ | **✔ A2** — tetos absolutos `_HARD_MAX_*` |
| 15 | Rebate de taxa negativo reduziria o risco (usar `max(fee,0)`) | analítico | **✔ A2** — `max(fee, 0)` |
| 14 | Cálculos principais fora do `localcontext` com traps | parcial | **✔ A2** — `compute_size` sob `localcontext` |
| — | Fração de TP individual (`0<f≤1`) não validada; `[-0.5, 1.5]` passa | ✅ | **✔ A1** |
| — | `invalidation` está no contexto mas nenhum validador a usa | ✅ | **✔ A2** — validador `_invalidation_coherence` |

**A1 (commitado 68382dd):** 5a, 5b, 5c, 12, 13 + frações de TP.
**A2:** 11, 15, 14, invalidation + aperto da invariante P4 (removida a
tolerância de um qtyStep — a versão estrita `q·entry ≤ equity·leverage` se
sustenta em 1000+ casos). 100% de branch e (em verificação) mutation.

### Categoria B — o modelo tem autoridade indevida sobre risco (violação do princípio central)

Estes violam o princípio de que o modelo não decide risco. São mudanças de
**contrato** (`TradeIntent`) que rippleiam para o schema MCP, o prompt e o
parser — merecem decisão explícita antes de implementar.

| # | Achado | Confirmado | Ação recomendada |
|---|---|---|---|
| 1 | `rr_net` é declarado pelo modelo, não calculado. RR=999 sem TP aprova | ✅ | Intent traz **preços de TP**; engine calcula RR. `rr_net` do modelo vira só diagnóstico |
| 4 | `liquidation` vem da intenção; `liq=1` faz stop parecer seguro | ✅ | Remover da intenção; engine estima internamente (risk tier); confirmar pós-fill |
| 10 | `is_averaging_down` e `widens_stop` são autorrelatados pelo modelo | ✅ | Computar do estado real da posição, não aceitar da intenção |
| 6 | Sizing usa preços não canonicalizados; gateway arredonda depois | analítico | Engine produz plano já alinhado a tick/step; recalcula RR/risco sobre ele |
| 7 | Slippage bps (validado) e monetário (sizing) são fontes independentes | ✅ | Fonte única; a prazo, curva de impacto do livro + sizing iterativo |

### Categoria C — corretos, mas pertencem a sprints já planejados

O revisor está certo que são bloqueadores de produção. Não são bugs na
biblioteca pura — exigem a maquinaria em torno dela. Já constam no plano.

| # | Achado | Sprint |
|---|---|---|
| 2 | `max_total_risk` não é imposto contra o risco de portfólio | 5 (snapshot com risco monetário) |
| 3 | Corrida TOCTOU aprovar↔executar; sem reserva atômica | 5 (máquina de estados) + 6 (idempotência, S-2) |
| 8 | `equity×leverage` não é margem disponível; falta risk tier | 5–6 (snapshot rico + risk tier) |
| 9 | Limites diário/semanal com denominador móvel (equity atual) | 5 (rastreio de sessão: day/week-start equity) |
| — | Proteção pós-fill (SL/TP confirmados) como pós-condição | 6 (gateway) |
| — | `RiskDecision` deveria ser um plano executável completo | 6 (integração gateway) |
| — | Freshness por fonte (mercado, livro, wallet, posições) | 2 + 5 |

---

## Nota sobre o achado 14

O revisor afirmou que um contexto Decimal externo (`getcontext().prec=6`)
mudaria o resultado. Para o input testado, **não mudou** (prec 28 e 6 deram
o mesmo `0.372`). O overclaim é dele aqui. Mas o ponto estrutural é válido:
os cálculos principais não rodam sob o `localcontext` com traps, então as
proteções (`InvalidOperation`, `DivisionByZero`) não estão ativas onde
deveriam — o próprio `DivisionByZero` do achado 5b confirma isso. Corrigir
por robustez, não porque o cenário específico reproduziu.

---

## Ordem de execução

1. **Batch A1** — validação de fronteira em runtime (fecha 5a, 5b, 5c, 12,
   13 e as frações de TP de uma vez). É a fundação: converte "levanta / passa
   silencioso" em "rejeição explícita".
2. **Batch A2** — `localcontext` nos cálculos, `max(fee,0)`, sanidade da
   política, validação da `InstrumentSpec`, coerência `invalidation`↔stop.
3. **Batch B** — mudanças de contrato (RR de TP, remover liquidação,
   computar averaging-down). Requer decisão sobre a forma da `TradeIntent`.
4. **Categoria C** — permanece nos Sprints 5–6, agora explicitamente
   rastreada aqui.
