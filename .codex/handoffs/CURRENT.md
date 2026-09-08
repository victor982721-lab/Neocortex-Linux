# Handoff operativo vigente — NeoCortex

**Última verificación:** 2026-09-08, `America/Mexico_City`  
**Checkout:** `/home/winterboss/Neocortex/Repository`  
**Fuente de verdad:** `main`, `origin/main`, `PENDIENTES.md` y el receipt
canónico de estabilización

## Alcance actual

La línea de estabilización corrige la validación reproducible, las fronteras de
estado y efectos, la ingesta acotada, el lifecycle Semantic, la paridad de
contratos y la construcción Linux reproducible, sin ampliar autoridad de
mutación. Linux/Kubuntu es la única plataforma activa, no se usa GitHub
Actions ni proveedores remotos, y R1–R4, KIO real, MCP escrito y el corpus real
permanecen fuera de alcance.

## Evidencia de cierre

- La suite integral, Ruff, Mypy, Pyright y Semgrep se ejecutaron con los venv
  aislados documentados; los resultados, exclusiones y hashes de logs están en
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-08-stabilization-3b00d03/`.
- La release vigente se construyó desde el SHA publicado, pasó doble-build
  reproducible, `pip check`, verificación de manifest/launcher y smoke headless
  aislado, y conserva sólo `current` y el rollback inmediato.
- El piloto aislado usa 28 fixtures heterogéneas, presupuesto por proceso de
  180 s y dos pasadas, con `new_work=0` en la segunda; el smoke de audio local
  también confirmó replay sin red.
- El hash de metadata del corpus real permaneció idéntico antes y después de
  la instalación, y `.staging` quedó vacío.

## Límites y siguiente paso

Los 177 warnings Pyright y las 433 cohortes de formato no son errores de
runtime y quedan clasificados para trabajo P2 por cohortes, mientras que las
44 wheels de analizadores ausentes permanecen como limitación explícita de la
reconstrucción offline del toolchain. El siguiente trabajo debe continuar por
hotspots y contratos, sin reabrir Windows, R1–R4 ni las superficies de mutación.
