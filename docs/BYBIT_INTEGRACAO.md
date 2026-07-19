# Contrato de Integração Bybit V5 — verificado

**Versão:** 1.0 · **Data:** 2026-07-19 · **Escopo:** `category=linear`, BTCUSDT, one-way mode

Este documento é a **fonte de verdade** para a camada de adaptação Bybit. Tudo aqui foi verificado contra a documentação oficial em 2026-07-19, com a URL da fonte. Itens marcados **[NV]** são **não verificados** — a documentação é silenciosa — e por isso viram *spikes* empíricos obrigatórios antes do Sprint 6.

> **Regra de ouro:** nada nesta camada é implementado com base em suposição. Se não está aqui com fonte, ou é um spike, ou não entra no código.

---

## 1. Ambientes e credenciais

| Ambiente | REST | WS público | WS privado |
|---|---|---|---|
| Produção | `https://api.bybit.com` | `wss://stream.bybit.com/v5/public/linear` | `wss://stream.bybit.com/v5/private` |
| Testnet | `https://api-testnet.bybit.com` | `wss://stream-testnet.bybit.com/v5/public/linear` | `wss://stream-testnet.bybit.com/v5/private` |
| **Demo Trading** | `https://api-demo.bybit.com` | ⚠️ **mainnet** (`wss://stream.bybit.com/v5/public/linear`) | `wss://stream-demo.bybit.com` |

### ⚠️ Consequência arquitetural do Demo Trading

**No Demo Trading, os dados de mercado vêm da mainnet real.** O ambiente demo só simula execução e saldo. Isso significa:

1. O Sprint 9 (demo) **não** valida o pipeline de market data em isolamento — ele já estará consumindo dados de produção desde o Sprint 2.
2. A configuração não é um único switch `ENV=demo`. São **dois eixos independentes**:
   ```
   MARKET_DATA_ENV ∈ {mainnet, testnet}
   EXECUTION_ENV   ∈ {mainnet, testnet, demo}
   ```
3. Combinações válidas precisam ser explicitamente listadas. `EXECUTION_ENV=demo` + `MARKET_DATA_ENV=testnet` é **inválido** — a doc avisa: *"it is meaningless to use demo trading service in the testnet website"*.
4. **Combinação proibida por código:** credencial de demo jamais pode atingir `api.bybit.com`, e vice-versa. Teste dedicado.

Chaves de demo são geradas **dentro do modo Demo Trading de uma conta mainnet** (UID separado), ou via `POST /v5/user/create-demo-member` chamado contra `api.bybit.com` com a chave de produção. Contas demo expiram em **7 dias** — o runbook precisa cobrir renovação.

Fontes: [Demo Trading](https://bybit-exchange.github.io/docs/v5/demo) · [WS Connect](https://bybit-exchange.github.io/docs/v5/ws/connect)

---

## 2. Autenticação

### REST — HMAC-SHA256

Headers: `X-BAPI-API-KEY`, `X-BAPI-TIMESTAMP` (epoch ms), `X-BAPI-SIGN`, `X-BAPI-RECV-WINDOW`.

> `X-BAPI-SIGN-TYPE` **[NV]** — não consta na doc V5 (era header da V3). **Não enviar.**

String assinada — a ordem de concatenação é exata:

```
GET:   timestamp + api_key + recv_window + queryString
POST:  timestamp + api_key + recv_window + rawJsonBodyString
```

Saída: HMAC-SHA256 em **hex minúsculo**.

**Armadilha de implementação — a mais provável fonte de bug nesta camada:**
O corpo assinado é a **string literal enviada, byte a byte**. Se você assinar `json.dumps(payload)` e depois deixar a biblioteca HTTP re-serializar o dict, a assinatura falha de forma intermitente e inexplicável (a ordem das chaves ou o espaçamento muda). O mesmo vale para o `queryString` do GET: a ordem dos parâmetros na string assinada tem que ser **idêntica** à da URL.

**Regra de código:** o cliente REST assina a string exata e envia **essa string** como body (`content=` em httpx, não `json=`). Teste dedicado com property-based testing sobre ordem de chaves.

### Janela de tempo — assimétrica

Regra verbatim da doc:
```
server_time - recv_window <= timestamp < server_time + 1000
```

**Você pode estar até `recv_window` atrasado, mas no máximo 1000 ms adiantado — e aumentar `recv_window` não compensa relógio adiantado.** Um servidor com relógio 2s à frente falha 100% das requisições, independentemente de `recv_window`.

- Default `recv_window`: **5000 ms**. Máximo: **[NV]**.
- Erro: `retCode 10002` — "The request time exceeds the time window range".

**Implicação:** o serviço de sincronização de relógio não é opcional. Deve rodar no boot e periodicamente, medindo o offset contra `/v5/market/time` e alertando se o drift passar de 500ms. Circuit breaker de "relógio dessincronizado" da spec original é confirmado como necessário.

### WebSocket privado

```json
{"req_id": "10001", "op": "auth", "args": ["api_key", 1662350400000, "signature"]}
```

- String assinada: `GET/realtime{expires}` — concatenação literal, sem separador.
- `expires`: epoch em **milissegundos**, no futuro (exemplo oficial usa `now + 1s`).
- HMAC-SHA256, hex minúsculo.
- Timeout para autenticar após abrir conexão: **[NV]**.

Fonte: [Guide/Auth](https://bybit-exchange.github.io/docs/v5/guide) · [WS Connect](https://bybit-exchange.github.io/docs/v5/ws/connect)

---

## 3. Especificação do instrumento

`GET /v5/market/instruments-info?category=linear&symbol=BTCUSDT`

Aninhamento exato dos campos que o Risk Engine consome:

```
priceFilter:    tickSize, minPrice, maxPrice
lotSizeFilter:  qtyStep, minOrderQty, maxOrderQty, minNotionalValue,
                maxMktOrderQty, postOnlyMaxOrderQty
leverageFilter: minLeverage, maxLeverage, leverageStep
```

**Atenção:** `maxOrderQty` (ordens limit) e `maxMktOrderQty` (ordens market) são **limites diferentes**. O sizing precisa aplicar o correto conforme o `orderType`.

`minNotionalValue` é uma restrição adicional independente de `minOrderQty` — uma quantidade acima do mínimo pode ainda ser rejeitada por notional insuficiente. Ambas entram nos validadores.

Fonte: [Instruments Info](https://bybit-exchange.github.io/docs/v5/market/instrument)

---

## 4. Market data — regras que quebram implementações ingênuas

### 4.1 Kline REST vem em ordem reversa

`GET /v5/market/kline` retorna array de arrays de **7 strings**:
```
[startTime, openPrice, highPrice, lowPrice, closePrice, volume, turnover]
```

**Ordenado do mais recente para o mais antigo.** Inverter antes de qualquer cálculo de indicador. Um ATR calculado sobre a série invertida produz um número plausível e completamente errado — é o tipo de bug que passa despercebido por semanas.

Teste obrigatório: `test_kline_rest_is_reversed_before_indicator_calculation`.

### 4.2 Kline WS — `confirm` marca candle fechado

Tópico `kline.{interval}.{symbol}`. Campo `confirm: true` significa candle fechado. `type` é sempre `"snapshot"`.

Isso **confirma** o gatilho de decisão do plano original: o agente é chamado no `confirm: true`, nunca em candle parcial.

### 4.3 Orderbook — correções importantes

**Profundidades disponíveis em linear: `1, 50, 200, 1000`.** Não existe depth 500 (premissa comum e errada).

| Depth | Frequência |
|---|---|
| 1 | 10ms |
| 50 | 20ms |
| 200 | 100ms |
| 1000 | 200ms |

**Escolha: `orderbook.50.BTCUSDT`.** 20ms é suficiente para estimar slippage num sistema que decide em fechamento de candle, e 50 níveis cobrem folgadamente o tamanho de posição da política (risco de 0,25%).

Estrutura: `type` (`snapshot`/`delta`), `data.b`/`data.a` (`[preço, size]`), `data.u` (updateId), `data.seq` (cross sequence), `ts`, `cts`.

**Regras de aplicação do delta (verbatim):**
- size `0` → **deletar** o nível
- preço que não existe → **inserir**
- preço que existe → **atualizar**
- ao receber um novo `snapshot` → **resetar o livro local completamente**
- `u == 1` → snapshot forçado por restart do serviço; sobrescrever o livro local

### ⚠️ 4.4 Detecção de gap de sequência NÃO é documentada

**Correção ao plano original.** A doc da Bybit **não** define regra de gap, **não** afirma que `u` deve ser consecutivo, e **não** manda ressubscrever em caso de salto. O único mecanismo de recuperação documentado é **passivo**: a Bybit reenvia um snapshot quando detecta problema do lado dela.

Pior: no **depth 1**, se o livro não muda por 3 segundos, a Bybit reenvia um snapshot **com o mesmo `u` da mensagem anterior**. Uma verificação ingênua de "u estritamente crescente" dispararia falso positivo.

**Decisão:** tratar `u` não-consecutivo como **heurística defensiva**, não como regra. Concretamente:
- Detectar salto → **não** ressubscrever imediatamente. Marcar o livro como `SUSPECT`.
- Validar contra snapshot REST (`/v5/market/orderbook`).
- Divergência confirmada → ressubscrever e marcar `DATA_STALE`.
- Registrar métrica `orderbook_gap_detected` e `orderbook_gap_confirmed_divergent` para medir a taxa de falso positivo da heurística em produção.

Isso substitui o teste `test_orderbook_detects_sequence_gap_and_requests_resync` do plano original, que codificava uma regra inexistente.

### 4.5 `tickers` em linear é incremental — snapshot **e** delta

Verbatim: *"If a response param is not found in the message, then its value has not changed."*

**Campo ausente ≠ campo zerado.** Um merge ingênuo que faz `ticker.update(msg["data"])` funciona; um que reconstrói o objeto a partir da mensagem zera `fundingRate`, `openInterest` e `markPrice` silenciosamente — e o snapshot vai para o Claude com dados falsos.

Teste obrigatório: `test_ticker_delta_merge_preserves_absent_fields`.

(Spot e Option são snapshot-only. Não confundir.)

### 4.6 Heartbeat — o formato do pong difere por canal

Envio: `{"op": "ping"}`, a cada **20 segundos** (recomendação oficial).
Timeout do servidor: **10 minutos** sem ping-pong nem dados. Configurável via `?max_active_time=` (30s–600s).

| Canal | Como identificar o pong |
|---|---|
| Público linear | `ret_msg == "pong"` — **`op` continua `"ping"`** |
| Privado | `op == "pong"` |

Um detector unificado que só procura `op == "pong"` nunca reconhece o pong do canal público e derruba a conexão por falso timeout.

Fontes: [Orderbook WS](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook) · [Ticker WS](https://bybit-exchange.github.io/docs/v5/websocket/public/ticker) · [Kline WS](https://bybit-exchange.github.io/docs/v5/websocket/public/kline) · [WS Connect](https://bybit-exchange.github.io/docs/v5/ws/connect)

---

## 5. Execução — o que muda no plano

### 5.1 orderLinkId

- **Máximo 36 caracteres**, apenas alfanuméricos, `-` e `_`.
- Se `orderId` e `orderLinkId` forem enviados juntos, a Bybit **prioriza `orderId`**.
- Duplicata em linear: `retCode 110072` — "OrderLinkedID is duplicate".

### 🔴 5.2 A decisão D10 está comprometida

**A janela de deduplicação do `orderLinkId` é [NV].** A documentação diz apenas que deve ser *"always unique"* — não especifica por quanto tempo a unicidade é imposta, nem garante que um reenvio duplicado seja rejeitado de forma confiável em vez de aceito.

O plano original travou a decisão D10 assumindo que `orderLinkId` determinístico dá idempotência ponta a ponta. **Isso é uma suposição não verificada sustentando o controle mais crítico do sistema** — a garantia de que um retry após timeout não duplica ordem.

**Correção — defesa em duas camadas, não uma:**

| Camada | Mecanismo | Confiança |
|---|---|---|
| 1 | `orderLinkId` determinístico → espera `110072` na duplicata | Não verificada — best effort |
| 2 | **Consulta obrigatória de estado antes de qualquer reenvio** | Sob nosso controle |

A camada 2 passa a ser **normativa, não uma otimização**. Nenhum reenvio acontece sem antes consultar `/v5/order/realtime` e `/v5/execution/list` pelo `orderLinkId`. Se a consulta for inconclusiva → `RECONCILIATION_REQUIRED`, nunca reenvio.

**Spike S-2 (obrigatório, antes do Sprint 6)** mede empiricamente em demo: envia ordem, cancela, reenvia o mesmo `orderLinkId` após 1min / 1h / 24h. Documenta o comportamento real. O resultado determina se a camada 1 tem algum valor ou se é puramente decorativa.

### 5.3 Ordem pode reportar `Filled` duas vezes

Armadilha documentada verbatim: *"You may receive two orderStatus=Filled messages when the cancel request is accepted but the order is executed at the same time."* — uma com `rejectReason=EC_NoError`, outra com `cancelType=CancelByUser`.

**O handler de fill precisa ser idempotente por `orderId`.** Um contador ingênuo de quantidade preenchida dobraria a posição contabilizada — e dispararia o circuit breaker de "quantidade preenchida maior que a esperada" sem que nada de errado tenha ocorrido.

Correlato: `orderStatus == "Cancelled"` em derivativos **pode ter quantidade executada**. Não tratar cancelamento como "nada aconteceu".

### 5.4 `orderStatus` — lista completa

Abertos: `New`, `PartiallyFilled`, `Untriggered`
Fechados: `Filled`, `Cancelled`, `Rejected`, `Triggered`, `Deactivated`, `PartiallyFilledCanceled` (spot-only)

`Triggered` é um estado **instantâneo** entre `Untriggered` e `New` — a máquina de estados não pode depender de observá-lo.

### 5.5 Stream `execution` traz mais que fills

`execType` inclui: `Trade`, `AdlTrade`, `Funding`, `BustTrade`, `Delivery`, `Settle`, `BlockTrade`, `MovePosition`, `FutureSpread`, `UNKNOWN`.

**`Funding` e `Settle` chegam pelo mesmo stream e não são execuções de ordem.** Filtrar por `execType == "Trade"` no caminho de confirmação de fill. `Funding` vai para o cálculo de custo da posição; `AdlTrade` e `BustTrade` são eventos de liquidação que devem disparar halt imediato.

Deduplicação por `execId`.

### 5.6 TP/SL — `tpslMode` e o risco de ordem órfã

| | **Full** | **Partial** |
|---|---|---|
| Escopo | Posição inteira, qualquer tamanho | Quantidade fixa (`tpSize`/`slSize`) |
| Order type | **Somente `Market`** | `Market` ou `Limit` |
| Semântica da API | **Modifica** ordem existente | **Apenas adiciona** nova ordem |
| Fill parcial | Cobre o tamanho real automaticamente | Descasamento — comportamento **[NV]** |

**Decisão: usar `tpslMode = "Full"` na v0.**

Racional — resolve diretamente o requisito mais crítico da spec original ("se a ordem for parcialmente preenchida, o TP/SL deve refletir somente a quantidade efetivamente executada"). Em modo Full, o TP/SL está atrelado à posição inteira seja qual for o tamanho corrente; um fill parcial fica coberto automaticamente, sem quantidade órfã.

O modo Partial parece mais controlável, mas tem duas armadilhas documentadas:
1. Modificar unilateralmente só o TP ou só o SL de um par existente **quebra o vínculo entre eles**. Depois disso, cancelar por ID cancela apenas um lado, deixando o outro ativo — ordem órfã real.
2. Se o fill parcial deixar a posição menor que o `tpSize` configurado, a doc **não descreve** a reconciliação. **[NV]**

Custo aceito: Full só suporta TP/SL a mercado. Para a política v0 (1 posição, RR ≥ 2, alvos estruturais) isso é adequado. Realizações parciais em múltiplos alvos exigiriam Partial — fica para depois da v0, com spike próprio.

⚠️ `/v5/position/list` retorna um campo `tpslMode` marcado **"Deprecated, always 'Full'"**. **Não usar esse campo para detectar o modo real.** O modo é mantido no nosso estado local e reconciliado via comportamento observado.

Endpoint: `POST /v5/position/trading-stop`, obrigatórios `category`, `symbol`, `tpslMode`, `positionIdx`. `takeProfit`/`stopLoss`/`trailingStop` iguais a `0` **cancelam** a respectiva ordem.

⚠️ Casing inconsistente na API: `tpslMode` em `/v5/order/create` e `/v5/position/trading-stop`, mas `tpSlMode` no endpoint de troca de modo.

### 5.7 One-way mode

- `positionIdx = 0` para one-way. (`1` = Buy hedge, `2` = Sell hedge.)
- `POST /v5/position/switch-mode` usa `mode = 0` (one-way) ou **`3`** (hedge) — **não** 0 e 1. Não confundir com os valores de `positionIdx`.
- `POST /v5/position/set-leverage`: em one-way, `buyLeverage` **deve ser igual a** `sellLeverage`.

### 5.8 Consultas exigem filtro

- `/v5/order/realtime` (ordens abertas): para linear, **um de `symbol`, `baseCoin` ou `settleCoin` é obrigatório**. Não dá para listar tudo sem filtro. O cache de 500 registros fechados é **limpo em restart do serviço da Bybit** — histórico definitivo só via `/v5/order/history`.
- `/v5/position/list`: para linear, `symbol` ou `settleCoin` obrigatório.

Como operamos um único símbolo, isso é trivial de satisfazer — mas a reconciliação de "posições inesperadas em outros símbolos" precisa usar `settleCoin=USDT` para varrer tudo.

Fontes: [Create Order](https://bybit-exchange.github.io/docs/v5/order/create-order) · [Trading Stop](https://bybit-exchange.github.io/docs/v5/position/trading-stop) · [Order WS](https://bybit-exchange.github.io/docs/v5/websocket/private/order) · [Execution WS](https://bybit-exchange.github.io/docs/v5/websocket/private/execution) · [Enums](https://bybit-exchange.github.io/docs/v5/enum) · [Error Codes](https://bybit-exchange.github.io/docs/v5/error)

---

## 6. 🔴 Patrimônio: o WS de wallet não basta

Duas notas críticas da doc, ambas verbatim:

> *"There is no snapshot event given at the time when the subscription is successful"*
> *"The unrealised PnL change does not trigger an event"*

**Consequência direta no Risk Engine:** os limites de perda diária e semanal dependem do patrimônio corrente. Se o `equity` só atualiza quando chega um evento de wallet, e o PnL não realizado **não gera evento**, o motor de risco opera com um patrimônio defasado enquanto uma posição está aberta e se movendo contra nós.

Este é exatamente o cenário em que o controle mais importa.

**Decisão — política de frescor de patrimônio:**

| Situação | Fonte | Frequência |
|---|---|---|
| Boot | REST `/v5/account/wallet-balance?accountType=UNIFIED` | 1× obrigatório |
| Sem posição aberta | WS `wallet` + REST | REST a cada 60s |
| **Com posição aberta** | REST | **a cada 5s** |
| Antes de qualquer decisão de risco | REST | **obrigatório, síncrono** |

A última linha é a que importa: **nenhuma intenção é avaliada com patrimônio de cache.** O custo é uma chamada REST por ciclo de decisão — irrelevante frente ao limite de 600 req/5s.

Campos: `totalEquity`, `totalAvailableBalance`, `totalWalletBalance`, `totalMarginBalance`, `totalPerpUPL`.
⚠️ `availableToWithdraw` está **deprecado desde 09/01/2025** — usar `totalAvailableBalance`.

Fonte: [Wallet WS](https://bybit-exchange.github.io/docs/v5/websocket/private/wallet) · [Wallet Balance](https://bybit-exchange.github.io/docs/v5/account/wallet-balance)

---

## 7. 🔴 Rate limits — o limite de IP é o perigoso

| Endpoint | Limite |
|---|---|
| Create Order | 20/s |
| Cancel Order | 20/s |
| Amend Order | 10/s |
| Position List | 50/s |

**Limite de IP: 600 requisições por 5 segundos. Estourar gera ban temporário de no mínimo 10 minutos.**

Este é o risco operacional mais subestimado da integração. É **por IP, não por chave**, e independe dos limites por endpoint. Um bug de retry-loop pode banir o IP por 10 minutos — e um ban de 10 minutos com posição aberta e sem capacidade de enviar ordem de saída é um cenário de perda descontrolada.

**Decisão: um `RateLimitGovernor` centralizado é componente obrigatório, não opcional.**

Requisitos:
- **Todas** as chamadas REST passam por ele. Sem exceção — teste arquitetural garante que ninguém chama `httpx` diretamente.
- Token bucket duplo: por endpoint **e** global de IP, com o global configurado a **300/5s** (50% de folga sobre o limite real).
- Reserva de emergência: 20% do orçamento global reservado para operações de saída (`cancel`, `close_position`, `trading-stop`). Chamadas de leitura são recusadas antes que uma ordem de saída seja bloqueada.
- Lê os headers de resposta `X-Bapi-Limit`, `X-Bapi-Limit-Status`, `X-Bapi-Limit-Reset-Timestamp` e ajusta o bucket ao valor real reportado.
- `retCode 10006` / HTTP 403 / `retCode 10018` → backoff exponencial + circuit breaker.
- Métrica de utilização do orçamento, com alerta em 70%.

Sprint 6 ganha os testes: consumo do orçamento sob rajada, preservação da reserva de emergência, recusa de leitura antes de saída.

Fonte: [Rate Limit](https://bybit-exchange.github.io/docs/v5/rate-limit)

---

## 8. Códigos de erro — tabela de decisão

| retCode | Significado | Ação |
|---|---|---|
| `0` | Sucesso | — |
| `10002` | Timestamp fora da janela | Ressincronizar relógio; **halt** se persistir |
| `10006` | Rate limit por endpoint | Backoff exponencial |
| `10018` | Rate limit por IP | **Halt** + backoff longo |
| `110072` | `orderLinkId` duplicado | **Não é erro** — reconciliar; ordem provavelmente existe |
| `110030` | `orderId` duplicado | Reconciliar |
| `110004` / `110007` | Saldo insuficiente | Rejeitar intenção; auditar divergência de sizing |
| `110017` | Qty truncaria para zero | Bug de sizing — **halt** |
| `170136` | Qty abaixo do mínimo | Bug de sizing — rejeitar e auditar |
| `170134` / `170137` | Casas decimais de preço/qty inválidas | Bug de arredondamento — **halt** |
| `110108` | Preço fora da faixa de tick | Bug de arredondamento — **halt** |
| `110074` | Contrato não está ativo | **Halt** (símbolo suspenso) |

**Princípio:** erros de saldo são condições de mercado (rejeitar); erros de precisão e quantidade são **bugs nossos** e disparam halt. Um `170134` significa que o arredondamento por `tickSize` falhou — a última coisa que se quer é continuar enviando ordens.

Fonte: [Error Codes](https://bybit-exchange.github.io/docs/v5/error)

---

## 9. Spikes obrigatórios

Cada **[NV]** vira um experimento com resultado documentado. Todos rodam contra **Demo Trading**, antes do Sprint 6. Sem exceção — o Sprint 6 não começa com spike pendente.

| ID | Pergunta | Método | Bloqueia |
|---|---|---|---|
| **S-1** | Qual o `recv_window` máximo aceito? | Bisseção: 5s, 10s, 30s, 60s, 120s até `10002` | Config do cliente REST |
| **S-2** | 🔴 Por quanto tempo `orderLinkId` é deduplicado? Duplicata é sempre rejeitada? | Reenviar mesmo ID após 1min / 1h / 24h; e reenviar concorrentemente | **D10 / Sprint 6** |
| **S-3** | Gap de `u` no orderbook: com que frequência ocorre e sempre indica divergência real? | Rodar 48h em depth 50, comparar contra snapshot REST a cada gap | Sprint 2 |
| **S-4** | Timeout de autenticação do WS privado | Abrir conexão, atrasar auth progressivamente | Cliente WS privado |
| **S-5** | Em `tpslMode=Partial`, o que acontece quando o fill parcial deixa a posição < `tpSize`? | Ordem limit grande, fill parcial forçado | Confirma decisão por Full |
| **S-6** | Streams privados entregam duplicatas ou fora de ordem? | 200 ordens, log de todos os eventos, análise de `execId`/`seq` | Design do deduplicador |
| **S-7** | Latência real: envio → aceite → confirmação de fill via WS | Percentis p50/p95/p99 sobre 200 ordens | Modelo de latência do backtest |
| **S-8** | Custo real: taxas maker/taker efetivas e funding | Ordens reais, comparar `execFee` com a tabela | Modelo de custos do sizing |

**S-2 é o spike mais importante do projeto.** O resultado dele determina se a idempotência de ordens tem uma ou duas camadas reais de defesa.

Resultados vão para `docs/spikes/S-N-resultado.md`, com dados brutos anexados. Um spike sem dado bruto não conta como concluído.

---

## 10. Resumo — o que mudou no plano original

| # | Premissa original | Realidade verificada | Impacto |
|---|---|---|---|
| 1 | `orderLinkId` garante idempotência (D10) | **[NV]** — sem garantia documentada | 🔴 D10 revisada: consulta de estado vira normativa |
| 2 | Gap de sequência → ressubscrever | Regra não documentada; depth 1 repete `u` de propósito | Vira heurística + validação REST |
| 3 | Demo isola o ambiente inteiro | Demo usa **market data de mainnet** | Config em 2 eixos |
| 4 | `tickers` é snapshot | Linear é snapshot **+ delta** incremental | Merge preservando campos ausentes |
| 5 | Equity vem do WS | Sem snapshot inicial; PnL não gera evento | REST síncrono antes de cada decisão |
| 6 | Rate limit é por endpoint | **600/5s por IP → ban de 10 min** | `RateLimitGovernor` obrigatório + reserva de emergência |
| 7 | Fill confirmado uma vez | `Filled` pode chegar **duas vezes** | Handler idempotente por `orderId` |
| 8 | Depth 500 disponível | Não existe: `1, 50, 200, 1000` | Usar depth 50 |
| 9 | `position.avgPrice` | É **`entryPrice`** | Correção de parsing |
| 10 | `tpslMode` escolhido livremente | Full só aceita Market; Partial quebra vínculo TP↔SL | Decisão: **Full** na v0 |
| 11 | Stream `execution` = fills | Inclui `Funding`, `Settle`, `AdlTrade` | Filtrar `execType == "Trade"` |
| 12 | Kline REST em ordem cronológica | **Ordem reversa** | Inverter antes dos indicadores |
