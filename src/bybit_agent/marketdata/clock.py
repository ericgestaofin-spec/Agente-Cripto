"""Sincronização de relógio com o servidor Bybit.

Mede o offset entre o relógio local e o do servidor (GET /v5/market/time,
público) e fornece um "agora" corrigido. Sinaliza quando o desvio é grande
demais para operar — o circuit breaker de relógio da spec.

Por que importa: a janela `recv_window` da Bybit é assimétrica (no máximo
~1s adiantado), e o `data_age` do snapshot depende de comparar o timestamp
do servidor com o "agora". Misturar dois relógios sem medir o offset foi a
causa do `data_age` negativo que o próprio Claude flagrou ao vivo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


def compute_offset_ms(*, t_send: int, server_ms: int, t_recv: int) -> int:
    """Offset servidor − relógio local, descontando a latência da rede.

    Usa o ponto-médio de ida-e-volta (NTP-like): o melhor palpite para o
    instante local em que o servidor gerou a resposta é (t_send + t_recv)/2.
    offset = server − esse ponto-médio.
    """
    local_midpoint = (t_send + t_recv) // 2
    return server_ms - local_midpoint


@dataclass(frozen=True, slots=True)
class ClockSkew:
    """Offset medido e limite de saúde. Imutável."""

    offset_ms: int
    max_offset_ms: int

    def corrected_now_ms(self, *, local_now_ms: int) -> int:
        """O 'agora' na base de tempo do servidor."""
        return local_now_ms + self.offset_ms

    def is_healthy(self) -> bool:
        """True se o desvio está dentro do limite tolerável."""
        return abs(self.offset_ms) <= self.max_offset_ms


async def measure_skew(
    client: httpx.AsyncClient, *, max_offset_ms: int = 500
) -> ClockSkew:  # pragma: no cover - I/O, exercitado por smoke test ao vivo
    """Mede o skew consultando /v5/market/time.

    Precisa de uma função de tempo local em ms; usamos time.time() aqui.
    O cálculo de offset em si é puro e testado separadamente.
    """
    import time

    t_send = int(time.time() * 1000)
    resp = await client.get("/v5/market/time")
    resp.raise_for_status()
    t_recv = int(time.time() * 1000)
    payload: dict[str, Any] = resp.json()
    if payload.get("retCode") != 0:
        raise ValueError(f"Bybit /v5/market/time retCode {payload.get('retCode')}")
    server_ms = int(payload["result"]["timeNano"]) // 1_000_000
    offset = compute_offset_ms(t_send=t_send, server_ms=server_ms, t_recv=t_recv)
    return ClockSkew(offset_ms=offset, max_offset_ms=max_offset_ms)
