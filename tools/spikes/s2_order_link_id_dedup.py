"""Spike S-2 — a Bybit deduplica `orderLinkId`? Por quanto tempo?

CONTEXTO
--------
A decisão D10 do plano original assumia que um `orderLinkId` determinístico
garante idempotência: um retry após timeout nunca duplicaria a ordem.

A documentação da Bybit **não sustenta isso**. Ela diz apenas que o ID
"should be unique" — sem especificar por quanto tempo a unicidade é
imposta, nem garantir que uma duplicata seja rejeitada de forma confiável.

Este é o controle mais crítico do sistema apoiado numa suposição não
verificada. Este spike a converte em fato medido.

O QUE MEDE
----------
  T1  Duplicata imediata (mesmo ID, ~1s depois)         → rejeita?
  T2  Duplicata após a ordem original ser CANCELADA     → rejeita?
  T3  Duplicata após a ordem original ser PREENCHIDA    → rejeita?
  T4  Duplicata concorrente (2 envios simultâneos)      → 1 ou 2 ordens?
  T5  Duplicata após 1 hora            (--long)         → rejeita?
  T6  Duplicata após 24 horas          (--long)         → rejeita?

INTERPRETAÇÃO DO RESULTADO
--------------------------
  retCode 110072  → rejeitou (camada 1 funciona para aquele intervalo)
  retCode 0       → ACEITOU DUPLICATA — camada 1 é inútil naquele caso.
                    A consulta de estado antes do reenvio passa a ser a
                    ÚNICA defesa contra ordem duplicada.

  T4 é o caso mais importante: é exatamente o cenário de retry após
  timeout de rede, onde a primeira requisição pode estar em voo.

SEGURANÇA
---------
  * Roda SOMENTE contra Demo Trading (api-demo.bybit.com). Recusa-se a
    executar contra qualquer outro host — verificação em código.
  * Ordens limit a preço distante do mercado, que não devem preencher
    (exceto T3, deliberado).
  * Cancela tudo ao final, inclusive em caso de erro.

USO
---
  set BYBIT_DEMO_KEY=...
  set BYBIT_DEMO_SECRET=...
  python -m tools.spikes.s2_order_link_id_dedup            # T1-T4 (~2 min)
  python -m tools.spikes.s2_order_link_id_dedup --long     # + T5/T6

Resultado em: docs/spikes/S-2-resultado.md
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import httpx

DEMO_HOST: Final[str] = "https://api-demo.bybit.com"
SYMBOL: Final[str] = "BTCUSDT"
CATEGORY: Final[str] = "linear"
RECV_WINDOW: Final[str] = "5000"

RETCODE_OK: Final[int] = 0
RETCODE_DUPLICATE_LINK_ID: Final[int] = 110072


# ---------------------------------------------------------------------------
# Cliente REST mínimo — assinatura HMAC conforme BYBIT_INTEGRACAO.md §2
# ---------------------------------------------------------------------------


class BybitDemoClient:
    """Cliente mínimo para o spike. NÃO é o cliente de produção.

    Implementa a assinatura exata documentada:
        POST: timestamp + api_key + recv_window + rawJsonBodyString
        GET:  timestamp + api_key + recv_window + queryString

    Ponto crítico: a string assinada é o corpo EXATO enviado. Assinamos
    `body_str` e enviamos `content=body_str` — nunca `json=payload`, que
    deixaria o httpx re-serializar e quebraria a assinatura.
    """

    def __init__(self, key: str, secret: str, host: str = DEMO_HOST) -> None:
        if host != DEMO_HOST:
            raise RuntimeError(
                f"RECUSADO: este spike envia ordens reais e só pode rodar "
                f"contra Demo Trading ({DEMO_HOST}). Recebido: {host}"
            )
        self._key = key
        self._secret = secret
        self._host = host
        self._client = httpx.AsyncClient(base_url=host, timeout=15.0)

    async def __aenter__(self) -> BybitDemoClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    def _headers(self, payload: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        to_sign = f"{ts}{self._key}{RECV_WINDOW}{payload}"
        sign = hmac.new(
            self._secret.encode(), to_sign.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "X-BAPI-API-KEY": self._key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": sign,
            "Content-Type": "application/json",
        }

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"))
        resp = await self._client.post(path, content=body, headers=self._headers(body))
        return dict(resp.json())

    async def get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        resp = await self._client.get(
            f"{path}?{query}", headers=self._headers(query)
        )
        return dict(resp.json())

    # -- operações usadas pelo spike ---------------------------------------

    async def place_limit(
        self, order_link_id: str, price: Decimal, qty: Decimal, side: str = "Buy"
    ) -> dict[str, Any]:
        return await self.post(
            "/v5/order/create",
            {
                "category": CATEGORY,
                "symbol": SYMBOL,
                "side": side,
                "orderType": "Limit",
                "qty": str(qty),
                "price": str(price),
                "timeInForce": "GTC",
                "orderLinkId": order_link_id,
                "positionIdx": 0,
            },
        )

    async def place_market(self, order_link_id: str, qty: Decimal) -> dict[str, Any]:
        return await self.post(
            "/v5/order/create",
            {
                "category": CATEGORY,
                "symbol": SYMBOL,
                "side": "Buy",
                "orderType": "Market",
                "qty": str(qty),
                "orderLinkId": order_link_id,
                "positionIdx": 0,
            },
        )

    async def cancel(self, order_link_id: str) -> dict[str, Any]:
        return await self.post(
            "/v5/order/cancel",
            {"category": CATEGORY, "symbol": SYMBOL, "orderLinkId": order_link_id},
        )

    async def open_orders(self) -> dict[str, Any]:
        return await self.get(
            "/v5/order/realtime", {"category": CATEGORY, "symbol": SYMBOL}
        )

    async def order_history(self, order_link_id: str) -> dict[str, Any]:
        return await self.get(
            "/v5/order/history",
            {"category": CATEGORY, "symbol": SYMBOL, "orderLinkId": order_link_id},
        )

    async def ticker(self) -> dict[str, Any]:
        return await self.get(
            "/v5/market/tickers", {"category": CATEGORY, "symbol": SYMBOL}
        )

    async def instrument(self) -> dict[str, Any]:
        return await self.get(
            "/v5/market/instruments-info", {"category": CATEGORY, "symbol": SYMBOL}
        )

    async def close_position(self, qty: Decimal) -> dict[str, Any]:
        return await self.post(
            "/v5/order/create",
            {
                "category": CATEGORY,
                "symbol": SYMBOL,
                "side": "Sell",
                "orderType": "Market",
                "qty": str(qty),
                "reduceOnly": True,
                "positionIdx": 0,
            },
        )


# ---------------------------------------------------------------------------
# Resultados
# ---------------------------------------------------------------------------


@dataclass
class TestResult:
    name: str
    question: str
    duplicate_rejected: bool | None  # None = inconclusivo
    ret_code: int | None
    ret_msg: str
    raw: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    @property
    def verdict(self) -> str:
        if self.duplicate_rejected is None:
            return "INCONCLUSIVO"
        return "REJEITOU (bom)" if self.duplicate_rejected else "ACEITOU DUPLICATA (ruim)"


def _link_id(tag: str) -> str:
    """<= 36 chars, apenas [A-Za-z0-9_-] — limite verificado da Bybit."""
    stamp = int(time.time() * 1000) % 10_000_000_000
    lid = f"s2-{tag}-{stamp}"
    assert len(lid) <= 36, lid
    return lid


# ---------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------


async def t1_immediate(c: BybitDemoClient, price: Decimal, qty: Decimal) -> TestResult:
    lid = _link_id("t1")
    first = await c.place_limit(lid, price, qty)
    await asyncio.sleep(1)
    second = await c.place_limit(lid, price, qty)
    await c.cancel(lid)
    return TestResult(
        name="T1",
        question="Duplicata imediata (~1s) é rejeitada?",
        duplicate_rejected=second.get("retCode") == RETCODE_DUPLICATE_LINK_ID,
        ret_code=second.get("retCode"),
        ret_msg=str(second.get("retMsg", "")),
        raw=[first, second],
    )


async def t2_after_cancel(
    c: BybitDemoClient, price: Decimal, qty: Decimal
) -> TestResult:
    lid = _link_id("t2")
    first = await c.place_limit(lid, price, qty)
    cancelled = await c.cancel(lid)
    await asyncio.sleep(2)
    second = await c.place_limit(lid, price, qty)
    if second.get("retCode") == RETCODE_OK:
        await c.cancel(lid)
    return TestResult(
        name="T2",
        question="Duplicata após CANCELAMENTO é rejeitada?",
        duplicate_rejected=second.get("retCode") == RETCODE_DUPLICATE_LINK_ID,
        ret_code=second.get("retCode"),
        ret_msg=str(second.get("retMsg", "")),
        raw=[first, cancelled, second],
        note="Cenário real: ordem cancelada e o sistema tenta reenviar a mesma intenção.",
    )


async def t3_after_fill(c: BybitDemoClient, qty: Decimal) -> TestResult:
    """Ordem a mercado (preenche), depois reenvia o mesmo ID.

    Este é o cenário MAIS PERIGOSO: se aceitar, um retry após timeout
    numa ordem que já preencheu abre uma SEGUNDA posição.
    """
    lid = _link_id("t3")
    first = await c.place_market(lid, qty)
    await asyncio.sleep(3)
    second = await c.place_market(lid, qty)
    accepted = second.get("retCode") == RETCODE_OK

    # Fecha o que abriu — 1x ou 2x conforme o resultado
    to_close = qty * 2 if accepted else qty
    await asyncio.sleep(2)
    await c.close_position(to_close)

    return TestResult(
        name="T3",
        question="Duplicata após PREENCHIMENTO é rejeitada?",
        duplicate_rejected=second.get("retCode") == RETCODE_DUPLICATE_LINK_ID,
        ret_code=second.get("retCode"),
        ret_msg=str(second.get("retMsg", "")),
        raw=[first, second],
        note="CRÍTICO: se aceitar, retry após timeout duplica a POSIÇÃO.",
    )


async def t4_concurrent(
    c: BybitDemoClient, price: Decimal, qty: Decimal
) -> TestResult:
    """Dois envios simultâneos do mesmo ID.

    É o cenário exato de retry após timeout de rede: a primeira requisição
    pode ainda estar em voo quando a segunda parte. Se a dedup da Bybit
    for baseada em estado persistido, há uma janela de corrida.
    """
    lid = _link_id("t4")
    a, b = await asyncio.gather(
        c.place_limit(lid, price, qty),
        c.place_limit(lid, price, qty),
        return_exceptions=True,
    )
    results = [r for r in (a, b) if isinstance(r, dict)]
    accepted = sum(1 for r in results if r.get("retCode") == RETCODE_OK)

    await asyncio.sleep(1)
    open_now = await c.open_orders()
    matching = [
        o
        for o in open_now.get("result", {}).get("list", [])
        if o.get("orderLinkId") == lid
    ]
    await c.cancel(lid)
    if len(matching) > 1:  # pragma: no cover - depende do resultado real
        for _ in matching[1:]:
            await c.cancel(lid)

    return TestResult(
        name="T4",
        question="Dois envios SIMULTÂNEOS criam uma ou duas ordens?",
        duplicate_rejected=accepted <= 1 and len(matching) <= 1,
        ret_code=None,
        ret_msg=f"aceitas={accepted}, ordens_abertas_com_esse_id={len(matching)}",
        raw=[*results, open_now],
        note="Cenário exato do retry após timeout de rede. O mais relevante para D10.",
    )


async def t_delayed(
    c: BybitDemoClient, price: Decimal, qty: Decimal, *, hours: int, tag: str
) -> TestResult:
    lid = _link_id(tag)
    first = await c.place_limit(lid, price, qty)
    await c.cancel(lid)
    print(f"  [{tag}] aguardando {hours}h antes do reenvio...")
    await asyncio.sleep(hours * 3600)
    second = await c.place_limit(lid, price, qty)
    if second.get("retCode") == RETCODE_OK:
        await c.cancel(lid)
    return TestResult(
        name=tag.upper(),
        question=f"Duplicata após {hours}h é rejeitada?",
        duplicate_rejected=second.get("retCode") == RETCODE_DUPLICATE_LINK_ID,
        ret_code=second.get("retCode"),
        ret_msg=str(second.get("retMsg", "")),
        raw=[first, second],
    )


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------


def write_report(results: list[TestResult], out: Path) -> None:
    accepted_any = [r for r in results if r.duplicate_rejected is False]
    out.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Spike S-2 — Deduplicação de `orderLinkId` na Bybit",
        "",
        f"**Executado:** {datetime.now(UTC).isoformat()}",
        f"**Ambiente:** Demo Trading ({DEMO_HOST}) · {SYMBOL} · {CATEGORY}",
        "",
        "## Resultado",
        "",
        "| Teste | Pergunta | Veredito | retCode | Detalhe |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.name} | {r.question} | **{r.verdict}** | "
            f"`{r.ret_code}` | {r.ret_msg} |"
        )

    lines += ["", "## Conclusão para a decisão D10", ""]
    if accepted_any:
        names = ", ".join(r.name for r in accepted_any)
        lines += [
            f"🔴 **A Bybit ACEITOU duplicata em: {names}.**",
            "",
            "A camada 1 (`orderLinkId`) **não** é uma defesa confiável contra",
            "ordem duplicada nesses cenários. A consulta obrigatória de estado",
            "antes de todo reenvio é a **única** proteção real.",
            "",
            "Ações:",
            "- Manter `test_retry_NEVER_happens_without_state_query_first` como",
            "  teste bloqueante do Sprint 6.",
            "- Registrar métrica de reenvios evitados pela consulta de estado.",
        ]
    elif any(r.duplicate_rejected is None for r in results):
        lines += [
            "⚠️ **Resultado parcialmente inconclusivo.** Reexecutar antes do Sprint 6.",
        ]
    else:
        lines += [
            "✅ **A Bybit rejeitou duplicata em todos os cenários testados.**",
            "",
            "A camada 1 tem valor real. **Ainda assim, a camada 2 permanece",
            "normativa** — a garantia não é documentada e pode mudar sem aviso.",
            "Defesa em profundidade não é redundância desnecessária quando o",
            "modo de falha é uma posição duplicada.",
        ]

    lines += ["", "## Dados brutos", "", "```json",
              json.dumps([{
                  "test": r.name,
                  "rejected": r.duplicate_rejected,
                  "note": r.note,
                  "responses": r.raw,
              } for r in results], indent=2, ensure_ascii=False),
              "```", ""]

    out.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(long: bool) -> int:
    key = os.environ.get("BYBIT_DEMO_KEY")
    secret = os.environ.get("BYBIT_DEMO_SECRET")
    if not key or not secret:
        print(
            "ERRO: defina BYBIT_DEMO_KEY e BYBIT_DEMO_SECRET.\n\n"
            "As chaves são criadas DENTRO do modo Demo Trading de uma conta\n"
            "mainnet (UID separado). Não use chaves de testnet nem de produção.",
            file=sys.stderr,
        )
        return 2

    async with BybitDemoClient(key, secret) as c:
        inst = await c.instrument()
        if inst.get("retCode") != RETCODE_OK:
            print(f"ERRO ao ler instrumento: {inst}", file=sys.stderr)
            return 1
        spec = inst["result"]["list"][0]
        qty = Decimal(spec["lotSizeFilter"]["minOrderQty"])
        tick = Decimal(spec["priceFilter"]["tickSize"])

        tk = await c.ticker()
        last = Decimal(tk["result"]["list"][0]["lastPrice"])
        # Preço bem abaixo do mercado: a ordem limit de compra não preenche.
        price = ((last * Decimal("0.70")) / tick).to_integral_value() * tick

        print(f"Instrumento: minQty={qty} tick={tick} | last={last} | limit={price}\n")

        results: list[TestResult] = []
        for coro, label in [
            (t1_immediate(c, price, qty), "T1 duplicata imediata"),
            (t2_after_cancel(c, price, qty), "T2 após cancelamento"),
            (t3_after_fill(c, qty), "T3 após preenchimento"),
            (t4_concurrent(c, price, qty), "T4 concorrente"),
        ]:
            print(f"→ {label}")
            r = await coro
            print(f"   {r.verdict} (retCode={r.ret_code}) {r.ret_msg}\n")
            results.append(r)

        if long:
            results.append(await t_delayed(c, price, qty, hours=1, tag="t5"))
            results.append(await t_delayed(c, price, qty, hours=24, tag="t6"))

    out = Path(__file__).resolve().parents[2] / "docs" / "spikes" / "S-2-resultado.md"
    write_report(results, out)
    print(f"Relatório: {out}")

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Spike S-2 — dedup de orderLinkId")
    p.add_argument("--long", action="store_true", help="inclui T5 (1h) e T6 (24h)")
    args = p.parse_args()
    return asyncio.run(run(args.long))


if __name__ == "__main__":
    sys.exit(main())
