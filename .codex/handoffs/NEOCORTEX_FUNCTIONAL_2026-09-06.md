# Mejora funcional — implementación y aceptación

## Objetivo y autoridad

Plan definitivo autorizado en la tarea `01a0778b-9ef1-7130-81dc-5db1f48bc6de`.
Pendiente: `NEO-FUN-001`, relacionado con `NEO-CUR-001` y `NEO-AUTH-001`.
Baseline de fuente e instalación: `d1e06b9519fe2de3a159cd3522a5ea63546ff0d1`.
La autorización incluye implementar, validar y publicar el plan; no permite
limpieza, movimientos del corpus, KIO real, nuevos modelos ni proveedores.

## Entregas y cierre

1. P0: identidad por owner/codec, curación localizable y ámbito de entrada.
2. P0: evidencia dedup por miembro, keeper explicable, alias, contadores y tiempos.
3. P1: recuperación con intención/negación y fragmentos que sostengan la consulta.
4. P1: contexto v2 en CLI/MCP y diagnóstico por propietarios útil para decidir.
5. P2: lectura con presupuesto/reuso seguro e incrementalidad sin trabajo derivado nuevo.

Los incidentes reales de curación y exclusión del PDF se mantienen abiertos
hasta localizar la identidad y la etapa causales y verificar sus correcciones.
Un timeout sólo limita una ejecución; nunca aprueba un incidente inconcluso.
Cada piloto usa estado aislado o lecturas coherentes, 20–50 elementos como
máximo y un límite de 15 minutos. No hay piloto del corpus completo.

La evaluación inicia con 40 fixtures y 30 consultas (20 desarrollo y 10 reserva,
con 16/4 y 8/2 positivas/negativas), separadas por familias y congeladas antes
del ajuste. La reserva no se usa para tuning. Success@5, Recall@5 real y nDCG@10
se miden por recurso documental lógico, no por fragmentos repetidos.

Víctor acotó expresamente este corte a pasajes verificables y dejó al LLM
la evaluación de suficiencia, priorizando cerrar sin ampliar el motor.
R1 y R2 conservan sus resultados fallidos y la recuperación de 7/8 como
limitación, no como cumplimiento de la meta original de 8/8. La aceptación
acotada exige fuentes, citas, localizadores, cobertura y fragmentos útiles
desde las interfaces instaladas, sin atribuir al motor una decisión de
suficiencia ni iniciar otra campaña de ajuste o reserva.

Publicación exige HEAD == main == origin/main y árbol limpio. La aceptación
operativa exige instalación final, manifest/launcher y smoke/replay/llamada MCP
real; v2 debe ser el default de los callers de agente y v1 seguir seleccionable.
Replay permite una ejecución y recibos nuevos, no regeneración injustificada.

## Ownership de implementación

- Identidad: catálogo/schema/bindings, curation preview y su error CLI.
- Organización: planner/scope/model/policy, sin escribir catálogo ni CLI general.
- Dedup: owner deduplication, contratos/proofs/keeper/alias.
- Retrieval: lexical, búsqueda Semantic, fusión/planner Knowledge.
- Contexto: contratos/proyección v2, read API y sus callers CLI/MCP/GUI.
- Estado: presupuestos, SQLite, salud, retención e incrementalidad Semantic.
- Formatos: Archive/PDF/Texto/Imagen, tipo lógico y taxonomía documental.
- Diagnóstico: asset health y consultas/agrupación de review.
- Evaluación: fixtures, métricas y reserva independiente; no cambia ranking.
- Forense: sólo snapshots coherentes y evidencia de los incidentes reales.
- Raíz: CLI general/reporting, integración, docs, Git/release y SSOT.

Un único escritor por archivo. Los cambios de contrato entre frentes se
coordinan antes de integrar, sin descartar cambios concurrentes.

## Evidencia canónica

`/home/winterboss/Documentos/NeoCortex/Auditorias/2026-09-06-functional-improvement/`

El estado inicial no acredita ninguna corrección, aprobación de reserva ni
cierre de incidente. Los receipts detallados y logs viven fuera de docs;
este handoff conserva únicamente el avance mínimo necesario para reanudar.

## Congelación del candidato de integración

- Frentes y contratos implementados con pruebas focales. La primera suite
  ejecutada produjo 6,175 pasadas, 43 fallidas y 9 errores de setup headless;
  las 43 fallas se corrigieron y sus 21 módulos completos pasaron (486 pruebas).
- La validación headless exige un intérprete instalado, no el editable de
  desarrollo: usar `NEOCORTEX_TEST_PYTHON` apuntando al artefacto candidato.
  No retirar ese guard ni presentar el setup fallido como prueba aprobada.
- Ambas consultas reales sitúan el PDF en top-3 del flujo fusionado sin
  reindexar. Con los trece owners disponibles en la copia migrada figura
  tercero en ambas, con la condición y la página verificadas.
  El semántico puro consolidado conserva puestos 4 y 10, no top-3.
- El baseline completo inmutable conserva 87 archivos y 13 owners. Migraciones
  y pruebas posteriores usan otra copia, nunca hardlinks a esa evidencia.
- El primer candidato instalado pasó 6,247 pruebas, con 59 omitidas y 42
  subtests, pero DEV v2 detectó cuatro citas sin comprobación suficiente.
  El siguiente cambio corrige coordenadas de evidencia, lookup directo DOCX
  y omisiones intencionales del canal visual, sin cambiar modelos o métricas.
- El cierre independiente detectó preferencias keeper sólo disponibles en
  Python; la CLI normal debe llevar decisiones y ubicaciones preferidas al
  mismo planificador, con referencias verificadas desde su owner existente.
- La aceptación acotada requiere el nuevo artefacto, pruebas aplicables,
  pasajes verificables, incidentes reales y replay final con todos los owners,
  publicación y promoción productiva; no recalifica las reservas fallidas.
  El estado vigente de esas barreras, los hashes y sus receipts se registran
  en `NEO-FUN-001` y su historial, no se infieren desde este handoff de fuente.
