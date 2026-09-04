# NeoCortex — handoff operativo 0.10.0

> Actualizado: 2026-09-04, America/Mexico_City. Fuente canónica: `main` del
> repositorio `victor982721-lab/Neocortex-Linux`.

## Estado

- La línea 0.10 añade `verification_mode` al inventario y conserva
  `legacy_unknown` para planes históricos, separando candidatos `fast`,
  parciales y `full_hash` sin inferir exactitud retrospectiva.
- `curate scan`/`curate plan`/`curate verify` comparten digest, cursor y
  `source_heads` de inventario y catálogo; verify revalida identidad física,
  symlinks de ancestros, hash completo y bytes mediante descriptores
  `O_NOFOLLOW`, con límites de archivos/bytes y razones de abstención.
- API, SDK, CLI y MCP proyectan scan/verify; MCP mantiene `readOnlyHint=true`,
  no expone autorización y no crea `file_actions`, KIO ni efectos sobre el
  corpus.
- Los grants nuevos persisten un manifiesto append-only de heads de ReviewTask,
  con task/version/evento, fingerprints, selector y digest agregado; los grants
  legacy sin manifiesto permanecen legibles pero no son consumibles por un
  futuro `apply`.

## Validación del vertical

- Fixtures contenidos cubren duplicados exactos, candidatos fast, páginas de más
  de 100 elementos, selección vacía, symlink intermedio, presupuesto de bytes,
  plan heterogéneo, mutación con mtime restaurado, replay y cero mutación del
  corpus/owners.
- Ruff y `compileall` pasan; la suite Linux relevante pasa con 4,568 casos,
  70 omitidos, 135 subtests y un caso legado excluido por la carrera conocida
  de heartbeat/SQLite durante la suite completa.
- El test Windows/NTFS permanece fuera del alcance Linux-only; no se ejecutó
  GitHub Actions ni se procesó el corpus real.

## Gates restantes

1. Bump explícito a `0.10.0` y commit de release desde el árbol ejecutable
   validado.
2. Build offline e instalación desde el SHA final con `release_linux.py`,
   `release_linux.py verify`, manifest, launcher, `current` y rollback.
3. Smoke público y segunda ejecución de replay sin `PYTHONPATH`, con fixture
   aislado y cero `file_actions`.
4. Mantener fuera de esta versión `apply → verify → reconcile`, KIO real,
   autenticación de principal MCP, recovery de restore y endurecimiento general
   de fences SQLite, que corresponden a 0.11.x o a un gate independiente.
