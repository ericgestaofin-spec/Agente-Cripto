---
name: mutation-gate-no-concurrent-tests
description: Nunca rodar pytest concorrente com o mutation gate deste projeto
metadata:
  type: feedback
---

O mutation gate custom (`tools/mutation/mutate.py`) muta arquivos-fonte de
`risk/` em disco, roda a suite, e restaura via `finally`. **Rodar qualquer
outro `pytest` que toque `risk/` enquanto o gate roda corrompe os dois:** o
resultado do gate vem contaminado (falsos sobreviventes) e a suite paralela
falha com mutações transitórias.

**Why:** ambos os processos leem/escrevem os mesmos arquivos sem lock. Já
aconteceu duas vezes — a segunda gerou um 85,3% falso com 10 "sobreviventes"
que na verdade eram artefato de corrida.

**How to apply:** ao lançar o gate em background, NÃO rodar cobertura, suite
completa, nem outro gate até ele terminar. Usar Monitor esperando `^GATE` no
`docs/mutation_result.txt`. Só então rodar a suite. Lançar o gate UMA vez por
vez (não duplicar em PowerShell + Bash). Ver [[risk-engine-review-batches]].
