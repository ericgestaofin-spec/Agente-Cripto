"""Testes do próprio mutador.

O mutador é um gate de qualidade — se ele reporta mutante equivalente
como sobrevivente, ou muta conteúdo de string, o gate perde credibilidade
e a equipe aprende a ignorá-lo. Estes testes fixam o comportamento que o
torna confiável.
"""

from __future__ import annotations

from pathlib import Path

from tools.mutation.mutate import find_sites

# test_mutation_tool.py está em tests/unit/ — a raiz do repo é parents[2].
ROOT = Path(__file__).resolve().parents[2]


def test_enumerates_sites_in_risk_modules() -> None:
    sites = find_sites(ROOT)
    assert len(sites) > 30
    assert all("risk/" in s.path.as_posix() for s in sites)


def test_does_not_mutate_operators_inside_strings() -> None:
    """O '<=' dentro de f'... <= limite' é texto de mensagem, não lógica.
    Mutá-lo seria um falso positivo — a razão de usar tokenize."""
    sites = find_sites(ROOT)
    # validators tem várias mensagens com <=, >= no texto. Nenhum site
    # pode cair numa coluna que esteja dentro de uma string literal.
    for s in sites:
        line = s.path.read_text(encoding="utf-8").splitlines()[s.line_no - 1]
        before = line[: s.col]
        # heurística: um operador de lógica real não está dentro de aspas
        # abertas na mesma linha antes dele
        assert before.count('"') % 2 == 0, (
            f"mutação dentro de string em {s.label}: {line.strip()}"
        )


def test_skips_param_separator_star() -> None:
    """`def f(x, *, y)` — o '*' é keyword-only, não multiplicação.
    Mutá-lo produz mutante equivalente."""
    sites = find_sites(ROOT)
    for s in sites:
        line = s.path.read_text(encoding="utf-8").splitlines()[s.line_no - 1]
        if s.original == "*":
            # não pode ser um '*' seguido imediatamente de ',' ou ')'
            after = line[s.col + 1 :].lstrip()
            assert not after.startswith((",", ")")), (
                f"marcador de parâmetro mutado em {s.label}"
            )


def test_skips_pragma_no_cover_lines() -> None:
    sites = find_sites(ROOT)
    for s in sites:
        line = s.path.read_text(encoding="utf-8").splitlines()[s.line_no - 1]
        assert "pragma: no cover" not in line


def test_every_site_has_valid_mutation() -> None:
    sites = find_sites(ROOT)
    for s in sites:
        assert s.mutant != s.original
        line = s.path.read_text(encoding="utf-8").splitlines()[s.line_no - 1]
        assert line[s.col : s.col + len(s.original)] == s.original
