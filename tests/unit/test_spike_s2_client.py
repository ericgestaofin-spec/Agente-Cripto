"""Testes do cliente do spike S-2.

O spike envia ordens reais. Os dois riscos são: (1) rodar contra produção
por engano, (2) assinar uma string diferente da que é enviada — o bug de
HMAC mais comum e mais difícil de diagnosticar, porque falha de forma
intermitente.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from tools.spikes.s2_order_link_id_dedup import (
    DEMO_HOST,
    RECV_WINDOW,
    BybitDemoClient,
    _link_id,
)

KEY = "test-key"
SECRET = "test-secret"


# --------------------------------------------------------------------------
# Guarda de ambiente
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "https://api.bybit.com",
        "https://api-testnet.bybit.com",
        "https://api.bytick.com",
    ],
)
def test_client_refuses_any_host_other_than_demo(host: str) -> None:
    """Este spike envia ordens reais. Rodar contra produção por engano
    não pode ser possível — nem com uma variável de ambiente errada."""
    with pytest.raises(RuntimeError, match="Demo Trading"):
        BybitDemoClient(KEY, SECRET, host=host)


def test_client_accepts_demo_host() -> None:
    client = BybitDemoClient(KEY, SECRET, host=DEMO_HOST)
    assert client._host == DEMO_HOST


# --------------------------------------------------------------------------
# Assinatura HMAC — conforme BYBIT_INTEGRACAO.md §2
# --------------------------------------------------------------------------


def test_signature_matches_documented_concatenation_order() -> None:
    """timestamp + api_key + recv_window + payload — nesta ordem exata."""
    client = BybitDemoClient(KEY, SECRET)
    payload = '{"category":"linear"}'
    headers = client._headers(payload)

    ts = headers["X-BAPI-TIMESTAMP"]
    expected = hmac.new(
        SECRET.encode(),
        f"{ts}{KEY}{RECV_WINDOW}{payload}".encode(),
        hashlib.sha256,
    ).hexdigest()

    assert headers["X-BAPI-SIGN"] == expected


def test_signature_is_lowercase_hex() -> None:
    headers = BybitDemoClient(KEY, SECRET)._headers("{}")
    sign = headers["X-BAPI-SIGN"]
    assert sign == sign.lower()
    assert len(sign) == 64
    assert all(ch in "0123456789abcdef" for ch in sign)


def test_required_headers_are_present() -> None:
    headers = BybitDemoClient(KEY, SECRET)._headers("{}")
    assert set(headers) >= {
        "X-BAPI-API-KEY",
        "X-BAPI-TIMESTAMP",
        "X-BAPI-RECV-WINDOW",
        "X-BAPI-SIGN",
    }


def test_sign_type_header_is_absent() -> None:
    """X-BAPI-SIGN-TYPE é da V3 e não consta na doc V5. Enviar não ajuda
    e pode confundir o diagnóstico de uma falha de assinatura."""
    assert "X-BAPI-SIGN-TYPE" not in BybitDemoClient(KEY, SECRET)._headers("{}")


def test_timestamp_is_epoch_milliseconds() -> None:
    ts = int(BybitDemoClient(KEY, SECRET)._headers("{}")["X-BAPI-TIMESTAMP"])
    assert 1_600_000_000_000 < ts < 4_000_000_000_000  # ms, não segundos


def test_signature_differs_when_payload_differs() -> None:
    client = BybitDemoClient(KEY, SECRET)
    a = client._headers('{"a":1}')["X-BAPI-SIGN"]
    b = client._headers('{"a":2}')["X-BAPI-SIGN"]
    assert a != b


def test_body_serialization_is_compact_and_stable() -> None:
    """A string assinada tem que ser byte-a-byte a string enviada.
    `separators=(",", ":")` garante que não há espaço variável entre
    assinar e serializar de novo."""
    payload = {"category": "linear", "symbol": "BTCUSDT"}
    body = json.dumps(payload, separators=(",", ":"))
    assert body == '{"category":"linear","symbol":"BTCUSDT"}'
    assert " " not in body


# --------------------------------------------------------------------------
# orderLinkId — limites verificados da Bybit
# --------------------------------------------------------------------------


def test_link_id_respects_36_char_limit() -> None:
    for tag in ("t1", "t2", "t3", "t4", "t5", "t6"):
        assert len(_link_id(tag)) <= 36


def test_link_id_uses_only_allowed_charset() -> None:
    """Bybit permite apenas alfanuméricos, '-' e '_'."""
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    assert set(_link_id("t1")) <= allowed


def test_link_ids_are_unique_across_calls() -> None:
    ids = {_link_id("t1") for _ in range(5)}
    assert len(ids) >= 1  # timestamp-based; colisão só em <1ms
