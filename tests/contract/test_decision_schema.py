"""Sprint 1 — schema de decisão do agente.

Este schema é passado à API da Anthropic em `output_config.format`, o que
faz a conformidade ser garantida na decodificação, não pedida no prompt.

Duas classes de teste:
  1. O schema é válido E usa apenas construções que a API suporta.
  2. Payloads válidos/inválidos parseiam ou falham como esperado.

O teste mais importante é `test_schema_uses_no_unsupported_constraints`:
a API **ignora silenciosamente** constraints como `minimum` e `maxLength`.
Um schema que as declara parece correto em revisão de código e não faz
nada em produção — a pior combinação possível.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
SCHEMA_PATH = CONTRACTS / "decision_v1.json"

# Constraints que a API da Anthropic NÃO suporta em structured outputs.
# Declará-las cria falsa sensação de validação.
UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema)


def _walk(node: Any) -> list[tuple[str, Any]]:
    """Percorre o schema retornando (chave, valor) de todo objeto."""
    found: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            found.append((k, v))
            found.extend(_walk(v))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item))
    return found


def _objects(node: Any) -> list[dict[str, Any]]:
    """Todo subschema que declara type: object."""
    out: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            out.append(node)
        for v in node.values():
            out.extend(_objects(v))
    elif isinstance(node, list):
        for item in node:
            out.extend(_objects(item))
    return out


# --------------------------------------------------------------------------
# Validade estrutural
# --------------------------------------------------------------------------


def test_schema_file_exists() -> None:
    assert SCHEMA_PATH.exists(), f"contrato ausente: {SCHEMA_PATH}"


def test_schema_is_valid_json_schema(schema: dict[str, Any]) -> None:
    Draft202012Validator.check_schema(schema)


def test_schema_uses_no_unsupported_constraints(schema: dict[str, Any]) -> None:
    """⭐ A API ignora estas constraints. Declará-las é pior que omiti-las:
    parece validação e não é. Faixas numéricas vão para o Risk Engine."""
    offenders = [
        (k, v) for k, v in _walk(schema) if k in UNSUPPORTED_KEYWORDS
    ]
    assert offenders == [], (
        "constraints não suportadas pela API encontradas no schema — "
        "elas serão silenciosamente ignoradas. Valide em código:\n"
        + "\n".join(f"  {k}: {v!r}" for k, v in offenders)
    )


def test_every_object_forbids_additional_properties(schema: dict[str, Any]) -> None:
    """Exigido pela API. Também impede o modelo de inventar campos."""
    for obj in _objects(schema):
        assert obj.get("additionalProperties") is False, (
            f"objeto sem additionalProperties=false: {sorted(obj.get('properties', {}))}"
        )


def test_schema_is_not_recursive(schema: dict[str, Any]) -> None:
    """Schemas recursivos não são suportados."""
    refs = [v for k, v in _walk(schema) if k == "$ref"]
    for ref in refs:
        assert not str(ref).startswith("#/$defs/decision"), f"$ref recursivo: {ref}"


def test_all_declared_properties_are_required(schema: dict[str, Any]) -> None:
    """Campo opcional vira ambiguidade: ausente significa 'não sei' ou
    'não se aplica'? Tudo é obrigatório; ausência se expressa com null."""
    for obj in _objects(schema):
        props = set(obj.get("properties", {}))
        required = set(obj.get("required", []))
        assert props == required, (
            f"propriedades não obrigatórias: {sorted(props - required)}"
        )


def test_prices_are_declared_as_strings(schema: dict[str, Any]) -> None:
    """Preço como number vira float no parse JSON. Todo valor monetário
    trafega como string decimal."""
    price_fields = {
        "price",
        "price_min",
        "price_max",
        "stop_loss",
        "invalidation_price",
        "close_fraction",
        "estimated_rr_gross",
        "estimated_rr_net",
        "maximum_slippage_bps",
    }
    for obj in _objects(schema):
        for name, spec in obj.get("properties", {}).items():
            if name in price_fields:
                types = spec.get("type")
                types = [types] if isinstance(types, str) else list(types or [])
                assert "number" not in types, f"{name} declarado como number"
                assert "string" in types, f"{name} deve ser string decimal"


def test_symbol_is_locked_to_allowlist(schema: dict[str, Any]) -> None:
    """v0 opera só BTCUSDT. Travar no enum impede o modelo de propor
    outro símbolo no nível da decodificação."""
    symbol = schema["properties"]["symbol"]
    assert symbol.get("enum") == ["BTCUSDT"]


def test_action_enum_matches_specification(schema: dict[str, Any]) -> None:
    assert set(schema["properties"]["action"]["enum"]) == {
        "NO_TRADE",
        "WATCH",
        "OPEN_LONG",
        "OPEN_SHORT",
        "ADJUST_STOP",
        "TAKE_PARTIAL",
        "CLOSE_POSITION",
        "HALT_TRADING",
    }


def test_schema_has_no_qty_or_leverage_field(schema: dict[str, Any]) -> None:
    """⭐ O modelo produz INTENÇÃO, nunca tamanho. Se o schema aceitasse
    qty ou leverage, a separação entre analista e motor de risco estaria
    comprometida no contrato — antes mesmo do código."""
    forbidden = {"qty", "quantity", "size", "leverage", "position_size", "notional"}
    declared = {k for k, _ in _walk(schema) if k == "properties"}
    _ = declared
    names = {
        name
        for obj in _objects(schema)
        for name in obj.get("properties", {})
    }
    assert names & forbidden == set(), (
        f"o schema expõe dimensionamento ao modelo: {sorted(names & forbidden)}"
    )


# --------------------------------------------------------------------------
# Validação de payloads
# --------------------------------------------------------------------------


def _valid_no_trade() -> dict[str, Any]:
    return {
        "decision_id": "5f1e4d3c-2b1a-4098-8765-43210fedcba9",
        "timestamp": "2026-07-19T12:00:00Z",
        "symbol": "BTCUSDT",
        "action": "NO_TRADE",
        "data_quality": {"status": "VALID", "snapshot_age_ms": 184, "issues": []},
        "market_regime": "RANGE",
        "setup": {
            "name": None,
            "timeframe": None,
            "direction": "NONE",
            "quality_score": 0,
            "evidence_for": [],
            "evidence_against": ["spread acima do normal"],
        },
        "entry": {
            "type": "NONE",
            "price": None,
            "price_min": None,
            "price_max": None,
            "confirmation": None,
            "expires_at": None,
        },
        "risk_plan": {
            "invalidation_price": None,
            "stop_loss": None,
            "take_profit_levels": [],
            "estimated_rr_gross": None,
            "estimated_rr_net": None,
            "maximum_slippage_bps": None,
        },
        "cancellation_conditions": [],
        "reason_codes": ["NO_CLEAR_SETUP"],
        "summary": "Mercado em range sem assimetria clara.",
    }


def test_valid_no_trade_payload_passes(validator: Draft202012Validator) -> None:
    validator.validate(_valid_no_trade())


def test_valid_open_long_payload_passes(validator: Draft202012Validator) -> None:
    payload = _valid_no_trade()
    payload["action"] = "OPEN_LONG"
    payload["setup"] = {
        "name": "TREND_PULLBACK",
        "timeframe": "15m",
        "direction": "LONG",
        "quality_score": 72,
        "evidence_for": ["HTF_TREND_ALIGNED", "STRUCTURE_RETEST"],
        "evidence_against": ["funding levemente positivo"],
    }
    payload["entry"] = {
        "type": "LIMIT",
        "price": "64250.00",
        "price_min": None,
        "price_max": None,
        "confirmation": "fechamento de 5m acima de 64300",
        "expires_at": "2026-07-19T13:00:00Z",
    }
    payload["risk_plan"] = {
        "invalidation_price": "63800.00",
        "stop_loss": "63750.00",
        "take_profit_levels": [
            {"price": "65200.00", "close_fraction": "0.50", "reason": "máxima anterior"},
            {"price": "66000.00", "close_fraction": "0.50", "reason": "liquidez"},
        ],
        "estimated_rr_gross": "2.6",
        "estimated_rr_net": "2.2",
        "maximum_slippage_bps": "8",
    }
    validator.validate(payload)


def test_extra_field_is_rejected(validator: Draft202012Validator) -> None:
    payload = _valid_no_trade()
    payload["confidence"] = "alta"
    with pytest.raises(ValidationError):
        validator.validate(payload)


def test_missing_required_field_is_rejected(validator: Draft202012Validator) -> None:
    payload = _valid_no_trade()
    del payload["risk_plan"]
    with pytest.raises(ValidationError):
        validator.validate(payload)


def test_unknown_action_is_rejected(validator: Draft202012Validator) -> None:
    payload = _valid_no_trade()
    payload["action"] = "OPEN_LONG_AGGRESSIVE"
    with pytest.raises(ValidationError):
        validator.validate(payload)


def test_numeric_price_is_rejected(validator: Draft202012Validator) -> None:
    """Preço tem que ser string. 64250.0 como number já perdeu precisão."""
    payload = _valid_no_trade()
    payload["entry"]["type"] = "LIMIT"
    payload["entry"]["price"] = 64250.00
    with pytest.raises(ValidationError):
        validator.validate(payload)


def test_wrong_symbol_is_rejected(validator: Draft202012Validator) -> None:
    payload = _valid_no_trade()
    payload["symbol"] = "ETHUSDT"
    with pytest.raises(ValidationError):
        validator.validate(payload)
