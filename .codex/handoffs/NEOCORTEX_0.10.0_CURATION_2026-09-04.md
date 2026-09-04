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
- Ruff y `compileall` pasan; la suite Linux amplia del SHA de código registró
  4,547 pasadas, 70 omitidas y 137 subtests al excluir Windows/NTFS y el módulo
  run-control con su carrera conocida de heartbeat/SQLite, con un único fallo
  de identidad contra la distribución 0.9.0 todavía instalada en ese momento;
  el foco final de curation/MCP/fachadas/documentación pasa 93 casos.
- `0.10.0-86216627bb2e-cp314-linux-x86_64` quedó instalada desde el SHA
  `86216627bb2ecef0fb2fb5c9e96a1d387265a920`, `release_linux.py verify`
  devuelve `verified=true`, `pip check` no reporta requisitos rotos, el alias
  estable reporta 0.10.0 y se conservan sólo `current` y el rollback inmediato
  `0.10.0-e93fa5b4ac98-cp314-linux-x86_64`.
- El smoke público sobre fixture aislado confirmó replay equivalente de scan y
  verify, `source_heads=2`, `items_verified=1`, cero `file_actions` y corpus
  byte-identical; el alias estable se usó sólo para version/doctor porque fija
  deliberadamente las rutas canónicas.
- El test Windows/NTFS permanece fuera del alcance Linux-only; no se ejecutó
  GitHub Actions ni se procesó el corpus real.

## Gates posteriores (0.11.x)

1. Mantener fuera de 0.10.x `apply → verify → reconcile`, KIO real,
   autenticación de principal MCP, recovery de restore y endurecimiento general
   de fences SQLite.
2. Para 0.11.x, consumir el grant de heads, revalidar identidad física junto a
   la frontera de efecto y cerrar/reconciliar cada intento sin fallback
   destructivo.
