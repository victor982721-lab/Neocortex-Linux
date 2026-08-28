# NeoCortex — handoff operativo vigente

> Actualizado: 2026-08-28 CST. El basename es histórico y permanece estable.
> `~/.codex/PENDIENTES.md` conserva el compromiso operativo; este archivo
> describe sólo la frontera técnica de reanudación.

## Objetivo activo

Reorganizar toda la topología productiva en el namespace único `neocortex`, con
responsabilidades explícitas, límites de dependencia y compatibilidad
transitoria verificable, hasta retirar `_01_Enumeracion`, `_02_Deduplicacion`,
`_03_Progreso`, `_04_Nucleo_Operativo`, `_05_Interfaz` y `Orquestador.py`.

## Corte aceptado

- Checkout: `/home/winterboss/Neocortex/Repository`
- SHA aceptado: `f92f4e9ff070a2098d8f01a48beb68696e5fe60d`
- Árbol verificado limpio en ese corte; `main` sigue sin publicar y está 36
  commits delante de `origin/main`.
- Archive vive en
  `neocortex/capabilities/formats/archive/{models,route,state,text_worker}.py`;
  los módulos `_04_Nucleo_Operativo` correspondientes son fachadas de
  compatibilidad. Las sondas de capacidades viven en
  `neocortex/capabilities/runtime.py` y el paquete ya no colisiona con un
  módulo plano.
- El registro de arquitectura conserva explícitos los 11 cruces transitorios
  Archive → foundation/core; deben desaparecer al migrar esas hojas.

## Evidencia de aceptación y release

- `Neocortex code validate --baseline 23a9597374266887357f62c7b02ee3681e77b916`
  terminó `passed`: 17 barreras, 335 pruebas seleccionadas, Coverage 24/24,
  experimentos, wheel, replay e identidades públicas. Receipt:
  `/home/winterboss/.local/state/Neocortex/self-analysis/validation-receipts/f92f4e9ff070a2098d8f01a48beb68696e5fe60d-bf89169b5fe639c837dd6a6e74a17c43992226656e704d2acdaa9aae6bdca62d.json`
  (`sha256:7809a854f82525ad9a85302927a8f6b600356f29fbd9a85f0fd7b54e97eef6b2`).
- Release instalada y verificada: `0.9.0-f92f4e9ff070-cp314-linux-x86_64`,
  pip `26.2.1`, Semgrep `1.172.0`; `current`, manifest y launcher declaran el
  mismo SHA.
- E2E instalado, sin `PYTHONPATH` ni `--apply`: primera corrida de 20 módulos
  (`processed=20`) y replay de los mismos 20 (`processed=0`, `cache_hits=20`),
  ambas con exit 0. Transcripts finales:
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-08-28-neocortex-archive-cohort/`.

## Próximo corte, en orden

1. Migrar una familia de formatos siguiente, preferentemente DOCX, con
   implementación física bajo `neocortex/capabilities/formats/docx`, fachadas
   `_04` explícitas y consumidores productivos apuntando al namespace nuevo.
2. Mantener un lote material coherente, ejecutar pruebas focales y congelar un
   commit; después producir el snapshot trusted-static autorizado, ejecutar una
   sola aceptación canónica, instalar desde el SHA y repetir el E2E instalado.
3. No iniciar otra vertical mientras falte el receipt, la release o el E2E del
   corte vigente; no hacer push hasta el cierre integral de `NEO-CORE-004`.

## Límites

- Linux/Kubuntu es la única plataforma activa; Windows y GitHub Actions quedan
  fuera de alcance.
- Durante gates observa únicamente stream, transcript, proceso y systemd en el
  namespace real; no abras SQLite cercadas con lectores ordinarios.
- pip-audit sólo se renueva mediante su productor explícito autorizado, sin
  `--fix` ni otros proveedores remotos.
