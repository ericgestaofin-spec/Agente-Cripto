"""Cliente do agente Claude — monta a requisição e parseia a decisão.

Usa o Opus 4.8 com structured output: o `output_config.format` GARANTE que
a resposta valida contra o schema da decisão, então não pedimos ao modelo
para "retornar JSON válido" — é uma restrição de decodificação, não uma
instrução (ver docs/PLANO.md §0.1).

Regras do Opus 4.8 que quebram se ignoradas:
  - thinking é OFF por padrão → setar {"type": "adaptive"} explicitamente
  - temperature/top_p/top_k removidos → não enviar (400)
  - prefill de assistant removido → usamos structured output

Testável sem chave: o cliente Anthropic é injetado. Em produção, criamos
um `anthropic.Anthropic()` (lê a chave do ambiente).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from bybit_agent.agent.prompt import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION

MODEL: str = "claude-opus-4-8"
MAX_TOKENS: int = 8000


@dataclass(frozen=True, slots=True)
class AgentResult:
    decision: dict[str, Any]
    system_prompt_version: str
    refused: bool = False
    usage: dict[str, int] = field(default_factory=dict)


class DecisionAgent:
    """Envia o snapshot ao Claude e devolve a decisão estruturada."""

    def __init__(
        self,
        *,
        decision_schema: dict[str, Any],
        client: Any = None,
        effort: str = "high",
    ) -> None:
        self._schema = decision_schema
        self._effort = effort
        self._client = client  # injetado nos testes; criado sob demanda em prod

    def _ensure_client(self) -> Any:
        if self._client is None:  # pragma: no cover - caminho de produção com chave
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def analyze(self, snapshot: dict[str, Any]) -> AgentResult:
        """Uma decisão a partir de um snapshot. O snapshot vai na mensagem
        do usuário (volátil), não no system (estável e cacheado)."""
        client = self._ensure_client()

        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={
                "effort": self._effort,
                "format": {"type": "json_schema", "schema": self._schema},
            },
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(snapshot, ensure_ascii=False),
                }
            ],
        )

        usage = _extract_usage(response)

        # Refusal: não pode ser lido como decisão normal. Vira HALT.
        if getattr(response, "stop_reason", None) == "refusal":
            return AgentResult(
                decision={"action": "HALT_TRADING", "reason": "model_refusal"},
                system_prompt_version=SYSTEM_PROMPT_VERSION,
                refused=True,
                usage=usage,
            )

        text = next(
            (b.text for b in response.content if getattr(b, "type", None) == "text"),
            None,
        )
        if text is None:
            raise ValueError("resposta do modelo sem bloco de texto")

        decision = json.loads(text)
        return AgentResult(
            decision=decision,
            system_prompt_version=SYSTEM_PROMPT_VERSION,
            usage=usage,
        )


def _extract_usage(response: Any) -> dict[str, int]:
    u = getattr(response, "usage", None)
    if u is None:  # pragma: no cover
        return {}
    return {
        "input_tokens": getattr(u, "input_tokens", 0),
        "output_tokens": getattr(u, "output_tokens", 0),
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0),
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0),
    }
