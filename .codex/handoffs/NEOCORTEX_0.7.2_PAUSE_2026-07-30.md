# NeoCortex — handoff operativo vigente

> Actualizado: 2026-08-26 CST. El basename es histórico y permanece estable.
> `~/.codex/PENDIENTES.md` conserva el compromiso operativo; este archivo
> describe sólo la frontera técnica de reanudación.

## Objetivo activo

Reorganizar la topología productiva en el namespace único `neocortex`, con
responsabilidades explícitas y sin raíces históricas numeradas. El resultado
final elimina `_01_Enumeracion`, `_02_Deduplicacion`, `_03_Progreso`,
`_04_Nucleo_Operativo`, `_05_Interfaz` y `Orquestador.py` después de migrar
cada consumidor, cerrar cada alias y demostrar uso cero.

## Estado vivo al reanudar

- El candidato congelado conserva como padre directo
  `6770ba5e194d46dede86e456fac64cbcac1b1ce1`; al reanudar, confirmar `HEAD`,
  ese padre y árbol limpio. `main` sigue 25 commits delante de `origin/main`;
  no se ha hecho push.
- `_01_Enumeracion`, `_02_Deduplicacion`, `_03_Progreso`, `_05_Interfaz` y
  `Orquestador.py` ya están ausentes. `neocortex` contiene las familias
  canónicas `enumeration`, `deduplication`, `progress`, `interface`, `runtime`
  y `sdk`; `_04_Nucleo_Operativo` sigue pendiente de migración por cohortes.
- La última aceptación de `4aa0f03…` llegó a `trusted_deep_publication`, donde
  falló con `OperationalError: no such savepoint: external_provider_publication`.
  El candidato `2a0e3ac…` incorpora la corrección que preserva el error SQLite
  primario cuando SQLite revierte la transacción completa, más su regresión en
  `tests/test_external_provider_schema_v4.py`. La prueba focal aprobó 32/32 el
  2026-08-25. La primera validación del lote se abstuvo en el preflight
  pip-audit aunque el snapshot publicado era exacto y vigente. El candidato
  añade `exact_lookup` a esa abstención y la regresión correspondiente, para
  conservar las identidades que expliquen cualquier discrepancia futura; no
  existe aún receipt canónico del lote.
- Preflight host del 2026-08-25: cero unidades/procesos de gate NeoCortex,
  `code.sqlite3-wal=0`, `framework.sqlite3-wal=0`, 9,980,633,088 bytes de
  memoria disponibles, 49,421,869,056 bytes libres y PSI `some/full avg10=0`.
  `pdf.sqlite3-wal=189,552` pertenece al owner documental `pdf`; el status
  público registra el run 78 como `completed` con
  `recovery_required_actions=0`. No abrir ni modificar ese owner durante el
  gate Code, que cerca únicamente `code.sqlite3`.
- La release instalada sigue siendo la base aceptada `6770ba5…` (`Neocortex
  0.9.0`). El verificador del source actual rechaza su manifiesto porque la
  política vigente cambió el bootstrap de pip de 26.1.2 a 26.2.1. Es un
  rechazo de política fail-closed, no corrupción del artefacto histórico ni
  una corrección de código: la próxima release deberá construirse desde el
  candidato congelado con los pins vigentes.
- La aceptación de `c1d8726…` completó Coverage trusted-deep 24/24 y la
  publicación incremental terminó exit 0 con 899 cache hits, pero el watchdog
  abortó al leer el verdict por `memory_pressure_full_abort_threshold`
  (`min_available=6,224,046,128`, PSI `some/full avg10=5.44`). No existe
  receipt; Code/Framework WAL quedaron vacíos. Transcript y hash viven en
  `cut-10-final-candidate/code-validate-c1d8726-final.log`.
- Para evitar que el owner Code vuelva a acumular runs y proyecciones externas
  sin límite, el working tree incorpora `code-owner-generational-retention-v1`.
  La política conserva dos completados, cuatro incidentes, receipts y fuentes
  de replay, impone un techo de 64 runs terminales y elimina sólo un run
  elegible por frontera dentro de la transacción del owner. Las versiones de
  archivos, símbolos, FTS, grafo y compactación física quedan fuera; las
  regresiones focales de retención, storage, estado y arquitectura están
  verdes, pero aún falta congelar el candidato y obtener el receipt canónico.

## Próximos pasos, en orden

1. Congelar el working tree de retención después de revisar el diff y el
   resultado focal; no tocar el corpus ni ejecutar poda manual sobre el owner
   vivo desde este handoff.
2. Esperar un preflight host con margen real sobre la reserva de KDE/Chrome y
   PSI estable; no iniciar el gate si la memoria disponible está demasiado
   cerca de la reserva. No tocar release ni corpus.
3. Mantener el snapshot trusted-static que siga siendo exacto para el candidato;
   no repetir el producer salvo que el gate lo declare obsoleto.
4. Con el candidato inmutable, recursos holgados y sin writers, ejecutar una
   sola aceptación:
   `Neocortex code validate --baseline HEAD^`. No relanzarla si falla: guardar
   el transcript y diagnosticar sólo el gate señalado.
5. Con receipt `passed`, instalar desde ese SHA, verificar `current`, manifest
   y launcher, y ejecutar el E2E público sin `--apply`. Repetir sólo si el diff
   final cruza caché, reanudación, schema o pipeline.
6. Continuar `_04_Nucleo_Operativo` por una familia vertical con productor,
   estado, lector y comando visibles. El push permanece reservado para el
   cierre integral único, con `main=origin/main=current.source_sha` y árbol
   limpio.

## Límites operativos

- Linux/Kubuntu es la única plataforma activa. GitHub Actions y Windows no son
  gates ni superficies de trabajo.
- Observa gates sólo por systemd/transcript/progreso. No abras SQLite cercadas
  con lectores ordinarios ni borres WAL/SHM para forzar una corrida.
- La aceptación canónica consume el snapshot de supply ya publicado; pip-audit
  se renueva únicamente por su productor explícito y con el permiso permanente
  de Víctor, sin `--fix`.
- Toda release y E2E final se ejecutan desde el artefacto instalado, sin
  `PYTHONPATH` ni imports desde este checkout.
