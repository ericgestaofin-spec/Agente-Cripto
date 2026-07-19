"""Lint AST: proíbe `float` em módulos que tocam dinheiro.

Gate de CI bloqueante. Convenção de equipe não sobrevive a um deploy às
23h de sexta; um gate mecânico sobrevive.

Escopo: apenas os pacotes em PROTECTED_PACKAGES. Fora deles float é
legítimo (latência, percentuais de UI, métricas de observabilidade).

Escape hatch: `# noqa: no-float` na linha, para os casos raros e
deliberados. Deve ser justificado em code review.

Uso:
    python -m tools.lint.no_float          # varre o repo, exit 1 se violar
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PROTECTED_PACKAGES: Final[tuple[str, ...]] = (
    "domain",
    "risk",
    "execution",
    "marketdata",
    "features",
)

NOQA_MARKER: Final[str] = "noqa: no-float"


@dataclass(frozen=True, slots=True)
class Violation:
    path: Path
    line: int
    message: str


class _FloatVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, source_lines: list[str]) -> None:
        self.path = path
        self.lines = source_lines
        self.violations: list[Violation] = []

    def _suppressed(self, line: int) -> bool:
        if 1 <= line <= len(self.lines):
            return NOQA_MARKER in self.lines[line - 1]
        return False

    def _report(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", 0)
        if not self._suppressed(line):
            self.violations.append(Violation(self.path, line, message))

    # -- literais: 0.25, 1e-9 ------------------------------------------------
    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, float) and not isinstance(node.value, bool):
            self._report(node, f"literal float {node.value!r}")
        self.generic_visit(node)

    # -- chamadas: float(x) --------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "float":
            # isinstance(v, float) é legítimo — é como se REJEITA float.
            self._report(node, "chamada float() converte para ponto flutuante")
        self.generic_visit(node)

    # -- anotações: x: float -------------------------------------------------
    def visit_arg(self, node: ast.arg) -> None:
        self._check_annotation(node.annotation, node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_annotation(node.returns, node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_annotation(node.returns, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_annotation(node.annotation, node)
        self.generic_visit(node)

    def _check_annotation(self, annotation: ast.expr | None, node: ast.AST) -> None:
        if annotation is None:
            return
        for sub in ast.walk(annotation):
            if isinstance(sub, ast.Name) and sub.id == "float":
                self._report(node, "anotação de tipo float")
            elif isinstance(sub, ast.Constant) and sub.value == "float":
                self._report(node, "anotação de tipo float (string)")


def scan_source(source: str, *, path: Path) -> list[Violation]:
    """Varre um trecho de código. Usado pelos testes e por scan_repository.

    Um arquivo que não parseia é reportado como violação, não como crash:
    o CI precisa mostrar qual arquivo está quebrado, não um traceback do
    próprio linter.
    """
    source = source.lstrip("﻿")  # BOM de editores Windows
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 0, f"arquivo não parseia: {exc.msg}")]
    visitor = _FloatVisitor(path, source.splitlines())
    visitor.visit(tree)
    return sorted(visitor.violations, key=lambda v: v.line)


def _is_protected(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return any(pkg in parts for pkg in PROTECTED_PACKAGES)


def scan_repository(root: Path) -> list[Violation]:
    """Varre src/bybit_agent, retornando violações nos pacotes protegidos."""
    src = root / "src" / "bybit_agent"
    if not src.exists():
        return []

    violations: list[Violation] = []
    for py_file in sorted(src.rglob("*.py")):
        if not _is_protected(py_file, root):
            continue
        source = py_file.read_text(encoding="utf-8-sig")
        violations.extend(scan_source(source, path=py_file))
    return violations


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    violations = scan_repository(root)

    if not violations:
        print(f"no-float: OK — pacotes protegidos limpos {PROTECTED_PACKAGES}")
        return 0

    print(f"no-float: {len(violations)} violação(ões) em caminho de dinheiro:\n")
    for v in violations:
        rel = v.path.relative_to(root)
        print(f"  {rel}:{v.line} — {v.message}")
    print("\nUse Decimal. Se for deliberado, marque com '# noqa: no-float'.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
