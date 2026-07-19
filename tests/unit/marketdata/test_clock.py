"""A1 — serviço de sincronização de relógio.

O bug que o Claude achou ao vivo (data_age_ms negativo) veio de misturar o
relógio local com o timestamp do servidor Bybit sem medir o offset entre
eles. Este serviço mede o skew via GET /v5/market/time (público) e fornece
um "agora" corrigido, além de sinalizar quando o desvio é grande demais
para operar (circuit breaker de relógio da spec).

Cálculo do offset por ponto-médio de ida-e-volta (NTP-like):
    offset = servidor - (t_envio + t_recebimento) / 2
Assim a latência da rede é descontada simetricamente.
"""

from __future__ import annotations

from bybit_agent.marketdata.clock import ClockSkew, compute_offset_ms


# --------------------------------------------------------------------------
# Cálculo do offset (puro, testável com timestamps injetados)
# --------------------------------------------------------------------------


def test_offset_zero_when_clocks_aligned_and_no_latency() -> None:
    """Sem latência e relógios iguais → offset 0."""
    assert compute_offset_ms(t_send=1000, server_ms=1000, t_recv=1000) == 0


def test_offset_uses_round_trip_midpoint() -> None:
    """Envio em 1000, recebimento em 1100 (RTT 100, meio em 1050).
    Servidor responde 1050 → relógios alinhados, offset 0."""
    assert compute_offset_ms(t_send=1000, server_ms=1050, t_recv=1100) == 0


def test_positive_offset_when_server_ahead() -> None:
    """Servidor 200ms à frente do ponto-médio local."""
    # meio local = (1000+1100)/2 = 1050; servidor = 1250 → offset +200
    assert compute_offset_ms(t_send=1000, server_ms=1250, t_recv=1100) == 200


def test_negative_offset_when_server_behind() -> None:
    # meio local = 1050; servidor = 900 → offset -150
    assert compute_offset_ms(t_send=1000, server_ms=900, t_recv=1100) == -150


# --------------------------------------------------------------------------
# ClockSkew — estado de saúde
# --------------------------------------------------------------------------


def test_corrected_now_applies_offset() -> None:
    """O 'agora' corrigido = relógio local + offset do servidor."""
    skew = ClockSkew(offset_ms=200, max_offset_ms=500)
    assert skew.corrected_now_ms(local_now_ms=1000) == 1200


def test_healthy_when_offset_within_threshold() -> None:
    assert ClockSkew(offset_ms=300, max_offset_ms=500).is_healthy()
    assert ClockSkew(offset_ms=-300, max_offset_ms=500).is_healthy()


def test_unhealthy_when_offset_exceeds_threshold() -> None:
    """⭐ Skew além do limite é o circuit breaker de relógio — não se opera
    com relógio dessincronizado (a janela de recv_window da Bybit é
    assimétrica: no máx 1s adiantado)."""
    assert not ClockSkew(offset_ms=800, max_offset_ms=500).is_healthy()
    assert not ClockSkew(offset_ms=-800, max_offset_ms=500).is_healthy()


def test_healthy_exactly_at_threshold() -> None:
    """Fronteira: offset == limite é aceito; só acima é unhealthy."""
    assert ClockSkew(offset_ms=500, max_offset_ms=500).is_healthy()
    assert ClockSkew(offset_ms=-500, max_offset_ms=500).is_healthy()


def test_clock_skew_is_immutable() -> None:
    import pytest

    skew = ClockSkew(offset_ms=100, max_offset_ms=500)
    with pytest.raises((AttributeError, TypeError)):
        skew.offset_ms = 999  # type: ignore[misc]
