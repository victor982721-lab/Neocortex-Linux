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

## Cobertura por ronda

La prioridad es corrección y cobertura útil por iteración, no llenar el cupo ni
prometer menor tiempo por sumar agentes. Una consulta simple o un frente serial
no exige una matriz ni delegación. En cambios complejos, la raíz descompone
dependencias y delega en paralelo todos los frentes realmente independientes,
mientras avanza trabajo distinto en la ruta crítica.

### Una tarea principal, capacidad comprobada

- Usa una sola tarea principal con subagentes internos, no varias tareas del
  usuario como coordinadores implícitos. Un coordinador interno se justifica
  sólo si sus hojas aportan cobertura distinta y reducen carga de integración.
- Comprueba el namespace/esquema nativo del actor antes de exigir descendientes.
  Si no lo tiene, la raíz admite directamente sus frentes pendientes, dentro de
  la misma ronda. Un fallo local no demuestra imposibilidad global; no inventes
  aliases, claves de profundidad, proveedores ni tareas visibles para sortearlo.
  Habilitar un nivel anidado requiere observar una creación real y su entrega,
  no sólo texto de configuración. Evita más niveles sin beneficio demostrado.
- La configuración local conserva V2 con límite 16 y espera por defecto 120 s.
  Verifica cómo cuenta el runtime los actores antes de planear al límite; no
  traslades automáticamente semántica de otra versión/backend. Es un techo,
  no un objetivo: no equivale a 16 validadores/OCR simultáneos. Ajusta admisión
  a memoria, I/O y cuota; ante fallos homogéneos reduce concurrencia y prueba
  una canaria. Si faltan slots, continúa en oleadas sin recortar la ronda.
- El modelo de la raíz sigue la selección del usuario. Los workers acotados
  tienen default `gpt-5.6-luna`/`max`; los coordinadores anidados y la revisión
  crítica usan explícitamente `gpt-6-astra`/`max`, no el default de las hojas,
  salvo selección distinta expresa del usuario. Verifica catálogo y herramientas
  del actor antes de delegarle coordinación; no atribuyas capacidad por
  el título del actor. Una instrucción no cambia modelo/esfuerzo de un actor
  reanudado: comprueba metadata y, si no coincide, crea un reemplazo sin
  escritores simultáneos. No reduzcas MAX tácitamente para acelerar.

### Matriz y ownership antes del despacho

Identifica todas las superficies afectadas, y sólo ésas: productor, owner/persistencia,
publicación, consumidores e interfaz pública, compatibilidad y lifecycle.
Asigna una celda estable a cada obligación/riesgo material, no a cada archivo
o agente. Una misma prueba puede referenciar varias celdas sin contarse como
evidencia independiente. Usa una tabla breve de trabajo, fuera del producto:

| Celda / contrato o riesgo | Interfaces y consumidores | Actor / escritor exclusivo | POS y adversarial aplicables | Evidencia / ID del revisor | Estado / siguiente gate |
|---|---|---|---|---|---|
| ID estable y alcance | Productor → publicación → consumidor | Rutas lógicas asignadas | Conducta esperada y fallo/abstención | Fuente, comando, resultado y hash/SHA | Pendiente, en curso, verificada, fallida o bloqueada |

Justifica `N/A`; falta de tiempo, herramienta, slots o autorización no es `N/A`.
Congela el denominador al planear la ronda y amplíalo explícitamente ante un
consumidor/riesgo descubierto; no elimines celdas fallidas para mejorar la cifra.
Los briefs incluyen IDs, objetivo, entradas mínimas, rutas de escritura,
dependencias, POS/adversarial, prohibiciones y entrega esperada. Usa
`fork_turns="none"` con un brief autosuficiente; comparte historia acotada sólo
si es necesaria. No vuelvas a copiar transcripciones completas entre actores.

Ownership se aplica al archivo **lógico**, también entre copias temporales.
Los cambios del mismo archivo se serializan; leer o proponer un diff no concede
escritura. No hay aislamiento por actor garantizado sólo por el prompt: conserva
el sandbox real y revisa el diff. La raíz es el único escritor de Git,
integración, publicación y SSOT; un coordinador no cierra el objetivo global.

### Aceptación independiente y evidencia compacta

- El autor aporta regresión focal; un revisor distinto deriva la aceptación de
  la especificación/interfaz pública, no sólo de los mocks o supuestos del autor.
  Fronteras compartidas/críticas requieren esa revisión antes de integrar.
- Prueba el caso útil positivo y los fallos pertinentes. Para budgets/I/O,
  consulta deadline/cancelación **durante** la operación, no sólo antes; prueba
  error tipado, retry/cursor/replay e idempotencia cuando sean parte del contrato.
  Un deadline agotado no puede anunciar `complete`. No fuerces estos casos a
  una edición documental que no los afecta: conserva un `N/A` razonado.
- El handoff compacto conserva IDs de actor y revisor, modelo/esfuerzo observados, IDs cubiertos,
  archivos/SHA, prueba y resultado, hallazgos, límites y próximo gate. Enlaza
  logs grandes y lee sólo la evidencia/diff necesaria para aceptar; `OK`, un
  archivo presente o tests focales verdes no sustituyen aceptación integral.
- La raíz arbitra hallazgos contra el contrato y su evidencia: distingue defecto
  demostrado, riesgo y mejora opcional. No convierte endurecimientos sugeridos
  o un `FAIL` sin contraejemplo válido en gates automáticos ni en trabajo nuevo.
- Espera sólo resultados de la ruta crítica con la herramienta nativa y esperas
  largas interrumpibles. Para tareas de usuario autorizadas usa cursores cuando
  existan; no apliques parámetros de esa API a Collaboration. `followup_task`
  reactiva un subagente idle, y hay que verificar que arrancó. Un timeout no es
  estado terminal: conserva intento/identidad y decide un reintento acotado
  sólo si es seguro e idempotente. Una orden STOP detiene nuevas acciones y
  descendientes propios; una continuación técnica no autoriza reanudar.
- Acepta cada celda una sola vez; concilia respuestas tardías/duplicadas por ID
  y SHA. Una oleada terminada no cierra la ronda. Reporta verificadas/aplicables,
  fallidas, pendientes, bloqueadas y `N/A` por separado. Un gate externo permite
  una entrega parcial explícita, no llamar completa la ronda.

### Integración y medida

Integra las oleadas admitidas, congela SHA/entradas y ejecuta en la raíz la
validación final proporcional con herramientas individuales. No dupliques el
gate pesado por actor ni edites entradas durante la corrida. Si cambia código,
configuración efectiva o fixtures después del gate, revalida lo afectado; no
disfraces ese cambio de docs-only. Publicación e instalación conservan sus
verificaciones separadas en este documento y AGENTS.

Mide cobertura verificada de la misma ronda y registra retrabajo, defectos
escapados, tiempo de coordinación/espera y bytes de contexto sólo cuando ayuden
a comparar rutas equivalentes. Más actores, más tokens, más tests o menos
elapsed aislado no prueban mejor calidad. Configurar este procedimiento no
acredita corregir defectos del producto ni mejorar velocidad sin medición.

Los cambios de Codex se prueban con su consumidor nativo en proceso nuevo
(configuración efectiva/origen e instrucciones cargadas), sin credenciales ni
red si basta lectura. Una prueba de comportamiento usa trabajo acotado y los
actores/herramientas realmente expuestos. No recargues tareas ajenas ni fuerces
reiniciar Desktop; las sesiones existentes pueden conservar configuración vieja.
Estos cambios de coordinación no requieren una release del producto por sí solos.

## MCP de desarrollo bajo demanda

La integración MCP de Codex con NeoCortex permanece deshabilitada por defecto;
actívala sólo dentro de un alcance concreto que necesite MCP, sin ampliar las
herramientas permitidas ni la autoridad sobre corpus. Una tarea LLM, un subagente
y una conexión MCP son unidades distintas: su número no acredita ownership.

Cambiar el default no autoriza recargas globales ni cerrar conexiones activas.
Los transportes previos pueden conservar sus llamadas aunque el inventario
refleje el nuevo OFF: un catálogo vacío no prueba que el proceso haya terminado.
Capacidades nuevas se incorporan mediante una conexión nueva en un punto seguro,
no forzando un reload de las anteriores. Desconecta únicamente clientes
innecesarios identificados, sin peticiones en vuelo y al terminar su uso;
verifica identidad y salida, sin killall ni cierre por estar idle. Si siguen
siendo necesarios, consérvalos.

Este ajuste operativo no exige promover una release. En una promoción incluida
en alcance, una release seleccionada para retirar que siga en uso conserva su
gate de bloqueo; no fuerces GC ni alteres la retención para sortearlo. Ser una
release anterior no exige detenerla si pertenece al par current/rollback retenido.

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

## Validar una fuente temporal

Usa esta receta sólo cuando la comprobación requiera una copia privada de fuente;
no es una barrera nueva para toda tarea ni una validación de release instalada.
`SRC` es la raíz completa de esa copia, con procedencia del mismo SHA/diff, no el
checkout vivo ni tests de otro árbol. La receta crea un `RUN` nuevo y privado en
`/tmp`; ambas raíces deben ser escribibles según el permiso efectivo, sin abrir
el HOME real.

Resuelve `QUALITY_PY` y `STATIC_PY` a los **binarios absolutos dentro de sus venvs**
verificados; no uses `readlink -f` sobre Python, porque puede seleccionar el
intérprete base. `TOOLING_ROOT` es el padre absoluto que contiene el venv nombrado
en `[tool.pyright].venv`. Confirma módulos/versiones y Node local antes de analizar;
si faltan, no permitas bootstrap remoto implícito. No muevas las raíces canónicas.

```bash
set -euo pipefail
: "${SRC:?raíz privada}" "${QUALITY_PY:?Python de calidad}" \
  "${STATIC_PY:?Python estático}" "${TOOLING_ROOT:?padre del venv de Pyright}"
command -v node >/dev/null  # no bootstrap de Node por red
RUN=$(mktemp -d /tmp/neocortex-validation-XXXXXX)
export SRC RUN QUALITY_PY STATIC_PY TOOLING_ROOT
SEMGREP_BIN="${STATIC_PY%/*}/semgrep"
test -x "$SEMGREP_BIN" || exit 1
umask 077
export HOME="$RUN/home" XDG_CONFIG_HOME="$RUN/config" XDG_CACHE_HOME="$RUN/cache"
export XDG_DATA_HOME="$RUN/data" XDG_STATE_HOME="$RUN/state"
export XDG_DOCUMENTS_DIR="$RUN/documents" XDG_RUNTIME_DIR="$RUN/runtime"
export XDG_CONFIG_DIRS="$RUN/config" XDG_DATA_DIRS="$RUN/data"
export TMPDIR="$RUN/tmp" TMP="$RUN/tmp" TEMP="$RUN/tmp"
export HF_HOME="$RUN/model-cache/huggingface" TORCH_HOME="$RUN/model-cache/torch"
export HF_HUB_CACHE="$HF_HOME/hub"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1
export DO_NOT_TRACK=1 ORT_DISABLE_TELEMETRY=1 PIP_NO_INDEX=1
export PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
export PYTHONPYCACHEPREFIX="$RUN/pycache" COVERAGE_FILE="$RUN/coverage"
export SEMGREP_SETTINGS_FILE="$RUN/semgrep/settings.yml"
export SEMGREP_LOG_FILE="$RUN/semgrep/semgrep.log"
unset PYTHONPATH PYTHONHOME NEOCORTEX_CORPUS_ROOT NEOCORTEX_TEST_PYTHON
unset HUGGINGFACE_HUB_CACHE TRANSFORMERS_CACHE
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" \
  "$XDG_STATE_HOME" "$XDG_DOCUMENTS_DIR/NeoCortex/Corpus" "$XDG_RUNTIME_DIR" \
  "$TMPDIR" "$HF_HUB_CACHE" "$TORCH_HOME" "$RUN/semgrep"
cd -- "$SRC" || exit 1
```

Los exports no crean un sandbox ni cierran la red: conserva el aislamiento real
autorizado. `XDG_CONFIG_HOME` debe existir antes de Semgrep; sus settings/log
explícitos evitan el fallback a `HOME/.semgrep`. No uses el launcher productivo
para comprobar fuente: puede fijar destinos instalados distintos de estos exports.

### Metadatos y foco antes de una suite completa

Genera metadatos desde **esa fuente privada**; nunca copies `.egg-info`/`.dist-info`
del checkout ni instales otra versión en el venv para satisfacer la identidad.
Con `setuptools==83.0.0`, `setup(script_args=["egg_info"])` crea metadatos propios
en `SRC`, con lista de archivos y locators resolubles. No lo ejecutes en el
checkout activo. Preparar sólo `.dist-info` externo mediante
`prepare_metadata_for_build_wheel` no basta: puede carecer de registros de
archivos y convertir el caso importante en un skip.

```bash
"$QUALITY_PY" - <<'PY'
import importlib.metadata as md
import os
from pathlib import Path
import sys
import tomllib
from setuptools import setup

src = Path(os.environ["SRC"]).resolve()
assert Path.cwd().resolve() == src
assert not src.is_relative_to(Path("/home/winterboss/Neocortex/Repository").resolve())
assert not (src / ".git").exists(), "usa una extracción privada, no un checkout"
cfg = tomllib.loads((src / "pyproject.toml").read_text())
assert cfg["build-system"]["build-backend"] == "setuptools.build_meta"
assert f"setuptools=={md.version('setuptools')}" in cfg["build-system"]["requires"]
assert not list(src.glob("*.egg-info")) and not list(src.glob("*.dist-info")), "usa fuente privada sin metadatos heredados"
setup(script_args=["egg_info"])
import neocortex
assert Path(neocortex.__file__).resolve().parent == src / "neocortex"
dist = md.distribution("neocortex-framework")
metadata_dir = Path(dist._path).resolve()  # egg-info puede devolver una ruta relativa
assert metadata_dir.parent == src and metadata_dir.name.endswith(".egg-info")
assert Path(dist.locate_file("")).resolve() == src
assert dist.version == neocortex.__version__
files = {str(item): item for item in dist.files or ()}
assert files, "los metadatos deben enumerar archivos reales"
for relative in ("neocortex/__init__.py", "neocortex/capabilities/formats/text/text_route.py"):
    assert relative in files
    located = Path(dist.locate_file(files[relative])).resolve()
    assert located == src / relative and located.is_file()
assert Path(sys.prefix).resolve() == Path(os.environ["QUALITY_PY"]).parent.parent.resolve()
assert (Path(os.environ["TOOLING_ROOT"]) / cfg["tool"]["pyright"]["venv"] / "bin/python").is_file()
print(sys.executable, neocortex.__file__, metadata_dir, dist.version)
PY
```

El preflight modifica sólo `SRC` privado y no instala paquetes. No basta con que
coincida la versión: comprueba archivos de implementación y sus rutas, resolviendo
primero las rutas relativas de metadatos. No uses `PYTHONPATH` para inyectar una
distribución incompleta ni mezcles fuente con metadatos del artefacto instalado.

Desde `SRC`, el foco de esta identidad es
`"$QUALITY_PY" -m pytest --capabilities=all -q tests/test_text_implementation_identity.py`.
Comprueba antes su colección con `--collect-only`, y exige todos sus casos
ejecutados y aprobados, sin skips por metadatos ausentes. Sólo después amplía a la suite
pertinente. Para otro cambio selecciona además su foco, sin borrar errores o
confundir los skips previstos de otra capacidad con aceptación de esta identidad.

Para estática, elige targets afectados, no agregues `neocortex tests tools` por
costumbre. Ejecuta sólo las herramientas pertinentes, cada una por separado:

- Ruff: `"$QUALITY_PY" -m ruff check --config "$SRC/pyproject.toml" <targets>`
- Mypy: `"$QUALITY_PY" -m mypy --config-file "$SRC/pyproject.toml" <targets>`
- Pyright: `"$QUALITY_PY" -m pyright --project "$SRC" --venvpath "$TOOLING_ROOT" <targets>`
- Semgrep: `"$SEMGREP_BIN" --config "$SRC/semgrep/neo-invariants.yml" --metrics=off --disable-version-check <targets>`

Usa el entrypoint del venv de Semgrep: `python -m semgrep` está retirado en la
versión instalada y devuelve error antes de analizar archivos.

Comprueba root/config, intérprete/venv, reglas y cobertura de archivos mayor que
cero sobre el foco antes de ampliar el análisis. Un rc=0 con cero archivos no
valida nada; un error de tipos tampoco se vuelve falso porque otra herramienta
falló por entorno. Corrige la invocación mínima y conserva los errores restantes,
sin silenciamientos ni nuevas barreras integrales.

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
