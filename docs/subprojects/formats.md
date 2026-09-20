# Ingesta de formatos

## Ownership

`neocortex/capabilities` mantiene el registro y `formats` sus rutas. Cada ruta
declara inputs, límites, progreso, owner y resultado; su riqueza de localizadores
es cobertura explícita, no evidencia inventada. Consulta [Architecture](../ARCHITECTURE.md#zip-intake-físico-y-rutas-de-contenido)
y [Operations](../OPERATIONS.md#modelos-y-herramientas-externas).

## Fronteras

- No ejecutes contenido del corpus. Preserva originales, procedencia y la identidad
  del contenedor y sus miembros; formatos no soportados se declaran, no se simulan.
- Trabaja sobre la ruta solicitada; `--all` no es el smoke por defecto y también
  escribe estado aunque no haya `--apply`. Fija raíz, cantidad y deadline real.
- Distingue dependencia Python, ejecutable/backend, pesos locales y procesamiento
  funcional. Presencia de paquetes/modelos no prueba cobertura ni éxito de una ruta.
- Dependencias autorizadas se preparan en venv/caché aislados desde artefactos
  canónicos y wheelhouse autenticado, nunca globalmente ni en otra tarea.
  PyPI, proveedor remoto y preparación/descarga de modelos no son pasos implícitos.
- Si el sandbox no ve un dispositivo o biblioteca, verifica la frontera mediante
  la superficie host autorizada antes de declarar ausencia, sin tocar owners activos.

## Validación proporcional

Usa un fixture pequeño del formato y una regresión focal. Para audio comprueba
carga del modelo local, procesamiento, salida y error; no basta instalar paquetes.
Si se entrega una capacidad instalada, prueba su launcher final con HOME/XDG y
estado aislados, sin `PYTHONPATH` ni imports del checkout. Repite la entrada sólo
cuando corresponda replay/caché/idempotencia y conserva receipts fuera del runtime.
`NEOCORTEX_TEST_PYTHON` pertenece a pruebas instaladas y `--prepare-models` exige
autorización explícita; registra versión, fuente, revisión y hash usados.
