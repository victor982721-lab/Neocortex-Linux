# Inventario técnico de licencias y componentes de terceros

> **DOCUMENTO HISTÓRICO — NO NORMATIVO.** Este inventario describe
> resoluciones y releases anteriores, incluida infraestructura de QA que ya no
> forma parte del runtime de Code, así como la dependencia NudeNet que ya fue
> retirada de Image. Las dependencias vigentes se definen en
> `pyproject.toml` y `constraints.txt`.

> Snapshot de metadata técnica observada. No concede permisos, no elige una licencia para NeoCortex y no constituye asesoría jurídica.

## Alcance y fecha

- Fecha: **2026-07-25**.
- Entorno: **Python global revalidado (17/17 directas coinciden con constraints); artefactos PRE-REPORT 0.6.0, venvs aislados `--no-deps`, `--system-site-packages` y resolución hermética sin red**.
- Intérprete: `C:\Program Files\Python313\python.exe`.
- Python: `3.13.14 (tags/v3.13.14:fd17997, Jun 10 2026, 13:03:48) [MSC v.1944 64 bit (AMD64)]`.
- Distribuciones alcanzables: **64**; runtime base/semantic: **51**; sólo desarrollo: **13**.

## Metodología reproducible

1. Leer `pyproject.toml` y las raíces base, `semantic` y `dev`.
2. Recorrer `Requires-Dist` con los marcadores del entorno.
3. Capturar `License-Expression`, `License`, classifiers, URLs y evidencias bajo directorios de licencia o nombres LICENSE/LICENCE/COPYING/NOTICE visibles mediante importlib.metadata.
4. Inspeccionar wheel y sdist para detectar distribuciones ajenas y avisos.
5. Marcar contradicciones para revisión humana, sin reinterpretarlas.

El recolector usado fue una herramienta acotada del laboratorio de auditoría y no forma parte actualmente del repositorio ni de la interfaz pública. Por ello este documento no promete un comando de producto inexistente; los comandos exactos quedan en el informe técnico fechado.

Las **17/17 dependencias directas** instaladas satisfacen tanto su specifier de `pyproject.toml` como el pin correspondiente de `constraints.txt`. Las transitivas siguen gestionadas por el resolver y este entorno global no sustituye una instalación aislada constrained.

Las versiones siguientes son las instaladas en el entorno indicado. La release
Linux CPython 3.14 las fija ahora mediante `constraints-linux-cp314.lock`; otros
entornos y wheelhouses conservan su propia verificación.

### Addendum del runtime candidato 0.7.2 — Hito 5

El snapshot jurídico histórico de 2026-07-25 no se reescribió. La aceptación de
Hito 5 sí añadió un inventario técnico reproducible del runtime candidato:
104 distribuciones, 101 con alguna metadata de licencia, 38 declaraciones
ambiguas y 3 ausentes. Las 13 dependencias base requeridas estaban instaladas y
compatibles. Se verificaron 296 hashes/tamaños `RECORD` y 5 909 535 bytes sin
faltantes, rutas inseguras ni discrepancias. Estos conteos describen evidencia
de metadata; no resuelven licencias ni sustituyen revisión jurídica.

El wheel candidato de 1 540 150 bytes y 293 miembros aprobó integridad ZIP y
`pip check`; su SHA-256 es
`D0957627056E873FEA87C9D89B0FB874FBB807874D58ACE8938E8246158EFD97`.
El wheel incluye las tres reglas Semgrep locales, pero no incorpora Pyright ni
Node. Cualquier bundle que los incluya debe inventariarlos por separado.

| Componente declarado por la fuente 0.7.2 | Entrega | Alcance operativo | Declaración observada |
|---|---|---|---|
| Ruff `0.15.17` | dependencia Python base, pin | perfiles `protected` y `trusted-static` | MIT |
| Mypy `2.1.0` | dependencia Python base, pin | tipos en `trusted-static` | MIT |
| Grimp `3.15` | dependencia Python base, pin | grafo y contratos | BSD-2-Clause |
| Complexipy `6.2.0` | dependencia Python base, pin | complejidad cognitiva | MIT |
| Coverage `7.14.1` | dependencia Python base, pin | `trusted-deep` | Apache-2.0 |
| Pytest `9.1.0` | dependencia Python base, pin | `trusted-deep` | MIT |
| Vulture `2.16` | dependencia Python base, pin | consenso advisory de uso | MIT |
| Semgrep `1.172.0` | dependencia Python base, pin | tres invariantes locales | LGPL-2.1-or-later |
| Deptry `0.25.1` | dependencia Python base, pin | higiene de dependencias | MIT |
| pip-audit `2.10.1` | dependencia Python base, pin | snapshot de vulnerabilidades | classifier Apache Software License |
| Packaging `26.2` | dependencia Python base, pin | constraints y versiones | Apache-2.0 OR BSD-2-Clause |
| Pyright `1.1.411` | paquete npm aislado junto al runtime | tipos mediante Node | MIT |
| Node `24.18.1` | runtime externo | host de Pyright | inventariar antes de redistribuir |

El proveedor `installed-package-inventory` expone metadata, ambigüedad e
integridad como categorías separadas y sin conclusión jurídica. Debe
regenerarse para cada resolución constrained que se pretenda promover.

### Addendum del runtime candidato 0.7.2 — Hito 6

El candidato Hito 6 se validó con Python 3.13.14 desde el wheel
`$HOME\Neocortex\Laboratory\neocortex-0.7.2-hito6-mutation-history-20260803-rc3\wheelhouse\neocortex_framework-0.7.2-py3-none-any.whl`.
El artefacto tiene 1 580 823 bytes, 297 miembros, SHA-256
`7CA6C693847869DF5D18139F9C2F2D22D7F890991077A06A2A86A96D9B54CA11`,
integridad ZIP y `pip check` limpios. Los 23 módulos de producción comparados
fueron byte-idénticos entre fuente, wheel e instalación.

Cosmic Ray `8.4.6` quedó instalado como dependencia Python del autoanálisis
profundo. Su metadata instalada declara el repositorio
`https://github.com/sixty-north/cosmic-ray`, autoría de Sixty North AS,
classifier MIT y un `licenses/LICENCE.txt` con el texto MIT. La metadata y el
archivo empaquetado son evidencia de procedencia y declaración observada; este
inventario no emite una conclusión jurídica.

| Componente observado en el candidato Hito 6 | Entrega | Alcance operativo | Declaración observada |
|---|---|---|---|
| Cosmic Ray `8.4.6` | dependencia Python base, pin | mutación focal en `trusted-deep` sobre copia staged | MIT; metadata y `licenses/LICENCE.txt` instalados |
| Pyright `1.1.411` | paquete npm aislado junto al runtime | tipos en perfiles trusted | MIT |
| Node `24.18.1` | runtime externo | host de Pyright | inventariar antes de redistribuir |

`installed-package-inventory` se recalcula en cada corrida, sin subprocesos,
para verificar el wheel instalado efectivo; no se reutiliza por caché como si
la resolución del entorno fuese inmutable. Cualquier redistribución debe volver
a generar el inventario completo de esa resolución constrained.

### Addendum del runtime candidato 0.7.2 — Hito 7

Hito 7 no añadió una dependencia de producción. El wheel instalado tiene
1 589 930 bytes, 298 miembros y SHA-256
`3B5687DE15B51EA5A2A22190AF999D11CFC720CED8BA617AD068D08D0B4B9087`;
aprobó integridad ZIP y `pip check`. Los cuatro módulos de la nueva consulta
pública fueron byte-idénticos entre fuente, wheel e instalación.

El inventario efectivo del venv candidato observó 124 distribuciones: 121 con
alguna metadata de licencia, 46 declaraciones ambiguas y 3 ausentes. Las 14
dependencias base aplicables estaban instaladas y compatibles. Se verificaron
301 hashes y tamaños `RECORD`, 6 155 721 bytes, sin faltantes, rutas inseguras ni
discrepancias. Pyright 1.1.411 y Node 24.18.1 permanecen aislados fuera del wheel.
Las acciones oficiales `actions/*@v7` pertenecen a la orquestación CI y no se
redistribuyen dentro del paquete Python.

### Revalidación PRE-REPORT de continuación

La revalidación del **2026-07-25 16:55 -06:00** confirmó sin red ni cambios globales:

- versión fuente y artefactos `0.6.0`; distribución global observada `0.3.0`;
- **17/17** dependencias directas satisfacen sus specifiers y pins; closure base **44**, runtime base+semantic **51**, total con desarrollo **64** y sólo desarrollo **13**;
- `pip check` global limpio, sin convertir el entorno global en evidencia de una instalación hermética;
- sin cambios en los hechos sensibles de FastEmbed, NudeNet, PyMuPDF, la familia Qt, NumPy o `py_rust_stemmers` descritos abajo;
- wheel instalable con `--no-deps`, con versión y ayuda funcionales, pero `pip check` falla por las nueve dependencias base omitidas;
- venv con `--system-site-packages` limpio sólo por heredar el entorno global;
- resolución hermética local bloqueada exactamente por ausencia de `faster-whisper==1.2.1`; el sdist requiere además `setuptools==82.0.1` en el wheelhouse.

La diferencia operativa entre estas modalidades y el procedimiento seguro para completar el wheelhouse se documentan en `OFFLINE_INSTALLATION.md`. Esta revalidación no selecciona licencia ni resuelve las contradicciones de metadata.

## Dependencias runtime y opcionales

| Paquete | Versión | Alcance | Relación | Declaración observada | Origen metadata | Archivos |
|---|---:|---|---|---|---|---:|
| anyio | 4.14.0 | base, semantic | transitiva | MIT | Documentation, https://anyio.readthedocs.io/en/latest/ | 1 |
| av | 18.0.0 | base | transitiva | BSD-3-Clause | Bug Tracker, https://github.com/PyAV-Org/PyAV/issues | 3 |
| certifi | 2026.5.20 | base, semantic | transitiva | MPL-2.0 | https://github.com/certifi/python-certifi | 1 |
| cffi | 2.0.0 | base | transitiva | MIT | Documentation, https://cffi.readthedocs.io/ | 2 |
| charset-normalizer | 3.4.7 | base, semantic | transitiva | MIT | Changelog, https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md | 1 |
| click | 8.4.2 | base, semantic | transitiva | BSD-3-Clause | Changes, https://click.palletsprojects.com/page/changes/ | 1 |
| colorama | 0.4.6 | base, dev, semantic | transitiva | License :: OSI Approved :: BSD License | Homepage, https://github.com/tartley/colorama | 1 |
| cryptography | 49.0.0 | base | transitiva | Apache-2.0 OR BSD-3-Clause | changelog, https://cryptography.io/en/latest/changelog/ | 3 |
| ctranslate2 | 4.8.1 | base | transitiva | MIT | https://opennmt.net | 0 |
| fastembed | 0.8.0 | semantic | semantic | Apache License | Homepage, https://github.com/qdrant/fastembed | 2 |
| faster-whisper | 1.2.1 | base | base | MIT | https://github.com/SYSTRAN/faster-whisper | 1 |
| filelock | 3.29.4 | base, semantic | transitiva | MIT | Documentation, https://py-filelock.readthedocs.io | 1 |
| flatbuffers | 25.12.19 | base, semantic | transitiva | Apache 2.0 | https://google.github.io/flatbuffers/ | 0 |
| fsspec | 2026.6.0 | base, semantic | transitiva | BSD-3-Clause | Changelog, https://filesystem-spec.readthedocs.io/en/latest/changelog.html | 1 |
| h11 | 0.16.0 | base, semantic | transitiva | MIT | https://github.com/python-hyper/h11 | 1 |
| hf-xet | 1.5.2 | base, semantic | transitiva | Apache-2.0 | Documentation, https://huggingface.co/docs/hub/xet/index | 1 |
| httpcore | 1.0.9 | base, semantic | transitiva | BSD-3-Clause | Documentation, https://www.encode.io/httpcore | 1 |
| httpx | 0.28.1 | base, semantic | transitiva | BSD-3-Clause | Changelog, https://github.com/encode/httpx/blob/master/CHANGELOG.md | 1 |
| huggingface_hub | 1.24.0 | base, semantic | transitiva | Apache-2.0 | https://github.com/huggingface/huggingface_hub | 1 |
| idna | 3.18 | base, semantic | transitiva | BSD-3-Clause | Changelog, https://github.com/kjd/idna/blob/master/HISTORY.md | 1 |
| loguru | 0.7.3 | semantic | transitiva | License :: OSI Approved :: MIT License | Changelog, https://github.com/Delgan/loguru/blob/master/CHANGELOG.rst | 0 |
| markdown-it-py | 4.2.0 | base | transitiva | License :: OSI Approved :: MIT License | Documentation, https://markdown-it-py.readthedocs.io | 2 |
| mdurl | 0.1.2 | base | transitiva | License :: OSI Approved :: MIT License | Homepage, https://github.com/executablebooks/mdurl | 1 |
| mmh3 | 5.2.1 | semantic | transitiva | MIT License Copyright (c) 2011-2026 Hajime Senuma Permission is hereby granted, free of charge, t... | Homepage, https://pypi.org/project/mmh3/ | 1 |
| nudenet | 3.4.2 | base | base | MIT | https://github.com/notAI-tech/nudenet | 2 |
| numpy | 2.4.6 | base, semantic | semantic | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | homepage, https://numpy.org | 20 |
| onnxruntime | 1.27.0 | base, semantic | transitiva | MIT License | https://onnxruntime.ai | 1 |
| opencv-python-headless | 5.0.0.93 | base | transitiva | Apache 2.0 | https://github.com/opencv/opencv-python | 4 |
| packaging | 26.2 | base, dev, semantic | transitiva | Apache-2.0 OR BSD-2-Clause | Documentation, https://packaging.pypa.io/ | 5 |
| pdfminer.six | 20260107 | base | base | MIT | Homepage, https://github.com/pdfminer/pdfminer.six | 1 |
| pillow | 12.2.0 | base, semantic | base | MIT-CMU | Release notes, https://pillow.readthedocs.io/en/stable/releasenotes/index.html | 1 |
| protobuf | 7.35.1 | base, semantic | transitiva | 3-Clause BSD License | https://developers.google.com/protocol-buffers/ | 1 |
| py_rust_stemmers | 0.1.8 | semantic | transitiva | sin declaración en metadata | - | 1 |
| pycparser | 3.0 | base | transitiva | BSD-3-Clause | Homepage, https://github.com/eliben/pycparser | 1 |
| Pygments | 2.20.0 | base, dev | transitiva | BSD-2-Clause | Homepage, https://pygments.org | 2 |
| PyMuPDF | 1.27.2.3 | base | base | Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License | Documentation, https://pymupdf.readthedocs.io/ | 1 |
| PySide6 | 6.11.1 | base | base | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | Homepage, https://pyside.org | 1 |
| PySide6_Addons | 6.11.1 | base | transitiva | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | Homepage, https://pyside.org | 1 |
| PySide6_Essentials | 6.11.1 | base | transitiva | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | Homepage, https://pyside.org | 1 |
| pytesseract | 0.3.13 | base | base | Apache License 2.0 | https://github.com/madmaze/pytesseract | 1 |
| PyYAML | 6.0.3 | base, semantic | transitiva | MIT | https://pyyaml.org/ | 1 |
| requests | 2.34.2 | semantic | transitiva | Apache-2.0 | Documentation, https://requests.readthedocs.io | 2 |
| rich | 15.0.0 | base | base | MIT | Documentation, https://rich.readthedocs.io/en/latest/ | 1 |
| setuptools | 82.0.1 | base | transitiva | MIT | Source, https://github.com/pypa/setuptools | 19 |
| shiboken6 | 6.11.1 | base | transitiva | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | Homepage, https://pyside.org | 1 |
| tokenizers | 0.23.1 | base, semantic | transitiva | License :: OSI Approved :: Apache Software License | Homepage, https://github.com/huggingface/tokenizers | 0 |
| tqdm | 4.68.2 | base, semantic | transitiva | MPL-2.0 AND MIT | homepage, https://tqdm.github.io | 1 |
| typing_extensions | 4.15.0 | base, dev, semantic | transitiva | PSF-2.0 | Bug Tracker, https://github.com/python/typing_extensions/issues | 1 |
| urllib3 | 2.7.0 | semantic | transitiva | MIT | Changelog, https://github.com/urllib3/urllib3/blob/main/CHANGES.rst | 1 |
| win32_setctime | 1.2.0 | semantic | transitiva | MIT license | https://github.com/Delgan/win32-setctime | 1 |
| xxhash | 3.8.1 | base | base | BSD-2-Clause | https://github.com/ifduyue/python-xxhash | 1 |

## Herramientas sólo de desarrollo en el snapshot 2026-07-25

Las filas Mypy y Ruff siguientes conservan el alcance que tenían al capturar el
snapshot. La declaración fuente 0.7.2 del addendum superior las mueve a la base;
no deben interpretarse como el cierre vigente.

| Paquete | Versión | Alcance | Relación | Declaración observada | Origen metadata | Archivos |
|---|---:|---|---|---|---|---:|
| ast_serialize | 0.5.0 | dev | transitiva | MIT | Homepage, https://github.com/mypyc/ast_serialize | 1 |
| build | 1.5.0 | dev | dev | MIT | changelog, https://build.pypa.io/en/stable/changelog.html | 1 |
| coverage | 7.14.1 | dev | dev | Apache-2.0 | https://github.com/coveragepy/coveragepy | 1 |
| iniconfig | 2.3.0 | dev | transitiva | MIT | Homepage, https://github.com/pytest-dev/iniconfig | 1 |
| librt | 0.11.0 | dev | transitiva | MIT | Homepage, https://github.com/mypyc/librt | 1 |
| mypy | 2.1.0 | dev | dev | MIT | Homepage, https://www.mypy-lang.org/ | 3 |
| mypy_extensions | 1.1.0 | dev | transitiva | MIT | Homepage, https://github.com/python/mypy_extensions | 1 |
| pathspec | 1.1.1 | dev | transitiva | License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0) | Change Log, https://python-path-specification.readthedocs.io/en/latest/changes.html | 1 |
| pluggy | 1.6.0 | dev | transitiva | MIT | - | 1 |
| pyproject_hooks | 1.2.0 | dev | transitiva | License :: OSI Approved :: MIT License | Changelog, https://pyproject-hooks.readthedocs.io/en/latest/changelog.html | 1 |
| pytest | 9.1.0 | dev | dev | MIT | Changelog, https://docs.pytest.org/en/stable/changelog.html | 1 |
| ruff | 0.15.17 | dev | dev | MIT | https://docs.astral.sh/ruff | 1 |
| vulture | 2.16 | dev | dev | The MIT License (MIT) Copyright (c) 2012-2020 Jendrik Seipp (jendrikseipp@gmail.com) Permission i... | Homepage, https://github.com/jendrikseipp/vulture | 1 |

## Componentes que requieren atención del propietario

- **av 18.0.0** — metadata: `BSD-3-Clause`; archivos detectados: `av-18.0.0.dist-info/licenses/AUTHORS.py`, `av-18.0.0.dist-info/licenses/AUTHORS.rst`, `av-18.0.0.dist-info/licenses/LICENSE.txt`.
- **fastembed 0.8.0** — metadata: `Apache License`; archivos detectados: `fastembed-0.8.0.dist-info/licenses/LICENSE`, `fastembed-0.8.0.dist-info/licenses/NOTICE`.
- **numpy 2.4.6** — metadata: `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`; archivos detectados: `numpy-2.4.6.dist-info/licenses/LICENSE.txt`, `numpy-2.4.6.dist-info/licenses/numpy/_core/include/numpy/libdivide/LICENSE.txt`, `numpy-2.4.6.dist-info/licenses/numpy/_core/src/common/pythoncapi-compat/COPYING`, `numpy-2.4.6.dist-info/licenses/numpy/_core/src/highway/LICENSE`, `numpy-2.4.6.dist-info/licenses/numpy/_core/src/multiarray/dragon4_LICENSE.txt`, `numpy-2.4.6.dist-info/licenses/numpy/_core/src/npysort/x86-simd-sort/LICENSE.md`, `numpy-2.4.6.dist-info/licenses/numpy/_core/src/umath/svml/LICENSE`, `numpy-2.4.6.dist-info/licenses/numpy/fft/pocketfft/LICENSE.md`, `numpy-2.4.6.dist-info/licenses/numpy/linalg/lapack_lite/LICENSE.txt`, `numpy-2.4.6.dist-info/licenses/numpy/ma/LICENSE`, `numpy-2.4.6.dist-info/licenses/numpy/random/LICENSE.md`, `numpy-2.4.6.dist-info/licenses/numpy/random/src/distributions/LICENSE.md`, `numpy-2.4.6.dist-info/licenses/numpy/random/src/mt19937/LICENSE.md`, `numpy-2.4.6.dist-info/licenses/numpy/random/src/pcg64/LICENSE.md`, `numpy-2.4.6.dist-info/licenses/numpy/random/src/philox/LICENSE.md`, `numpy-2.4.6.dist-info/licenses/numpy/random/src/sfc64/LICENSE.md`, `numpy-2.4.6.dist-info/licenses/numpy/random/src/splitmix64/LICENSE.md`, `numpy/_core/include/numpy/random/LICENSE.txt`, `numpy/ma/LICENSE`, `numpy/random/LICENSE.md`.
- **onnxruntime 1.27.0** — metadata: `MIT License`; archivos detectados: `onnxruntime/LICENSE`.
- **opencv-python-headless 5.0.0.93** — metadata: `Apache 2.0`; archivos detectados: `cv2/LICENSE-3RD-PARTY.txt`, `cv2/LICENSE.txt`, `opencv_python_headless-5.0.0.93.dist-info/LICENSE-3RD-PARTY.txt`, `opencv_python_headless-5.0.0.93.dist-info/LICENSE.txt`.
- **py_rust_stemmers 0.1.8** — metadata: `sin declaración en metadata`; archivos detectados: `py_rust_stemmers-0.1.8.dist-info/licenses/LICENSE`.
- **PyMuPDF 1.27.2.3** — metadata: `Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License`; archivos detectados: `pymupdf-1.27.2.3.dist-info/COPYING`.
- **PySide6 6.11.1** — metadata: `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`; archivos detectados: `pyside6-6.11.1.dist-info/licenses/LicenseRef-Qt-Commercial.txt`.
- **PySide6_Addons 6.11.1** — metadata: `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`; archivos detectados: `pyside6_addons-6.11.1.dist-info/licenses/LicenseRef-Qt-Commercial.txt`.
- **PySide6_Essentials 6.11.1** — metadata: `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`; archivos detectados: `pyside6_essentials-6.11.1.dist-info/licenses/LicenseRef-Qt-Commercial.txt`.
- **shiboken6 6.11.1** — metadata: `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`; archivos detectados: `shiboken6-6.11.1.dist-info/licenses/LicenseRef-Qt-Commercial.txt`.

### Inconsistencias o metadata incompleta

- Sin declaración en expresión/campo/classifier: **1** — `py_rust_stemmers 0.1.8`.
- Sin archivo LICENSE/COPYING/NOTICE visible mediante importlib.metadata: **4** — `ctranslate2 4.8.1`, `flatbuffers 25.12.19`, `loguru 0.7.3`, `tokenizers 0.23.1`. Su ausencia en metadata no prueba ausencia de licencia upstream.
- `fastembed` declara Apache en el campo libre y conserva LICENSE/NOTICE, pero publica el classifier Other/Proprietary.
- El NOTICE de `fastembed 0.8.0` cataloga tres modelos Jina bajo CC-BY-NC-4.0 y `vidore/colpali-v1.3` bajo términos Gemma. No se descargaron ni redistribuyeron modelos en esta auditoría; cada modelo efectivamente seleccionado necesita revisión, versión y procedencia propias.
- `nudenet 3.4.2` declara `License: MIT` y classifier MIT, pero sus dos archivos declarados `LICENSE` y `LICENSE.md` comienzan con GNU AGPL-3.0. La distribución incluye además `nudenet/320n.onnx` (12150158 bytes). Son hechos contradictorios que requieren revisión humana; este inventario no determina qué licencia gobierna el código o el modelo.
- `PyMuPDF` declara una alternativa AGPL-3.0/comercial.
- La familia `PySide6/Qt` declara alternativas LGPL/GPL en metadata. Cada una de las cuatro distribuciones observadas expone únicamente `licenses/LicenseRef-Qt-Commercial.txt`; no se observaron textos LGPL/GPL entre los archivos declarados por esos wheels.
- `numpy` publica una expresión compuesta y múltiples textos de componentes incorporados; no debe reducirse a una etiqueta.
- No se descargaron modelos adicionales durante esta auditoría. El modelo NudeNet descrito arriba ya forma parte de la dependencia instalada; todo modelo seleccionado requiere inventario propio de licencia, origen, versión y firma antes de redistribuirse.

## Payload nativo observado en la instalación global

Los conteos siguientes proceden de `importlib.metadata`; no se suman como tamaño instalado total porque distribuciones de namespace pueden declarar rutas relacionadas. Los binarios pertenecen a las dependencias instaladas, no al wheel de NeoCortex.

| Distribución | Versión | Binarios del paquete | Bytes del paquete | Launchers del instalador |
|---|---:|---:|---:|---:|
| av | 18.0.0 | 73 | 68566016 | 1 |
| ctranslate2 | 4.8.1 | 4 | 61890976 | 7 |
| numpy | 2.4.6 | 42 | 28421690 | 2 |
| onnxruntime | 1.27.0 | 3 | 35319248 | 1 |
| opencv-python-headless | 5.0.0.93 | 2 | 116724224 | 0 |
| PyMuPDF | 1.27.2.3 | 5 | 41974838 | 1 |
| PySide6_Addons | 6.11.1 | 181 | 296051352 | 0 |
| PySide6_Essentials | 6.11.1 | 238 | 166438280 | 23 |
| shiboken6 | 6.11.1 | 13 | 2893850 | 0 |

Este inventario confirma que PyAV/FFmpeg, CTranslate2, ONNX Runtime, OpenCV, PyMuPDF, NumPy y Qt incorporan payload nativo. El propietario debe revisar los textos y componentes incorporados antes de redistribuir un entorno completo o un instalador que los incluya.

## Contenido de los artefactos de NeoCortex

- Wheel PRE-REPORT 0.6.0: **202** entradas; `RECORD` con **202** filas y verificación SHA-256/tamaños correcta.
- Sdist PRE-REPORT 0.6.0: **349** entradas, **338** archivos regulares, **116** módulos de prueba y **13** documentos en `docs`.
- Por el wildcard `docs/TECHNICAL_AUDIT_*.md` de `MANIFEST.in`, el sdist redistribuye los tres informes históricos y el informe actual. Antes de una publicación externa, el propietario debe confirmar que esa exposición deliberada de rutas, entorno y evidencia de auditoría sea apropiada.
- Rutas, duplicados, enlaces, especiales, cachés y binarios propios incidentales: wheel=limpio; sdist=limpio.
- `dist-info` ajenos dentro del wheel: `[]`. El wheel no incorpora wheels completos de dependencias; pip los resuelve por separado.
- Archivos LICENSE/COPYING/NOTICE del proyecto en wheel: `[]`; en sdist: `[]`.
- `docs/OFFLINE_INSTALLATION.md` existe en el árbol fuente, pero no aparece en este wheel ni sdist porque `MANIFEST.in` no lo incluye. Esta auditoría no alteró el manifiesto; una distribución futura debe decidir explícitamente si esa guía forma parte del artefacto.
- Los tres iconos de `_05_Interfaz/assets` se redistribuyen byte a byte en wheel y sdist. SVG/PNG/ICO no exponen autor, copyright, licencia o elemento de procedencia en las comprobaciones SVG/ExifTool realizadas.
- No se observaron ffprobe, Tesseract, modelos descargados ni otros binarios externos dentro del wheel o sdist de NeoCortex.
- En particular, wheel y sdist de NeoCortex contienen **0** entradas `.onnx` y no incorporan `nudenet/320n.onnx`. Ese modelo de **12150158** bytes pertenece al wheel instalado de la dependencia NudeNet.
- El código sí descubre `tesseract` para OCR y `ffprobe`/`ffmpeg` para audio como herramientas externas. Si un instalador futuro las redistribuye, necesitan un inventario técnico y avisos separados.
- El venv `--no-deps` contiene `neocortex-framework 0.6.0` y el launcher `Neocortex.exe`. Su `pip check` falla de manera esperada por las nueve dependencias base deliberadamente no instaladas. Un segundo venv aislado instalado desde el sdist con `--system-site-packages` pasó `pip check`, pero hereda el conjunto global. La resolución constrained completamente limpia quedó bloqueada offline al faltar `faster-whisper==1.2.1`; uv abortó sin instalar paquetes, sin red ni modelos.

El proyecto no declara `project.license` y no contiene un LICENSE ni NOTICE afirmativo. Este inventario no toma esa decisión pendiente.

## Decisiones requeridas del propietario

1. Elegir, con asesoría adecuada, la licencia de NeoCortex.
2. Resolver la modalidad de PyMuPDF y obligaciones de Qt.
3. Aclarar con el proveedor la contradicción MIT/AGPL de NudeNet y la licencia/procedencia de `320n.onnx`.
4. Confirmar procedencia y permiso de los assets gráficos.
5. Definir si se redistribuirán binarios, modelos o sólo wheel/sdist.
6. Revisar textos exactos, transitivas y componentes nativos.
7. Regenerar el inventario para cada resolución constrained promovida.

## Estructura propuesta para un NOTICE futuro

No se crea un NOTICE afirmativo. Si el propietario lo autoriza, debería separar: identidad/version de NeoCortex; dependencia/version/origen; textos íntegros requeridos; bibliotecas nativas; modelos/datasets; assets; método y fecha de generación.

## Limitaciones

- La metadata puede estar incompleta, contradictoria o desfasada.
- La presencia de un archivo no determina obligaciones o compatibilidad.
- No se realizó interpretación jurídica de los textos.
- No se descargaron modelos ni se ejecutaron binarios externos.
- Una instalación unconstrained puede resolver otras versiones.

## Fuente de verdad

`pyproject.toml` y `constraints.txt` definen el conjunto esperado; la metadata del Python global observado define las versiones transitivas de este snapshot. `OFFLINE_INSTALLATION.md` define la terminología operativa de instalación sin red. El venv `--no-deps` sólo valida el artefacto y el entrypoint, y el venv con paquetes del sistema no demuestra una resolución limpia. Antes de publicar, debe regenerarse este inventario desde un venv constrained completamente aislado.
