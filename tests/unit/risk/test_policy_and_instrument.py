"""Sprint 4a — política de risco e especificação do instrumento.

A política é a autoridade do sistema. O requisito central da spec é que
o modelo não possa modificá-la — nem por prompt, nem por ferramenta, nem
por variável de ambiente. Estes testes tornam isso mecânico.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from bybit_agent.domain.instrument import InstrumentSpec
from bybit_agent.risk.policy import RiskPolicy, load_policy

# --------------------------------------------------------------------------
# InstrumentSpec — espelha /v5/market/instruments-info
# --------------------------------------------------------------------------


def _bybit_payload() -> dict[str, object]:
    """Resposta real da Bybit para BTCUSDT linear (formato verificado)."""
    return {
        "symbol": "BTCUSDT",
        "status": "Trading",
        "priceFilter": {"tickSize": "0.10", "minPrice": "0.10", "maxPrice": "999999.00"},
        "lotSizeFilter": {
            "qtyStep": "0.001",
            "minOrderQty": "0.001",
            "maxOrderQty": "500.000",
            "maxMktOrderQty": "100.000",
            "minNotionalValue": "5",
        },
        "leverageFilter": {"minLeverage": "1", "maxLeverage": "100.00"},
    }


def test_parses_bybit_instruments_info_payload() -> None:
    spec = InstrumentSpec.from_bybit(_bybit_payload())
    assert spec.symbol == "BTCUSDT"
    assert spec.tick_size == Decimal("0.10")
    assert spec.qty_step == Decimal("0.001")
    assert spec.min_order_qty == Decimal("0.001")
    assert spec.min_notional == Decimal("5")
    assert spec.max_leverage == Decimal("100.00")


def test_market_and_limit_max_qty_are_distinct() -> None:
    """maxOrderQty (limit) e maxMktOrderQty (market) são limites diferentes.
    Confundi-los faz o sizing propor quantidade que a Bybit rejeita."""
    spec = InstrumentSpec.from_bybit(_bybit_payload())
    assert spec.max_order_qty == Decimal("500.000")
    assert spec.max_market_order_qty == Decimal("100.000")
    assert spec.max_qty_for(order_type="Limit") == Decimal("500.000")
    assert spec.max_qty_for(order_type="Market") == Decimal("100.000")


def test_all_numeric_fields_are_decimal() -> None:
    spec = InstrumentSpec.from_bybit(_bybit_payload())
    for name in (
        "tick_size",
        "qty_step",
        "min_order_qty",
        "max_order_qty",
        "max_market_order_qty",
        "min_notional",
        "max_leverage",
    ):
        assert isinstance(getattr(spec, name), Decimal), name


def test_instrument_is_immutable() -> None:
    spec = InstrumentSpec.from_bybit(_bybit_payload())
    with pytest.raises((FrozenInstanceError, AttributeError)):
        spec.tick_size = Decimal("1")  # type: ignore[misc]


def test_non_trading_status_is_rejected() -> None:
    """Símbolo suspenso não pode virar spec utilizável — a spec da Bybit
    continua vindo, mas operar nela é o circuit breaker 110074."""
    payload = _bybit_payload()
    payload["status"] = "Closed"
    with pytest.raises(ValueError, match="não está negociando"):
        InstrumentSpec.from_bybit(payload)


def test_missing_nested_field_fails_loudly() -> None:
    """Campo ausente vira erro explícito, nunca default silencioso.
    Um qty_step default de 0.001 num símbolo que usa 0.01 produz
    quantidades rejeitadas pela corretora."""
    payload = _bybit_payload()
    del payload["lotSizeFilter"]  # type: ignore[arg-type]
    with pytest.raises((KeyError, ValueError)):
        InstrumentSpec.from_bybit(payload)


# --------------------------------------------------------------------------
# RiskPolicy — imutabilidade é o requisito central
# --------------------------------------------------------------------------


def test_policy_has_conservative_defaults_from_spec() -> None:
    p = RiskPolicy.conservative_v0()
    assert p.max_risk_per_trade == Decimal("0.0025")  # 0,25%
    assert p.max_total_risk == Decimal("0.0050")
    assert p.max_daily_loss == Decimal("0.0100")
    assert p.max_weekly_loss == Decimal("0.0300")
    assert p.max_concurrent_positions == 1
    assert p.max_leverage == Decimal("2")
    assert p.min_rr_net == Decimal("2.0")
    assert p.max_consecutive_losses == 2
    assert p.max_daily_entries == 3
    assert p.allowed_symbols == frozenset({"BTCUSDT"})


def test_policy_is_frozen() -> None:
    """⭐ Nenhum caminho de código pode mutar a política em runtime."""
    p = RiskPolicy.conservative_v0()
    with pytest.raises((FrozenInstanceError, AttributeError)):
        p.max_risk_per_trade = Decimal("0.10")  # type: ignore[misc]


def test_policy_rejects_new_attributes() -> None:
    """slots=True impede injetar atributo novo para burlar validação."""
    p = RiskPolicy.conservative_v0()
    with pytest.raises((AttributeError, TypeError)):
        p.bypass_all_limits = True  # type: ignore[attr-defined]


def test_policy_allowed_symbols_is_immutable_collection() -> None:
    """set mutável permitiria adicionar símbolo em runtime."""
    p = RiskPolicy.conservative_v0()
    assert isinstance(p.allowed_symbols, frozenset)
    with pytest.raises(AttributeError):
        p.allowed_symbols.add("ETHUSDT")  # type: ignore[attr-defined]


def test_policy_hash_identifies_the_ruleset() -> None:
    """⭐ O hash vai no event log de toda decisão. Se a política mudar,
    é auditável qual decisão usou qual versão."""
    p = RiskPolicy.conservative_v0()
    assert len(p.policy_hash) == 64  # sha256 hex
    assert p.policy_hash == RiskPolicy.conservative_v0().policy_hash


def test_policy_hash_changes_when_any_limit_changes() -> None:
    base = RiskPolicy.conservative_v0()
    tighter = base.replace(max_risk_per_trade=Decimal("0.0010"))
    assert tighter.policy_hash != base.policy_hash


def test_replace_returns_new_instance_without_mutating() -> None:
    base = RiskPolicy.conservative_v0()
    original = base.max_risk_per_trade
    tighter = base.replace(max_risk_per_trade=Decimal("0.0010"))
    assert base.max_risk_per_trade == original
    assert tighter.max_risk_per_trade == Decimal("0.0010")


# -- validação de sanidade dos próprios limites ----------------------------


def test_rejects_risk_per_trade_above_total_risk() -> None:
    with pytest.raises(ValueError, match="risco por opera"):
        RiskPolicy.conservative_v0().replace(
            max_risk_per_trade=Decimal("0.02"), max_total_risk=Decimal("0.01")
        )


def test_rejects_daily_loss_above_weekly_loss() -> None:
    with pytest.raises(ValueError, match="di"):
        RiskPolicy.conservative_v0().replace(
            max_daily_loss=Decimal("0.05"), max_weekly_loss=Decimal("0.03")
        )


def test_rejects_non_positive_risk() -> None:
    with pytest.raises(ValueError):
        RiskPolicy.conservative_v0().replace(max_risk_per_trade=Decimal("0"))


def test_rejects_absurd_risk_per_trade() -> None:
    """Teto rígido de 5% — acima disso é erro de digitação, não estratégia.
    0,25 em vez de 0,0025 é o erro que apaga a conta."""
    with pytest.raises(ValueError, match="implausível"):
        RiskPolicy.conservative_v0().replace(max_risk_per_trade=Decimal("0.25"))


def test_rejects_leverage_below_one() -> None:
    with pytest.raises(ValueError):
        RiskPolicy.conservative_v0().replace(max_leverage=Decimal("0.5"))


def test_rejects_rr_below_one() -> None:
    """RR < 1 significa arriscar mais do que se busca ganhar."""
    with pytest.raises(ValueError, match="risco/retorno"):
        RiskPolicy.conservative_v0().replace(min_rr_net=Decimal("0.8"))


def test_rejects_empty_symbol_allowlist() -> None:
    with pytest.raises(ValueError, match="símbolo"):
        RiskPolicy.conservative_v0().replace(allowed_symbols=frozenset())


def test_rejects_zero_concurrent_positions() -> None:
    """max_concurrent_positions < 1 tornaria o sistema incapaz de operar."""
    with pytest.raises(ValueError, match="max_concurrent_positions"):
        RiskPolicy.conservative_v0().replace(max_concurrent_positions=0)


# -- carregamento -----------------------------------------------------------


def test_load_policy_reads_from_file(tmp_path: object) -> None:
    """A política vem de arquivo em disco, não de env var."""
    import json
    from pathlib import Path

    path = Path(str(tmp_path)) / "policy.json"
    path.write_text(
        json.dumps(
            {
                "max_risk_per_trade": "0.0025",
                "max_total_risk": "0.0050",
                "max_daily_loss": "0.0100",
                "max_weekly_loss": "0.0300",
                "max_concurrent_positions": 1,
                "max_leverage": "2",
                "min_rr_net": "2.0",
                "max_consecutive_losses": 2,
                "max_daily_entries": 3,
                "max_spread_bps": "5",
                "max_slippage_bps": "10",
                "max_data_age_ms": 5000,
                "allowed_symbols": ["BTCUSDT"],
            }
        ),
        encoding="utf-8",
    )
    policy = load_policy(path)
    assert policy.max_risk_per_trade == Decimal("0.0025")
    assert policy.allowed_symbols == frozenset({"BTCUSDT"})


def test_load_policy_rejects_float_in_file() -> None:
    """⭐ Números JSON viram float no parse. A política tem que usar
    strings decimais, senão 0.0025 já entra com erro de representação."""
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "policy.json"
        path.write_text(json.dumps({"max_risk_per_trade": 0.0025}), encoding="utf-8")
        with pytest.raises((TypeError, ValueError)):
            load_policy(path)
