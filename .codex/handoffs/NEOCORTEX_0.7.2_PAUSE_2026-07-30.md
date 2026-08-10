# NeoCortex — handoff operativo actual

> Actualizado: 2026-08-10. El nombre del archivo es histórico y se conserva
> únicamente como ruta estable. Este documento es la fuente única de la
> frontera funcional y del orden de continuación vigentes.

## Preferencia operativa de Víctor

- GitHub debe conservar sólo `main` como rama visible.
- No crear PR ni ramas para evoluciones ordinarias de este proyecto personal.
- Cada entrega aceptada se integra con commits atómicos directos en `main`, una
  release Linux del SHA final exacto, verificación del launcher público y push
  directo a `origin/main` con CI verde.
- No usar `--apply` ni `--organization-apply` en Linux. Ningún resultado de
  búsqueda, OCR, clasificación o similitud autoriza una mutación.
- El corpus, el estado vivo y el launcher instalado son el SSOT operativo; los
  benchmarks y fixtures sólo autorizan pilotos posteriores, no promociones.

## Frontera NeoCortex 0.8 entregada

La versión de producto es `0.8.0`. La entrega es la cadena de commits de
`main`, no un SHA aislado anotado aquí: el criterio de cierre al final de este
documento exige igualdad dinámica entre Git, release, launcher y GitHub.

### Consulta humana y para agentes

- `Neocortex status|search|ask|inspect` consume únicamente snapshots
  publicados con scopes fijos `personal`, `framework` o ambos por separado.
  Conserva rutas, revisiones, señales, citas, completitud e incertidumbre en
  JSON legible; nunca acepta una ruta de estado ni abre productores.
- `Neocortex review value` ordena candidatos de poco valor de forma consultiva
  y fail-closed. En vivo devolvió `availability=ready`, `complete=true`, 844
  candidatos examinados y una ventana de 20; `advisory_only=true` y
  `mutation_authorized=false`.
- La GUI incorpora la página **Consulta** sin alterar los índices históricos.
  Permite búsqueda, contexto citado, status y revisión de valor. La inspección
  visual de la release instalada, a 1440×900, confirmó el aviso de solo lectura,
  cinco resultados reales y ausencia de controles de mutación.
- `Neocortex agent serve` expone por MCP stdio exactamente `status`, `search`,
  `context`, `evidence` e `inspect_code`. Las cinco herramientas declaran
  `readOnlyHint=true`, `destructiveHint=false`, `openWorldHint=false`; un
  intercambio JSON-RPC real desde el launcher instalado terminó limpiamente
  con código 0 y sin proceso huérfano.

### Knowledge y búsqueda

- Una ventana top-k normal ya no se confunde con truncamiento. Los contratos
  distinguen `result_window_full`, candidatos omitidos por ventana, hard cutoff,
  `next_cursor` y `cutoff_score`; sólo un corte real marca el resultado parcial.
- `ContextBundle` reserva primero la mejor cita K1 cuando cabe y reduce el
  diagnóstico antes de sacrificar evidencia. `code_search` ordena de forma
  determinista ubicaciones `None|string`.
- La búsqueda lexical conserva AND estricto como primera vía. Sólo ante cero
  aciertos elimina stopwords ES/EN/DE y aplica un fallback acotado; las
  consultas CJK sin espacios también tienen tokenización y regresiones propias.
- La búsqueda humana viva de mantenimiento de transformadores devolvió cinco
  resultados con `complete=true` y `result_window_full=true`. `Neocortex ask`
  devolvió cuatro citas útiles y código 4 de forma intencional cuando el
  presupuesto de caracteres omitió un candidato y truncó dos fragmentos.

### Semantic

- El escaneo exacto de vectores usa NumPy por páginas acotadas, valida dtype,
  dimensiones, finitud y norma, y conserva fallback escalar. En el fixture
  3,233×768 bajó de 0.826 s a 0.043 s (aprox. 19×) sin cambiar scores extremos.
- Texto Jina y OCR siguen publicados en espacios firmados. La última repetición
  estable de `Neocortex --all` creó generaciones `ready` sin jobs nuevos:
  4,655 chunks de 360 items de texto, 431 items visuales y 294 chunks OCR de
  imagen, con `queued=embedded=failed=pending=leased=0` en el replay.
- CLIP ahora es fail-closed: exige una calibración humana positiva y negativa
  ligada a modelos, pipeline y backend exactos. Sin contrato no carga el
  backend, escanea 0 vectores, devuelve 0 vecinos y explica
  `image_retrieval_not_calibrated`.
- El fixture CLIP inspeccionado contiene 25 imágenes y 50 consultas (25
  positivas, 25 negativas). Las distribuciones se solapan: mínimo positivo
  0.204114 frente a máximo negativo 0.238547. En el diagnóstico acotado sobre
  431 vectores, negativos llegaron a 0.300938; ese piso conservaría sólo 32 %
  de los positivos. No se fabricó ni cableó un umbral escalar inseguro.
- Un bakeoff offline ES/EN/DE/ZH de 24 consultas y 48 candidatos favoreció a
  MiniLM 384d frente a Jina 768d: top-1 58.3 % vs 41.7 %, MRR 0.736 vs 0.505,
  R@5 95.8 % vs 54.2 % y mediana warm 6.31 ms vs 24.70 ms. La muestra es
  sintética y pequeña; MiniLM no se promovió, no se mezclaron espacios ni se
  reutilizó el piso de Jina.

### Documentos, OCR e imagen

- Office schema 2 persiste cada celda XLSX no vacía con libro, hoja, A1, tipo,
  valor lógico y XML crudo, fórmula y caché separados, estilo/formato y una
  proyección FTS compatible. La migración 1→2 fue aditiva y el replay reutilizó
  20/20 libros.
- PDF schema 12 persiste por página el perfil OCR, idiomas efectivos, OSD,
  confianza y fallback. Imagen y Video comparten perfiles explícitos
  `configured` y `auto-multilingual` sin inferir paquetes ausentes.
- El runtime vivo tiene Tesseract `spa`, `eng`, `deu`, `chi_sim`, `chi_tra` y
  `osd`. Los doctores PDF y Video de la release instalada aprobaron esos seis
  paquetes; Video aprobó además `/usr/bin/ffmpeg` y `/usr/bin/ffprobe`.
- Archive conserva ZIP anidado sin materializar miembros: 31 contenedores, 27
  completos, 4 parciales, 258 miembros, 240 indexados, 18 sólo por metadata,
  3 ZIP anidados, 5,089,368 caracteres y 8 incidencias de seguridad visibles.
- Texto físico conserva 48 candidatos y reutilizó los 48; uno es un error
  permanente cacheado porque el PPT CFB está corrupto y no contiene texto
  recuperable. La ruta informó 0 errores nuevos en el replay.

### Video y audio visual-only

- Video schema 1 indexa muestreo acotado de frames, escenas/keyframes,
  dimensiones, OCR por frame, métricas, procedencia y enlace al transcript de
  Audio. Los límites son fail-closed y el worker usa 2 GiB de memoria virtual,
  valor requerido por FFmpeg en el piloto Linux real.
- Un video sin pista de audio se registra en Audio como `no_audio` benigno, sin
  adquirir ni cargar el transcriptor; Video continúa y publica el contenido
  visual. El piloto FFmpeg aislado `route=all` completó así con 0 errores y sin
  reviews espurias.
- El corpus vivo actual no contiene candidatos Audio ni Video. El schema y los
  doctores están verificados, pero no debe presentarse como validación de videos
  reales de Víctor hasta añadir una muestra representativa autorizada.

## Migración, integridad e incrementalidad vivas

- Antes de migrar se creó un backup SQLite consistente en
  `/home/winterboss/.codex/vault/backups/neocortex/pre-ed2b340-live-0.8/`.
  Su `backup-manifest.json` tiene SHA-256
  `9e450b85bb46934c8de7c25c1daafb017b6a5f81bbf6ea133b8c2bdd8c351d83`;
  las doce copias aprobaron `integrity_check` y `foreign_key_check`.
- El corpus tenía y conserva exactamente 844 archivos, 102 directorios sin
  contar la raíz, 0 symlinks y 430,572,271 bytes. Antes y después coinciden:
  contenido `d22ae1a61bd0406de7a9cb1d12162b505bddda155bc43ee9cb4d1158dd89ef64`,
  rutas `cf9fe8fd2d6a1b7650b9ae2fd560433acf27136f2a863ea4f46e6580a873f253`
  y metadata
  `49dc1f9c5cb269c9ac2505439af03d3326fa59d15ddcc0689b735cb5cb656cfd`.
- Las trece bases vivas aprobaron integridad y claves foráneas tras la
  migración. Schemas publicados: Dedup 9, Framework 20, PDF 12, DOCX 5,
  Office 2, Archive 1, Text 1, Audio 1, Video 1, Image 5, Catalog 6, Code 4 y
  Semantic 6.
- La primera corrida reanudó Semantic después de un límite externo y terminó
  los 790 jobs restantes sin pérdidas. El replay final estable reutilizó
  PDF 288/288, DOCX 26/26, Office 20/20, Archive 31/31, Text 48/48 e Image
  431/431; no extrajo, clasificó, convirtió, transcribió ni embebió contenido
  nuevo, y terminó con `action_mode=dry-run` y `action_errors=0`.
- No se usó `--apply`, `--organization-apply` ni se modificó o materializó un
  original del corpus.

## Barreras de release ejecutadas

- Suite integral local: **4,034 passed, 144 skipped, 98 subtests passed** en
  267.40 s. Quedó una advertencia upstream de Pydantic Settings al construir
  FastMCP; el protocolo MCP y su cierre real sí aprobaron.
- Ruff y `ruff format --check` aprobaron los 112 archivos Python modificados;
  mypy focal aprobó las superficies de agente, imagen y Knowledge; YAML de CI,
  `compileall` y `git diff --check` aprobaron.
- El repositorio completo todavía registra 77 hallazgos Ruff heredados fuera de
  la superficie modificada. No afectan la suite ni se ocultaron cambiando
  reglas; siguen siendo deuda estática, no una barrera falsamente declarada
  verde.
- La instalación candidata `0.8.0` preparó Jina, MiniLM, CLIP texto/visión,
  Whisper y NudeNet; `tools/release_linux.py verify`, el launcher público y los
  doctores vivos aprobaron. El último commit documental exige reinstalar el SHA
  final antes del push, como se especifica abajo.
- Evidencia mínima persistente: snapshots, comparación, logs de `--all`,
  consultas públicas, MCP y capturas GUI están bajo
  `/home/winterboss/.codex/vault/evidence/neocortex-0.8-release-2026-08-09/`;
  calibración CLIP y bakeoff tienen directorios de evidencia separados.

## Carencias y límites que no deben ocultarse

1. **CLIP no calibrado:** la búsqueda visual se abstiene por diseño hasta
   disponer de una política robusta; hoy no devuelve falsos vecinos, pero
   tampoco recuperación visual textual.
2. **MiniLM sin promover:** el bakeoff justifica un piloto shadow con consultas
   reales etiquetadas, no un cambio automático de modelo ni dos modelos
   residentes permanentemente.
3. **Video vivo sin muestra:** no hay candidatos Audio/Video en el corpus; la
   fidelidad real se demostró sólo en fixtures y un piloto FFmpeg aislado.
4. **Presupuesto de contexto:** `Neocortex ask` puede devolver código 4 con citas
   útiles cuando el límite de caracteres impide una respuesta exhaustiva. Es
   una señal honesta de parcialidad, no debe convertirse en éxito completo.
5. **Contenido irrecuperable:** permanece un PPT CFB corrupto y ocho incidencias
   Archive acotadas. No se inventa texto ni se relajan límites para ocultarlos.
6. **Archive Semantic:** Archive tiene FTS y procedencia, pero no se publicaron
   por inercia sus miles de chunks en Semantic. Sólo procede por selectores si
   mejora consultas reales frente a lexical.
7. **Organización Linux:** búsqueda, catálogo, clasificación y previews están
   disponibles; la mutación continúa intencionalmente deshabilitada hasta un
   backend POSIX ligado a identidad con garantías equivalentes a Windows.
8. **Relaciones y experiencia:** todavía no existe un grafo transversal que
   convierta toda evidencia en conocimiento causal. La consulta GUI es
   síncrona; una primera búsqueda fría puede pausar brevemente la ventana y
   conviene volverla asíncrona si el uso real lo hace perceptible.

## Próximos pasos, en orden

1. Usar `Neocortex search|ask` y la página Consulta para resolver preguntas
   reales; usar `Neocortex review value` para revisar archivos de
   bajo valor sin mover ni eliminar nada.
2. Etiquetar con Víctor 20–50 consultas reales ES/EN/DE/ZH y ejecutar MiniLM en
   un espacio shadow separado. Promoverlo sólo si mantiene calidad, latencia,
   procedencia y calibración por fuente sobre ese conjunto.
3. Ampliar la calibración CLIP con positivos/negativos reales por idioma y tipo
   de imagen. No introducir un piso escalar mientras las distribuciones sigan
   solapadas.
4. Añadir una muestra pequeña y autorizada de videos representativos para
   validar en estado vivo escenas, OCR DE/ZH, visual-only y enlaces Audio; repetir
   la corrida para demostrar caché antes de escalar.
5. Evaluar Archive Semantic sólo sobre selectores útiles y contra la línea base
   FTS. Después, si el uso lo justifica, volver asíncrona la consulta GUI y
   reducir gradualmente la deuda Ruff heredada sin mezclarla con cambios
   funcionales.

## Criterio de cierre de esta release

La entrega no termina sólo por código, pruebas o la versión `0.8.0`. Deben
cumplirse y comprobarse juntos:

1. `git rev-parse HEAD` = `git rev-parse origin/main`;
2. `current/neocortex-release.json:source_sha` = ese mismo SHA;
3. `python3.14 tools/release_linux.py verify` y `Neocortex --version` aprueban;
4. una repetición `Neocortex --all` desde el launcher final conserva corpus,
   integridad, dry-run e incrementalidad;
5. las consultas humanas, MCP stdio y GUI instalados siguen funcionando;
6. el CI de push de GitHub queda verde y GitHub sólo expone `main`.

Si cualquiera difiere, la release sigue abierta y se corrige sobre `main` antes
de declarar el cierre.
