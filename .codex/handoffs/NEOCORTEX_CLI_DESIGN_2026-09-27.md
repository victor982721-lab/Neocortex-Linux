# Handoff — jerarquía visual CLI y conteos ZIP

**Ronda:** NEO-CLI-DESIGN-20260927.
**Estado:** publicación, instalación y verificación terminadas, sin corpus real.
**Código:** `12dddb20d227ba4a7285ce9eba82117755615f34`.
**Release:** `0.14.1-12dddb20d227-cp313-linux-x86_64`.
**Rollback:** `0.14.1-d854809926ca-cp313-linux-x86_64`.

Etapas enmarcadas y centradas con padding/ancho adaptable; tablas homogéneas.
En estrecho se conservan avance/estado/errores con stack; en poca altura el foco
prioriza avisos/trabajo y declara tareas fuera de vista. Historial completo al
finalizar; stream intacto. NO_COLOR y ASCII son mecanismos distintos.

Revisar contenedores ya no rotula paquetes como archivos .zip. El desglose
separa nombres inventariados/admitidos, tipos, desconocidos y bloqueos; no crea
lecturas de corpus en el renderer, no suma categorías solapadas ni fabrica
extracted_files desde members/published boolean. Cache seeds atómicos conservan
el gate original del resultado engine. No cambian políticas o efectos ZIP.

Investigación: siete referentes CLI en fuentes primarias, con adopción separada
de calidad. Se conserva Rich, sin TUI/Go/dependencias nuevas. Informe/capturas:
`/home/ubuntu/Documents/NeoCortex/Auditorias/2026-09-27-cli-design/`.

- 228 pruebas focales y 2 subtests. Ruff/Semgrep pasan; Mypy94→93 y Pyright17→17
  sin diagnósticos nuevos, no globalmente limpios.
- Revisión independiente visual_review acepta V1/Z2/C1 y las ocho capturas.
- Instalador verify: SHA exacto, SQLite approved, ephemeral_empty_v1.
- PTY instalado120×60 rc0: cabeceras enmarcadas, stdoutJSON limpio, fixture intacto.
- API ZIP instalada y replay: orden, una publicación, originales/atómicos intactos.
- PNG regenerados desde la release instalada, con fixtures propios, sin PYTHONPATH
  ni imports del checkout. No acreditan inferencia o throughput sobre Corpus.

AGENTS.md previo intacto; build desde clon limpio del main publicado. Los commits
posteriores sólo de handoffs no alteran inputs ejecutables validados ni la release.
