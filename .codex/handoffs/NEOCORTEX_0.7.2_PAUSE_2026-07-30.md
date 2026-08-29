# NeoCortex — handoff operativo vigente

> Actualizado: 2026-08-29 CST. El basename es histórico y permanece estable.
> `~/.codex/PENDIENTES.md` conserva el compromiso operativo; este archivo
> describe sólo la frontera técnica de reanudación.

## Objetivo activo

Reorganizar toda la topología productiva en el namespace único `neocortex`, con
responsabilidades explícitas, límites de dependencia y compatibilidad
transitoria verificable, hasta retirar `_01_Enumeracion`, `_02_Deduplicacion`,
`_03_Progreso`, `_04_Nucleo_Operativo`, `_05_Interfaz` y `Orquestador.py`.

## Corte Text aceptado

- Checkout: `/home/winterboss/Neocortex/Repository`
- SHA ejecutable aceptado: `860a8ec8dbcd32e2fc3514f6938d924f7ba4dbb9`
- Árbol verificado limpio; `main` sigue sin publicar y el push permanece
  reservado al cierre integral de `NEO-CORE-004`.
- Archive, DOCX, Audio, Image, Office, PDF, Video y Text viven físicamente en
  `neocortex/capabilities/formats/{archive,docx,audio,image,office,pdf,text,video}`.
  Las fachadas `_04_Nucleo_Operativo` correspondientes son compatibilidad
  explícita; las sondas runtime están en `neocortex/capabilities/runtime.py`.
- El registro de arquitectura conserva explícitos los cruces transitorios de
  las ocho familias hacia foundation/core. El fingerprint de transición actual
  es `core-architecture-target-v1:sha256:c41ff44364df73be950eb46dd8f2a19a4e26ed7529d23d51d66e34a152d78f3a`.

## Evidencia de aceptación y release

- `Neocortex code validate --baseline 07a9f3600edcc3d3b146a595ac4cfbd2cd407022`
  sobre `860a8ec` terminó `passed`: selección `full`, 337 pruebas reportadas,
  17 barreras, Coverage trusted-deep de 24 shards, experimentos, wheel,
  replay e identidades públicas. Receipt:
  `/home/winterboss/.local/state/Neocortex/self-analysis/validation-receipts/860a8ec8dbcd32e2fc3514f6938d924f7ba4dbb9-88ea686c42cde13910e6cdb09d4c168a8d8ab4f89ccc5bd5f6468f6621786888.json`
  (`sha256:1a3eea5a7ecdab0755383a9f279d796964ef22db0df9e6acd01cb35023843d09`).
- Trusted-deep publicó `run_id=52` con 980 archivos, 974 candidatos,
  `code_processed=0`, `code_cache_hits=974`, y el replay `run_id=53` conservó
  los mismos contadores, ambos sin errores.
- La release vigente se reconstruyó desde el SHA ejecutable aceptado:
  `0.9.0-860a8ec8dbcd-cp314-linux-x86_64`, pip `26.2.1`, Semgrep `1.172.0`;
  `release_linux.py verify` devolvió `verified=true` y current/manifest/launcher
  coinciden.
- E2E público instalado, sin `PYTHONPATH` ni `--apply`: un TXT produjo
  `processed=1`, `cache_hits=0`, `extracted=1`, `errors=0`; el replay produjo
  `processed=0`, `cache_hits=1`, `extracted=0`, `errors=0`, ambos con exit 0.
  Evidencia:
  `/home/winterboss/Documentos/NeoCortex/Auditorias/2026-08-29-neocortex-text-cohort/`.

## Próximo corte, en orden

1. Migrar la cohorte de plataforma compartida a un namespace canónico bajo
   `neocortex/platform`, empezando por `content_types`, `zip_safety` y las
   proyecciones/registro de capacidades, con fachadas compatibles y sin tocar
   el estado SQLite.
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
