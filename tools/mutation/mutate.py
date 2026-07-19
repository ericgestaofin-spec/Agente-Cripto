"""Mutador mínimo para o Risk Engine.

Por que existe: mutmut 3.x não roda em Windows nativo, e mutatest quebra
com Python 3.13. Mutation testing não é opcional no `risk/` — é o gate
que distingue "os testes executam o código" de "os testes pegam o código
errado". Então construímos um.

O que faz: usa o `tokenize` para mutar SOMENTE operadores reais (tokens
OP e as palavras `and`/`or`), nunca conteúdo de string, comentário ou
docstring. Para cada mutação, roda a suite unitária de risco e confirma
que ela FALHA. Uma mutação que sobrevive (testes verdes com código
mutado) é um buraco de comportamento — o tipo de bug que 100% de
cobertura de linha não pega.

Usar tokenize é o que separa um mutador útil de um que produz falsos
positivos (operador dentro de f-string de mensagem) — e um mutador com
falso positivo é um mutador que se aprende a ignorar.

Score = mutações mortas / mutações totais. Gate: >= 90%.

Uso:
    python -m tools.mutation.mutate            # muta risk/, roda o gate
    python -m tools.mutation.mutate --list     # só lista as mutações
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import token
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

TARGETS: Final[tuple[str, ...]] = (
    "src/bybit_agent/risk/sizing.py",
    "src/bybit_agent/risk/validators.py",
    "src/bybit_agent/risk/engine.py",
    "src/bybit_agent/risk/policy.py",
)

TEST_CMD: Final[list[str]] = [
    sys.executable, "-m", "pytest",
    "tests/unit/risk",
    "-x", "-q", "--no-header", "-p", "no:cacheprovider",
]

# Mutações de operador (token OP → mutante).
OP_MUTATIONS: Final[dict[str, str]] = {
    "<=": "<",
    ">=": ">",
    "==": "!=",
    "!=": "==",
    "<": "<=",
    ">": ">=",
    "+": "-",
    "-": "+",
    "*": "/",
}

# Mutações de operador booleano (token NAME).
BOOL_MUTATIONS: Final[dict[str, str]] = {"and": "or", "or": "and"}


@dataclass
class MutationSite:
    path: Path
    line_no: int
    col: int
    original: str
    mutant: str

    @property
    def label(self) -> str:
        rel = self.path.as_posix().split("bybit_agent/")[-1]
        return f"{rel}:{self.line_no} [{self.original}]"


@dataclass
class Report:
    killed: list[MutationSite] = field(default_factory=list)
    survived: list[MutationSite] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.killed) + len(self.survived)

    @property
    def score(self) -> float:
        return len(self.killed) / self.total if self.total else 0.0


def _is_param_separator(tokens: list[tokenize.TokenInfo], i: int) -> bool:
    """True se o `*` ou `/` na posição i é marcador de parâmetro, não
    operador aritmético.

    `def f(x, *, y)` e `def f(x, /, y)` usam `*`/`/` como separadores —
    mutá-los produz sintaxe válida com comportamento idêntico (mutante
    equivalente). Distinguimos pelo vizinho: um separador é seguido por
    `,` ou `)`; uma multiplicação/divisão tem um operando à direita.
    """
    nxt = tokens[i + 1] if i + 1 < len(tokens) else None
    return nxt is not None and nxt.type == token.OP and nxt.string in {",", ")"}


def find_sites(root: Path) -> list[MutationSite]:
    """Enumera mutações via tokenize — só operadores reais e significativos.

    Ignora naturalmente (tokenize): strings, comentários, docstrings.
    Ignora explicitamente (mutantes equivalentes): marcadores de parâmetro
    `*`/`/` e linhas marcadas `pragma: no cover` (código inatingível por
    design). Reportar um mutante equivalente como sobrevivente é ruído que
    desvaloriza o gate.
    """
    sites: list[MutationSite] = []
    for target in TARGETS:
        path = root / target
        source = path.read_text(encoding="utf-8")
        source_lines = source.splitlines()
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
        for i, tok in enumerate(tokens):
            text = tok.string
            line = source_lines[tok.start[0] - 1] if tok.start[0] <= len(source_lines) else ""
            if "pragma: no cover" in line:
                continue

            mutant: str | None = None
            if tok.type == token.OP and text in OP_MUTATIONS:
                if text in {"*", "/"} and _is_param_separator(tokens, i):
                    continue
                mutant = OP_MUTATIONS[text]
            elif tok.type == token.NAME and text in BOOL_MUTATIONS:
                mutant = BOOL_MUTATIONS[text]

            if mutant is not None:
                sites.append(
                    MutationSite(
                        path=path,
                        line_no=tok.start[0],
                        col=tok.start[1],
                        original=text,
                        mutant=mutant,
                    )
                )
    return sites


def _apply(source: str, site: MutationSite) -> str:
    """Aplica a mutação na coluna exata da linha — precisão de token."""
    lines = source.splitlines(keepends=True)
    line = lines[site.line_no - 1]
    end = site.col + len(site.original)
    assert line[site.col:end] == site.original, (
        f"token não bate em {site.label}: {line[site.col:end]!r}"
    )
    lines[site.line_no - 1] = line[: site.col] + site.mutant + line[end:]
    return "".join(lines)


def _run_tests(root: Path) -> bool:
    """True se a suite passa. errors='replace' evita que um byte fora do
    cp1252 na saída do pytest derrube o próprio mutador no Windows."""
    import os

    result = subprocess.run(
        TEST_CMD,
        cwd=root,
        env={**os.environ, "PYTHONPATH": "src", "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode == 0


def run_gate(root: Path, *, threshold: float = 0.90) -> int:
    sites = find_sites(root)
    print(f"mutation: {len(sites)} mutações em {len(TARGETS)} módulos\n", flush=True)

    report = Report()
    for idx, site in enumerate(sites, start=1):
        original_source = site.path.read_text(encoding="utf-8")
        try:
            site.path.write_text(_apply(original_source, site), encoding="utf-8")
            passed = _run_tests(root)
        finally:
            site.path.write_text(original_source, encoding="utf-8")

        if passed:
            report.survived.append(site)
            mark = "SOBREVIVEU"
        else:
            report.killed.append(site)
            mark = "morta"
        print(f"  [{idx:>3}/{len(sites)}] {mark:<11} {site.label} -> {site.mutant}",
              flush=True)

    print(f"\nscore: {report.score:.1%} "
          f"({len(report.killed)} mortas / {report.total})", flush=True)

    if report.survived:
        print("\nMUTAÇÕES SOBREVIVENTES (buracos de comportamento):", flush=True)
        for s in report.survived:
            print(f"  {s.label}  {s.original} -> {s.mutant}", flush=True)

    if report.score < threshold:
        print(f"\nGATE FALHOU: {report.score:.1%} < {threshold:.0%}", flush=True)
        return 1
    print(f"\nGATE OK: {report.score:.1%} >= {threshold:.0%}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mutation testing do Risk Engine")
    parser.add_argument("--list", action="store_true", help="só lista as mutações")
    parser.add_argument("--threshold", type=float, default=0.90)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]

    if args.list:
        for site in find_sites(root):
            print(f"{site.label} -> {site.mutant}")
        return 0

    return run_gate(root, threshold=args.threshold)


if __name__ == "__main__":
    sys.exit(main())
