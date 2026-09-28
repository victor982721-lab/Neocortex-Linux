# Corpus útil y efectos Linux — 2026-09-28

## Estado comprobado

Implementación publicada en main, release instalada y verificada offline desde
`fd17360ccb303f29a1d794e070f6453a79c52b5c`. Release
`0.14.1-fd17360ccb30-cp313-linux-x86_64`; rollback inmediato `c7beacaca81a`.
SQLite mantiene acreditación. Un eventual commit posterior de estos handoffs
es sólo documentación: cotejar el diff de equivalencia en el expediente externo.

Se preservó el cambio ajeno en AGENTS.md, sin incorporarlo al commit. El build
salió de un worktree limpio del SHA publicado. No se descargaron modelos ni
se transmitieron originales/estado a terceros.

## Resultado del corpus autorizado

- 143/143 contenidos originales conservados, 145 archivos finales y 41
  directorios, ninguno vacío. Dos PDF nuevos proceden de adjuntos EML.
- 38 movimientos organizativos, todos sincronizados. Se corrigió una extensión
  y se deduplicó una firma exacta; el original de esa firma está en Papelera
  con keeper idéntico dentro del corpus. 197 directorios vacíos retirados.
- 198 receipts Trash comprobados con identidad y metadata; backup anterior
  de originales y once bases permanece disponible fuera del corpus.
- Run 2 falló parcialmente por selección de un checkpoint ancestral de
  Inventory. La corrección C30 sigue sólo successors publicados y terminales;
  no se reparó la DB manualmente ni se hizo factory-reset.
- Runs 3/4/5 terminaron completas. Último replay instalado: 5,412 s,
  exact_replay texto/imagen, cero chunks/jobs/embeddings y efectos nuevos.
  Once bases íntegras. Heads actuales: texto 7, imagen 6.

## Cambios principales

Organización POSIX no-replace, padres fijados/identidad, intent/receipt/recovery,
lock antes del efecto y rebinding por lote con Inventory COW. Limpieza KIO
reversible de directorios vacíos. Destinos propios desambiguados estables y
rechazo de hardlinks, drift y reservas extranjeras.

XML preserva atributos útiles. EML prepara/parsea una vez y materializa adjuntos
con límites, lineage y replay tras organización/dedupe. ZIP usa marcadores
streaming, cuotas finitas medidas y colisiones sin overwrite; followup EML
posee etapa separada. Consumo histórico no afirma cobertura actual de hijos.

Proyección XLSX mantiene valores y hoja/celda sin JSON repetido; fórmulas sin
resultado cacheado se declaran, nunca se inventan. Knowledge reconstruye y
valida la misma proyección al citar. Metadatos CSV/TSV también se reconocen por
su estructura cuando Identify los conserva como txt: FTS íntegro, dense advisory.

Búsqueda conserva diagramas antes filtrados como fórmulas, exige IDs exactos
antes de fusión y reconoce unidades/fechas/listas explícitas sin expandir rangos.
La CLI ofrece --semantic-search-include-title opt-in all/text y muestra el rol
advisory_metadata separado de contenido. Errores integrados muestran su causa.

Progreso vertical sin vista compacta; formatos/fases/efectos/readiness distintos.
Receipts v11 compactos lossless, bytes públicos v1 estables y sin reescritura
histórica. Helpers privados sin consumidores retirados y replay Text con menos
SQL sin eliminar comprobación física/hash.

## Utilidad y coste medidos

Mismas 57 consultas principales (18 positivas + 1 negativa en tres modos),
diez suplementarias y tres Knowledge, sin excepciones y con paths remapeados
por SHA. Híbrido: 9→15/18 top1, 12→16/18 top5; texto: 8→12 y 10→14;
lexical: 6→8 y 8→9. El ID inexistente no produce hits en ningún modo.
No confundir encontrar fuente con verdad/suficiencia de respuesta. Latencia
agregada híbrida similar (38,72→39,13 s), no una aceleración demostrada.

5071→1266 chunks activos, con raw/FTS conservados. Copia fría de los mismos
143 originales: 237,366 s frente a 887,625 s del baseline; no A/B de host aislado.
El estado real conserva historial: Semantic 217.956.352 bytes, no una base nueva
compactada artificialmente. No reset/VACUUM para maquillar métricas.

Calibración visual etiquetada antes de scores: 40 imágenes, 6 positivos y 5
negativos, umbral 0,241709. Holdout: 9/10 positivos top5, 4/10 top1; sólo 1/2
negativos correctamente vacío, con falso positivo español comprobado. No hubo
retuning para ocultarlo; búsqueda visual útil pero no prueba técnica fiable.

## QA y límites

La global intermedia tuvo 8475 pass/84 skip/3 fallos corregidos luego en focos.
No se repitió una global final tras cada delta. Último foco CLI/retrieval:459;
C30/owners/interfaces:138; revisiones independientes con negativos, más E2E
instalado y real. No sumar suites solapadas. Ruff/Semgrep pasan; tipos conservan
deuda previa, sin diagnósticos nuevos en los alcances comparados.

No reparación automática o borrado inferido de ZIP corruptos. No ZIP genéricos,
audio o vídeo en la muestra real: sus pruebas son fixtures. Cien fuentes siguen
sin identificación semántica fiable y siete propuestas advisory no se fuerzan.
El diagrama general requiere título para subir al primer resultado; el pedido
contiene un número distinto del nombre. No afirmar precisión perfecta o un
óptimo universal. La matriz conserva 30 contratos, incluido C07 parcial.

## Evidencia y siguiente trabajo

`/home/ubuntu/Documents/NeoCortex/Auditorias/2026-09-28-correccion-utilidad-linux-01a0e4bd/`
contiene INFORME.md, MATRIZ.md, AVANCE.md, REPRODUCIR.md, benchmarks, respaldos,
receipts, pruebas fallidas/rechecks y verificación de release. El SSOT externo
conserva publicación y equivalencia del cierre documental.

Próximas decisiones: política reversible para inciertos, recuperación/disposición
ZIP explícita, calidad visual multilingüe y diagramas por estructura/layout,
retención histórica con preservación de lineage. No reabrir esas capacidades ni
mutar corpus/modelos por este handoff sin una solicitud nueva pertinente.
