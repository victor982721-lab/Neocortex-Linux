# Runtime y persistencia

## Ownership

`neocortex/runtime` coordina configuración, recursos, procesos y lifecycle;
`neocortex/persistence` define el kernel de lectura/escritura, publicación y
recuperación de owners. Los schemas específicos siguen con su owner, no se
centralizan por esta organización documental. Consulta [Architecture](../ARCHITECTURE.md#persistencia),
[Persistence](../PERSISTENCE.md) y [Operations](../OPERATIONS.md#recursos-y-progreso).

## Fronteras

- En Linux la identidad usa `st_dev`/`st_ino`; cuando no existe birthtime real
  se conserva `birthtime_ns=-1`, nunca se convierte `ctime` en nacimiento.
- Una generación parcial no es vigente. Schema futuro, corrupción, identidad
  incierta o cambio concurrente abortan la frontera que no puede demostrar seguridad.
- `mode=ro` ordinario puede alterar WAL/SHM. Durante una corrida cercada observa
  sólo stream, transcript, proceso/cgroup del host y archivos de progreso previstos;
  una API sólo procede si garantiza compatibilidad con esos owners activos.
- Después del estado terminal, selecciona `SQLiteReadSession` o snapshot según
  su contrato y presupuesto. No borres sidecars ni relajes fences para validar
  una corrida interferida; conserva evidencia y repite sólo la corrida afectada.
- Un `ps` en sandbox no demuestra quiescencia del host. Mientras unidad, wrapper
  o cgroup estén activos no modifiques SHA, checkout ni entradas cercadas.
- Concurrencia de agentes, procesos y writers son límites distintos. Un único
  escritor por owner/archivo; CPU, memoria y temporales se dimensionan con
  cgroups, afinidad y presión observables, no sólo `os.cpu_count()`.
- Procesar contenido real requiere alcance, preflight y límites. Un piloto usa
  fixtures o 20–50 elementos autorizados y 10–15 minutos como máximo; un límite
  de PDF no limita el inventario ni garantiza un deadline global de `--all`.
  No escales sin un límite duro demostrado; si falta, impleméntalo primero.

## Validación proporcional

Selecciona pruebas existentes de `test_cgroup_resources`, `test_global_resources`,
`test_run_budget`, `test_run_budget_terminal_accounting`, `test_sqlite_immutable`
o `test_sqlite_*` según el cambio. Usa HOME/XDG y owners de fixtures aislados,
nunca SQLite productivas para comprobar una edición de desarrollo. Si inicias
una unidad con logs append, crea y verifica primero su directorio de logs.
