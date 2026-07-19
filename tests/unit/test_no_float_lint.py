"""Meta-testes do lint anti-float.

O lint é um gate de CI bloqueante. Um gate que não pega o que promete
pegar é pior que nenhum gate — dá falsa confiança. Estes testes verificam
o próprio detector plantando violações de propósito.
"""

from __future__ import annotations

from pathlib import Path

from tools.lint.no_float import Violation, scan_source

# --------------------------------------------------------------------------
# Deve DETECTAR
# --------------------------------------------------------------------------


def _scan(code: str) -> list[Violation]:
    return scan_source(code, path=Path("fake.py"))


def test_detects_float_literal() -> None:
    violations = _scan("risco = 0.25\n")
    assert len(violations) == 1
    assert "literal float" in violations[0].message


def test_detects_float_call() -> None:
    violations = _scan("qty = float(entrada)\n")
    assert len(violations) == 1
    assert "float(" in violations[0].message


def test_detects_float_annotation() -> None:
    violations = _scan("def size(preco: float) -> None: ...\n")
    assert len(violations) == 1
    assert "anotação" in violations[0].message


def test_detects_float_return_annotation() -> None:
    violations = _scan("def pnl() -> float: ...\n")
    assert len(violations) == 1


def test_detects_float_in_nested_function() -> None:
    code = "def outer():\n    def inner():\n        return 1.5\n"
    assert len(_scan(code)) == 1


def test_detects_multiple_violations_with_line_numbers() -> None:
    code = "a = 1.0\nb = 2\nc = float(b)\n"
    violations = _scan(code)
    assert len(violations) == 2
    assert violations[0].line == 1
    assert violations[1].line == 3


def test_detects_float_in_scientific_notation() -> None:
    assert len(_scan("epsilon = 1e-9\n")) == 1


# --------------------------------------------------------------------------
# NÃO deve detectar (falsos positivos são caros — desligam o gate)
# --------------------------------------------------------------------------


def test_ignores_integers() -> None:
    assert _scan("n = 42\n") == []


def test_ignores_decimal_from_string() -> None:
    assert _scan('x = Decimal("0.25")\n') == []


def test_ignores_string_containing_float_word() -> None:
    assert _scan('msg = "float é proibido"\n') == []


def test_ignores_comment_mentioning_float() -> None:
    assert _scan("# float não pode ser usado aqui\nx = 1\n") == []


def test_ignores_isinstance_float_check() -> None:
    """Rejeitar float exige mencionar float. O próprio money.py faz isso."""
    code = "if isinstance(v, float):\n    raise TypeError('float')\n"
    assert _scan(code) == []


def test_ignores_explicitly_allowed_line() -> None:
    assert _scan("latency = 0.5  # noqa: no-float\n") == []


# --------------------------------------------------------------------------
# Robustez — o linter não pode derrubar o CI com traceback
# --------------------------------------------------------------------------


def test_handles_utf8_bom_without_crashing() -> None:
    """Editores Windows gravam BOM. O linter deve enxergar o código,
    não estourar com 'invalid non-printable character U+FEFF'."""
    violations = _scan("﻿risco = 0.25\n")
    assert len(violations) == 1
    assert "literal float" in violations[0].message


def test_syntax_error_is_reported_not_raised() -> None:
    """Arquivo quebrado vira violação legível, não traceback do linter."""
    violations = _scan("def broken(:\n")
    assert len(violations) == 1
    assert "não parseia" in violations[0].message


# --------------------------------------------------------------------------
# O código real está limpo
# --------------------------------------------------------------------------


def test_protected_modules_have_zero_float_violations() -> None:
    """Se este teste falhar, alguém introduziu float em caminho de dinheiro."""
    from tools.lint.no_float import PROTECTED_PACKAGES, scan_repository

    root = Path(__file__).resolve().parents[2]
    violations = scan_repository(root)
    assert violations == [], (
        f"float encontrado em módulo protegido ({PROTECTED_PACKAGES}):\n"
        + "\n".join(f"  {v.path}:{v.line} — {v.message}" for v in violations)
    )
