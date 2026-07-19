"""Sprint 7 — cliente do agente Claude.

Testado com um fake da API (injeção de dependência) — sem chave, sem rede.
Verifica que a requisição é montada corretamente (modelo, thinking
explícito, structured output com o schema, cache_control) e que a resposta
é parseada. A chamada ao vivo precisa da chave Anthropic.
"""

from __future__ import annotations

import json
from typing import Any

from bybit_agent.agent.client import DecisionAgent
from bybit_agent.agent.prompt import SYSTEM_PROMPT_VERSION


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, decision: dict[str, Any]) -> None:
        self.content = [_FakeBlock(json.dumps(decision))]
        self.stop_reason = "end_turn"
        self.stop_details = None

    class _Usage:
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 100
        input_tokens = 50
        output_tokens = 200

    usage = _Usage()


class _FakeMessages:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        return self._response


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self.messages = _FakeMessages(response)


def _decision() -> dict[str, Any]:
    return {"action": "NO_TRADE", "symbol": "BTCUSDT", "summary": "sem setup"}


def _snapshot() -> dict[str, Any]:
    return {"symbol": "BTCUSDT", "data_age_ms": 100, "market_regime": "RANGE"}


# --------------------------------------------------------------------------
# Requisição bem formada
# --------------------------------------------------------------------------


def test_request_uses_opus_4_8() -> None:
    fake = _FakeClient(_FakeResponse(_decision()))
    agent = DecisionAgent(client=fake, decision_schema={"type": "object"})
    agent.analyze(_snapshot())
    assert fake.messages.last_kwargs is not None
    assert fake.messages.last_kwargs["model"] == "claude-opus-4-8"


def test_request_enables_adaptive_thinking_explicitly() -> None:
    """⭐ No Opus 4.8 o thinking é OFF por padrão. Precisa ser explícito."""
    fake = _FakeClient(_FakeResponse(_decision()))
    DecisionAgent(client=fake, decision_schema={"type": "object"}).analyze(_snapshot())
    assert fake.messages.last_kwargs["thinking"] == {"type": "adaptive"}


def test_request_uses_structured_output_with_schema() -> None:
    schema = {"type": "object", "properties": {"action": {"type": "string"}}}
    fake = _FakeClient(_FakeResponse(_decision()))
    DecisionAgent(client=fake, decision_schema=schema).analyze(_snapshot())
    fmt = fake.messages.last_kwargs["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] == schema


def test_request_has_no_temperature() -> None:
    """temperature/top_p são removidos no Opus 4.8 — 400 se presentes."""
    fake = _FakeClient(_FakeResponse(_decision()))
    DecisionAgent(client=fake, decision_schema={"type": "object"}).analyze(_snapshot())
    assert "temperature" not in fake.messages.last_kwargs
    assert "top_p" not in fake.messages.last_kwargs


def test_system_prompt_has_cache_control() -> None:
    fake = _FakeClient(_FakeResponse(_decision()))
    DecisionAgent(client=fake, decision_schema={"type": "object"}).analyze(_snapshot())
    system = fake.messages.last_kwargs["system"]
    assert isinstance(system, list)
    assert system[-1]["cache_control"] == {"type": "ephemeral"}


def test_snapshot_goes_in_user_message_not_system() -> None:
    """⭐ O snapshot é volátil — vai na mensagem do usuário, para não
    invalidar o cache do system prompt (que deve ser byte-idêntico entre
    chamadas)."""
    import json as _json

    snap = _snapshot()
    fake = _FakeClient(_FakeResponse(_decision()))
    DecisionAgent(client=fake, decision_schema={"type": "object"}).analyze(snap)
    kwargs = fake.messages.last_kwargs
    # o snapshot serializado está na mensagem do usuário
    user_content = kwargs["messages"][0]["content"]
    assert _json.loads(user_content) == snap
    # o system é o prompt estável — não contém o snapshot serializado
    system_text = "".join(b["text"] for b in kwargs["system"])
    assert user_content not in system_text


# --------------------------------------------------------------------------
# Resposta
# --------------------------------------------------------------------------


def test_analyze_returns_parsed_decision_dict() -> None:
    fake = _FakeClient(_FakeResponse(_decision()))
    agent = DecisionAgent(client=fake, decision_schema={"type": "object"})
    result = agent.analyze(_snapshot())
    assert result.decision["action"] == "NO_TRADE"
    assert result.system_prompt_version == SYSTEM_PROMPT_VERSION


def test_refusal_stop_reason_is_handled() -> None:
    """⭐ stop_reason 'refusal' não pode ser lido como decisão normal."""
    resp = _FakeResponse(_decision())
    resp.stop_reason = "refusal"
    fake = _FakeClient(resp)
    agent = DecisionAgent(client=fake, decision_schema={"type": "object"})
    result = agent.analyze(_snapshot())
    assert result.refused
    assert result.decision["action"] == "HALT_TRADING"


def test_analyze_records_usage_for_cost_tracking() -> None:
    fake = _FakeClient(_FakeResponse(_decision()))
    agent = DecisionAgent(client=fake, decision_schema={"type": "object"})
    result = agent.analyze(_snapshot())
    assert result.usage["output_tokens"] == 200
