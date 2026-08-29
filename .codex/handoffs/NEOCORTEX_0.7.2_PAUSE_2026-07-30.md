# NeoCortex — handoff operativo vigente

> Actualizado: 2026-08-29 CST. El basename es histórico y permanece estable.
> `~/.codex/PENDIENTES.md` conserva el compromiso operativo; este archivo
> describe sólo la frontera técnica de reanudación.

## Objetivo activo

Reorganizar toda la topología productiva en el namespace único `neocortex`, con
responsabilidades explícitas, límites de dependencia y compatibilidad
transitoria verificable, hasta retirar `_01_Enumeracion`, `_02_Deduplicacion`,
`_03_Progreso`, `_04_Nucleo_Operativo`, `_05_Interfaz` y `Orquestador.py`.

## Corte Plataforma aceptado

- Checkout: `/home/winterboss/Neocortex/Repository`
- SHA ejecutable aceptado: `ee4bdec1359fab0cc9fed18af6973f57f792bf33`
- Árbol verificado limpio; `main` sigue sin publicar y el push permanece
  reservado al cierre integral de `NEO-CORE-004`.
- Archive, DOCX, Audio, Image, Office, PDF, Video y Text viven físicamente en
  `neocortex/capabilities/formats/{archive,docx,audio,image,office,pdf,text,video}`.
  Las fachadas `_04_Nucleo_Operativo` correspondientes son compatibilidad
  explícita; las sondas runtime están en `neocortex/capabilities/runtime.py`.
- La cohorte compartida de plataforma vive físicamente en
  `neocortex/platform/{architecture_projection,capability_registry,capability_registry_specs,content_types,zip_safety}.py`.
  Las rutas `_04_Nucleo_Operativo.platform.shared.*` y los aliases planos de
  tipos/ZIP son fachadas de compatibilidad, sin una segunda implementación.
- El registro de arquitectura conserva explícitos los cruces transitorios de
  las ocho familias hacia foundation/core. El fingerprint de transición actual
  es `core-architecture-target-v1:sha256:779cf513def70abcde4f7c42f9dca335bc891ef382e1610840cfedd0ef2de9fa`.

## Evidencia de aceptación y release

- `Neocortex code validate --baseline e0f65f8ae2ca054a161b9a4100f7ab3b1a8c473e`
  sobre `ee4bdec` terminó `passed`: selección `full`, 338 pruebas reportadas,
  17 barreras, Coverage trusted-deep de 24 shards, experimentos, wheel,
  replay e identidades públicas. Receipt:
  `/home/winterboss/.local/state/Neocortex/self-analysis/validation-receipts/ee4bdec1359fab0cc9fed18af6973f57f792bf33-44a05ed08f4da0b7fc5568952862ab1dc533a2bfa5ecb456b6acedbe3582aa9d.json`
  (`sha256:d6135e44d001e1aa8e269576671192171351bd83f0859cd36a72ed2daa0067c5`).
- Trusted-deep publicó `run_id=56` con 987 archivos, 981 candidatos,
  `code_processed=0`, `code_cache_hits=981`, y el replay `run_id=57` conservó
  los mismos contadores, ambos sin errores.
- La release vigente se reconstruyó desde el SHA ejecutable aceptado:
  `0.9.0-ee4bdec1359f-cp314-linux-x86_64`, pip `26.2.1`, Semgrep `1.172.0`;
  `release_linux.py verify` devolvió `verified=true` y current/manifest/launcher
  coinciden.
- E2E público instalado, sin `PYTHONPATH` ni `--apply`: un ZIP produjo
  `processed=1`, `cache_hits=0`, `complete=1`, `members=1`, `indexed=1` y
  `errors=0`; el replay mantuvo `processed=1`, `cache_hits=1`, `complete=1`,
  `members=1`, `indexed=1` y `errors=0`, ambos con exit 0. Evidencia:
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-08-29-neocortex-platform-cohort/`.

## Próximo corte, en orden

1. Migrar la frontera `foundation.identity` y `foundation.provenance` a un
   namespace canónico bajo `neocortex/foundation`, empezando por
   `file_identity.py` y `processing_provenance.py`, con fachadas compatibles,
   consumidores explícitos y sin tocar el estado SQLite.
2. Mantener el lote material, ejecutar pruebas focales, congelar un commit,
   renovar trusted-static con la consulta pip-audit autorizada, ejecutar una sola
   aceptación canónica, instalar desde el SHA y repetir el E2E instalado.
3. Después continuar por las responsabilidades restantes de `_04_Nucleo_Operativo`
   (runtime, workflow, knowledge, semantic y code) hasta retirar las raíces
   numeradas, sin iniciar un nuevo corte mientras falte el receipt, release o
   E2E del corte vigente y sin hacer push hasta el cierre integral de
   `NEO-CORE-004`.

## Límites

- Linux/Kubuntu es la única plataforma activa; Windows y GitHub Actions quedan
  fuera de alcance.
- Durante gates observa únicamente stream, transcript, proceso y systemd en el
  namespace real; no abras SQLite cercadas con lectores ordinarios.
- pip-audit sólo se renueva mediante su productor explícito autorizado, sin
  `--fix` ni otros proveedores remotos.
