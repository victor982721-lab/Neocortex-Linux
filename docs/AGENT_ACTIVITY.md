# Actividades externas registradas

`neocortex.api.agent_activity.AgentActivity` es la interfaz local para que una
actividad externa (por ejemplo, un agente de prueba) use un workspace privado
sin crear un registro paralelo. El owner predeterminado es
`neocortex-framework`, el mismo owner que acepta `maintenance --scope
owned-temp`; no se deben cambiar los owner checks para admitir otro proceso.

## Recorrido Python

```python
from pathlib import Path
from neocortex.api.agent_activity import AgentActivity

state = Path("/ruta/estado-de-prueba")       # absoluto y privado
activity = AgentActivity.prepare(state, "actividad-20260916")
result = activity.run(["python3", "producer.py"])  # argv, sin shell
published = activity.publish(
    activity.path / "result.json",
    Path("/ruta/entregables/result.json"),
)
activity.close((activity.path / "result.json",))
```

El destino de `publish` se crea con no-replace y se registra como un artefacto
`canonical` no desechable. Un archivo preexistente nunca se adopta ni se
sobrescribe. Una repetición de la misma publicación valida destino, digest y
registro, y devuelve `already_published`.

## Recuperación

La recuperación no depende del objeto Python original:

```python
activity = AgentActivity.resume(state, "actividad-20260916")
activity.reconcile("resume")
activity.reconcile("publish")  # sólo si quedó una publicación pendiente
activity.reconcile("complete", result_paths=(activity.path / "result.json",))
activity.reconcile("retire")
```

Una interrupción conserva el scratch. La edad, la ausencia de PID o un TTL no
autorizan por sí solos el retiro. `close` sella miembros, identidades y bytes;
un cambio posterior bloquea `retire`. El retiro físico sigue siendo propiedad
de `ScratchManager`/`ArtifactRegistry` y el mantenimiento aislado puede
reproducirlo con:

```text
Neocortex maintenance --scope owned-temp --apply --maintenance-json \
  --state-directory /ruta/estado-de-prueba
```

El comando debe ejecutarse sólo sobre un estado sintético cuyo owner sea el
canónico. `hygiene` conserva su contrato preview/read-only; no es un alias de
este efecto.

## CLI

La superficie CLI instalada es:

```text
Neocortex agent-activity --agent-action prepare --agent-activity-id ID \
  --state-directory /ruta/estado --agent-json
Neocortex agent-activity --agent-action run --agent-activity-id ID \
  --agent-json --agent-command /ruta/absoluta/python producer.py
Neocortex agent-activity --agent-action publish --agent-activity-id ID \
  --agent-source /ruta/estado/scratch/owned-temp/workspace-ID/result.json \
  --agent-destination /ruta/entregables/result.json --agent-json
Neocortex agent-activity --agent-action complete --agent-activity-id ID \
  --agent-result /ruta/estado/scratch/owned-temp/workspace-ID/result.json \
  --agent-json
```

La integración del parser debe conservar la forma de argv y despachar antes
del grafo de rutas. No redirige `HOME`/`CODEX_HOME`, no adopta cachés,
credenciales, modelos ni sesiones externas, y no amplía `--all` al HOME.
