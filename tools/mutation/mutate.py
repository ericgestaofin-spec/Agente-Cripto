"""Mutador mínimo para o Risk Engine.

Por que existe: mutmut 3.x não roda em Windows nativo, e mutatest quebra
com Python 3.13. Mutation testing não é opcional no `risk/` — é o gate
que distingue "os testes executam o código" de "os testes pegam o código
errado". Então construímos um.

O que faz: aplica mutações operador a operador no código-fonte (troca
`<=` por `<`, `+` por `-`, `and` por `or`, etc.), roda a suite de risco
para cada mutação, e confirma que ela FALHA. Uma mutação que sobrevive
(testes continuam verdes com o código mutado) é um buraco na cobertura de
comportamento — o tipo de bug que 100% de cobertura de linha não pega.

Score = mutações mortas / mutações totais. Gate: >= 90%.

Uso:
    python -m tools.mutation.mutate            # muta risk/, roda o gate
    python -m tools.mutation.mutate --list     # só lista as mutações
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

# Alvos: os módulos onde uma mutação silenciosa custa dinheiro.
TARGETS: Final[tuple[str, ...]] = (
    "src/bybit_agent/risk/sizing.py",
    "src/bybit_agent/risk/validators.py",
    "src/bybit_agent/risk/engine.py",
    "src/bybit_agent/risk/policy.py",
)

# Só os testes unitários de risco: são 74, cobrem 100% dos branches e
# matam mutações de operador com asserções explícitas. Os property tests
# (1000 casos × 9 propriedades) custam 10× mais no loop de mutação sem
# ganho proporcional de kill — rodam separado, no CI.
TEST_CMD: Final[list[str]] = [
    sys.executable, "-m", "pytest",
    "tests/unit/risk",
    "-x", "-q", "--no-header", "-p", "no:cacheprovider",
]

# Mutações de operador. Cada par (original, mutante) é aplicado a cada
# ocorrência textual isolada. Ordem importa: os mais longos primeiro,
# para '<=' não ser parcialmente mutado por '<'.
MUTATIONS: Final[tuple[tuple[str, str], ...]] = (
    (" <= ", " < "),
    (" >= ", " > "),
    (" == ", " != "),
    (" and ", " or "),
    (" + ", " - "),
    (" - ", " + "),
    (" * ", " / "),
    (" < ", " <= "),
    (" > ", " >= "),
)


@dataclass
class MutationSite:
    path: Path
    line_no: int
    original_line: str
    mutated_line: str
    operator: str

    @property
    def label(self) -> str:
        rel = self.path.as_posix().split("bybit_agent/")[-1]
        return f"{rel}:{self.line_no} [{self.operator.strip()}]"


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


def _skip_line(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith("#")
        or stripped.startswith('"')
        or stripped.startswith("'")
        or "no cover" in line
    )


def find_sites(root: Path) -> list[MutationSite]:
    """Enumera todas as mutações aplicáveis, uma por ocorrência de operador."""
    sites: list[MutationSite] = []
    for target in TARGETS:
        path = root / target
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, start=1):
            if _skip_line(line):
                continue
            for original, mutant in MUTATIONS:
                if original in line:
                    # Uma mutação por (linha, operador) — a primeira ocorrência.
                    mutated = line.replace(original, mutant, 1)
                    if mutated != line:
                        sites.append(
                            MutationSite(path, i, line, mutated, original)
                        )
    return sites


def _run_tests(root: Path) -> bool:
    """True se a suite passa (todos os testes verdes)."""
    result = subprocess.run(
        TEST_CMD,
        cwd=root,
        env={"PYTHONPATH": "src", "PYTHONIOENCODING": "utf-8", "SYSTEMROOT": _sysroot()},
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _sysroot() -> str:
    import os

    return os.environ.get("SYSTEMROOT", r"C:\Windows")


def run_gate(root: Path, *, threshold: float = 0.90) -> int:
    sites = find_sites(root)
    print(f"mutation: {len(sites)} mutações em {len(TARGETS)} módulos\n")

    report = Report()
    for idx, site in enumerate(sites, start=1):
        original_source = site.path.read_text(encoding="utf-8")
        mutated_source = original_source.replace(
            site.original_line, site.mutated_line, 1
        )
        site.path.write_text(mutated_source, encoding="utf-8")
        try:
            passed = _run_tests(root)
        finally:
            site.path.write_text(original_source, encoding="utf-8")

        if passed:
            report.survived.append(site)
            mark = "SOBREVIVEU"
        else:
            report.killed.append(site)
            mark = "morta"
        print(f"  [{idx:>3}/{len(sites)}] {mark:<11} {site.label}")

    print(f"\nscore: {report.score:.1%} "
          f"({len(report.killed)} mortas / {report.total})")

    if report.survived:
        print("\nMUTAÇÕES SOBREVIVENTES (buracos de comportamento):")
        for s in report.survived:
            print(f"  {s.label}")
            print(f"    - {s.original_line.strip()}")
            print(f"    + {s.mutated_line.strip()}")

    if report.score < threshold:
        print(f"\nGATE FALHOU: {report.score:.1%} < {threshold:.0%}")
        return 1
    print(f"\nGATE OK: {report.score:.1%} >= {threshold:.0%}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mutation testing do Risk Engine")
    parser.add_argument("--list", action="store_true", help="só lista as mutações")
    parser.add_argument("--threshold", type=float, default=0.90)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]

    if args.list:
        for site in find_sites(root):
            print(site.label)
        return 0

    return run_gate(root, threshold=args.threshold)


if __name__ == "__main__":
    sys.exit(main())
