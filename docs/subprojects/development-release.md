# Desarrollo y distribución

## Flujo de cambio

Define el resultado útil pedido, lee el estado vivo y preserva cambios ajenos.
Traza productor, owner, publicación, lector e interfaz; reproduce el defecto o
captura una línea base read-only, corrige el mínimo flujo existente y añade una
regresión proporcional. No construyas otra tubería, auditoría ni backlog por
una consulta. Un archivo tiene un escritor; la raíz integra Git y registros.

Trabaja directamente en main bajo la autorización permanente: sin ramas/PR,
reescritura o force push; fast-forward para trabajo previo verificado cuando sea
posible. Publicación exige SHA remoto comprobado, `HEAD == main == origin/main`,
árbol limpio y evidencia proporcional, no sólo un commit local.

## Herramientas individuales

Pytest prueba comportamiento, Ruff errores estáticos, Mypy/Pyright tipos y
Semgrep invariantes específicas. Selecciona las comprobaciones por el diff y el
contrato; un focal verde no prueba aceptación integral. No crees un comando
agregador, proveedor/receipt de autoanálisis ni GitHub Actions.

Runtime instalado y herramientas de calidad son entornos separados. Resuelve
venvs vigentes desde evidencia verificada, no desde fechas incrustadas en reglas.
`pyproject.toml` gobierna piso de lenguaje/plataforma y configuración; el intérprete
del venv no obliga a cambiar ese piso. No introduzcas `ignore-missing-imports` ni
silenciamientos globales. Registra excepciones concretas sin reescribir fixtures
selladas, wheels/licencias upstream o entradas binarias para satisfacer estilo.

Instala dependencias autorizadas sólo en venv/caché aislados, primero desde
`dev-resources/offline/` y wheelhouse autenticado. Sin paquetes globales ni
bootstrap implícito por red/PyPI. `pip-audit` o una consulta advisory remota sólo
proceden ante solicitud expresa y con nombres/versiones mínimos, sin código,
corpus, secretos, `--fix` ni mutación de paquetes.

## Release, sólo cuando esté en alcance

Usa `tools/release_linux.py`, no otro instalador personal paralelo. La autorización
permanente cubre construir, instalar/promover y verificar una release incluida
en la tarea, no añade modelos, privacidad, mutación del corpus ni otros borrados.
La instalación ordinaria desde extracción sin Git usa empaquetado estándar y
wheels originales offline sin promover current; consulta [Linux](../LINUX_KUBUNTU.md).

Comprueba manifest, launcher y artefacto del SHA final; prueba interfaz pública
con HOME/XDG/owners aislados y sin `PYTHONPATH`, `PYTHONHOME` o imports del checkout.
`NEOCORTEX_TEST_PYTHON` identifica el intérprete instalado para sus pruebas.
Repite entrada cuando corresponda replay/caché/idempotencia, no por rutina.
Después de verificar conserva current y el rollback inmediato; nunca borres una
release en uso ni retires otra evidencia o material por una retención inferida.

Un commit posterior sólo documental no exige repetir validación ejecutable si
se demuestra diff exacto sin código, tests, configuración, build, contratos,
artefactos o entradas efectivas de validación. Conserva ambos SHA y evidencia
previa fuera del runtime; no hagas amend. Si la release debe identificar el SHA
final, constrúyela/verifícala y haz el smoke instalado pertinente; si falta prueba
de equivalencia, la validación aplicable permanece pendiente.

## Estado y cierre

El responsable de integración actualiza PENDIENTES/HISTORIAL ante transiciones
verificadas, con rutas/hashes y próximo paso, sin logs brutos ni éxitos inferidos.
Conserva el goal existente; creación y estados siguen el contrato superior de
la herramienta. Un timeout aislado no retira consentimiento ni justifica bloqueo.
La documentación conserva ownership: README entrada, Architecture presente,
Roadmap futuro, Operations procedimiento, Changelog historia y CURRENT puntero.
