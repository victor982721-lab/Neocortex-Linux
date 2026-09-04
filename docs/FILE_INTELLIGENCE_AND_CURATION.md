# File Intelligence & Curation

## Visión

NeoCortex debe permitir que Víctor pase de “tengo una carpeta caótica” a
“entiendo qué contiene, revisé un plan, autoricé efectos concretos y verifiqué el
resultado” sin crear un script nuevo para cada caso.

El producto sirve a dos consumidores con los mismos contratos:

- una persona que usa CLI o GUI;
- un agente local que necesita evidencia paginada, citable y accionable.

El agente no recibe más autoridad que la persona. El contenido del corpus, una
clasificación, una similitud o una recomendación nunca autorizan efectos.
La falta de autoridad para mutar no debe impedir que NeoCortex observe, relacione,
explique la incertidumbre y prepare una propuesta útil para revisión.

## Etiquetas de estado

- **CURRENT:** frontera vigente del producto, aunque una capacidad nueva aún no
  esté instalada desde el SHA final.
- **IMPLEMENTED:** código y pruebas presentes en el checkout; requiere promoción
  para considerarse disponible en el launcher instalado.
- **TARGET:** contrato futuro que todavía no existe como capacidad pública.

## Recorrido del producto

```text
descubrir → identificar → comprender → relacionar → proponer
          → revisar → autorizar → aplicar → verificar → conciliar
```

Cada etapa produce un artefacto durable o una abstención explicable:

1. **Descubrir:** enumerar con límites, progreso y checkpoint.
2. **Identificar:** conservar identidad física, revisión de contenido y linaje.
3. **Comprender:** detectar tipo real, extraer estructura y representar
   procedencia, confianza e incertidumbre.
4. **Relacionar:** separar duplicado exacto, versión, similitud y pertenencia.
5. **Proponer:** crear un plan inmutable y paginado con razones y cobertura.
6. **Revisar:** conservar decisiones humanas sin convertirlas en efectos.
7. **Autorizar:** ligar actor, alcance, caducidad, límites y digest del plan.
8. **Aplicar:** ejecutar sólo operaciones soportadas y revalidar junto al efecto.
9. **Verificar:** demostrar origen/destino/Papelera, identidad, bytes y conteos.
10. **Conciliar:** ante caída o ambigüedad, observar antes de reintentar.

**IMPLEMENTED** alcanza `plan → review → decide → authorize`: plan consulta,
review publica tareas advisory, decide registra una decisión humana y authorize
emite un grant durable separado. **TARGET** comienza en `apply`; ni una decisión
ReviewTask ni la existencia del grant demuestran un efecto físico.

## Evidencia y autoridad

Una observación debe indicar productor, versión, fecha, identidad, cobertura y
localizador. Las afirmaciones se tipan como:

- hecho estructural o extraído;
- inferencia con modelo/regla y confianza;
- decisión humana;
- autorización explícita;
- efecto observado;
- incertidumbre o ausencia de cobertura.

Los scores de texto e imagen permanecen en sus espacios y no se convierten en
certeza. Un duplicado destructivo exige verificación byte a byte; una huella
rápida sólo reduce candidatos. “Regenerable”, “de terceros” o “personal” requiere
evidencia de procedencia, no nombres o extensiones aislados.

## Estado actual

La fuente `0.9.0` aporta inventario, extracción multimodal, catálogos, búsqueda,
Knowledge, Semantic, Code como contenido, planes de duplicados/organización,
Review, receipts y recuperación parcial.

- **CURRENT:** `curate plan`, `curation_plan` y `--curation-preview` consultan el
  plan local paginado sin escribir estado o corpus.
- **IMPLEMENTED:** `curate review` publica páginas con cobertura completa como
  `ReviewTask` advisory; `curate decide` añade por CAS una decisión humana
  `resolved` o `dismissed`. Sólo escriben Framework, nunca `file_actions`, corpus
  o sistemas externos, y `actions_authorized` permanece `false`.
- **IMPLEMENTED:** `curate authorize` emite un `AuthorizationGrant` inmutable en
  la extensión `curation_authorization_grants` de Framework. El grant liga plan,
  snapshot, tareas resueltas, actor, acción, límites y expiración; declara
  `actions_authorized=true` y `physical_effect_applied=false`.
- **TARGET:** `apply → verify → reconcile` consumirá y revalidará el grant.

Las brechas principales son:

- no existe aún `apply → verify → reconcile`, la principal brecha del lifecycle;
- varios formatos pierden localizadores estructurales al llegar a búsqueda;
- igualdad, versión, procedencia, valor y disposición no tienen una proyección
  pública unificada;
- MCP ya expone plan y las escrituras advisory `curation_review` y
  `curation_decide`, pero no `authorize`: falta un principal autenticado;
- Linux no aplica movimientos ni Papelera;
- progreso, cancelación y replay no son uniformes en todos los productores.

## Decisión Linux para Papelera

La fuente ya prepara la foundation `neocortex.safety.kio_trash`, que descubre el
primer cliente disponible entre `kioclient6`, `kioclient5` y `kioclient`, valida
configuración y snapshot, ejecuta mediante un runner inyectable
`move <origen> trash:/` y exige verificación del caller antes de emitir receipt.
No está conectada a la CLI de aplicación, promovida ni probada contra KIO real.

La integración de producto añadirá raíz, permisos, locks, autorización,
same-filesystem y recovery sobre esa foundation. Después comprobará la ausencia
del origen, la entrada esperada en `trash:/`, su metadata de restauración y el
receipt. Un timeout, error ambiguo, configuración KDE no escribible, symlink,
hard link no admitido, mount inseguro o cambio concurrente deja
`recovery_required` y no se reintenta a ciegas.

No se usará `gio trash`, `unlink`, borrado permanente ni una carpeta de
cuarentena como fallback. La prueba real de KIO queda fuera de esta cohorte para
no tocar la configuración de escritorio; las pruebas de la foundation usan
runner, resolver y verificador inyectados sobre fixtures contenidos.

## Interfaces CURRENT, IMPLEMENTED y TARGET

**CURRENT — consulta:**

```text
Neocortex curate plan [--limit N] [--cursor TOKEN] [--json]
MCP: curation_plan
```

El `PLAN_ID` consumido por las operaciones siguientes es el `plan_digest`
`sha256:<64 hex>` devuelto por plan.

**IMPLEMENTED — ReviewTask advisory:**

```text
Neocortex curate review PLAN_ID [--limit N] [--cursor TOKEN] [--json]
Neocortex curate decide PLAN_ID ITEM_ID --expected-event-id EVENT_ID
  --decision resolved|dismissed
  --decision-scope until-source-change|until-policy-change|permanent
  --actor ACTOR [--note NOTA] [--json]
MCP: curation_review, curation_decide
```

Review exige un plan completo y digest vigente, publica páginas reanudables e
idempotentes y devuelve `task_id`, estado y `current_event_id`. Decide vuelve a
probar el digest, usa `expected_event_id` como CAS y conserva replay idempotente
del mismo evento. Ambas superficies declaran `read_only=false` porque escriben
estado ReviewTask, pero `effects.corpus=none`, `effects.external=none` y
`actions_authorized=false`.

**IMPLEMENTED — grant durable, sin efecto físico:**

```text
Neocortex curate authorize PLAN_ID --item-id ITEM_ID [--item-id ITEM_ID ...]
  --action trash|move|rename --actor ACTOR --expires-ns NS
  --max-bytes BYTES [--authorization-key KEY] [--json]
API/SDK: curation_authorize_payload
MCP: no disponible
```

Authorize exige plan completo y vigente, entre 1 y 100 items con ReviewTask
`resolved`, snapshots concordantes, expiración futura y un presupuesto que cubra
los bytes conocidos. Trash de duplicados exige `verification_mode=full_hash`;
move/rename exige destino absoluto distinto del origen. El grant es append-only
e idempotente por su key, no crea `file_actions`, no llama KIO y no toca corpus o
sistemas externos.

No hay tool MCP de autorización: aceptar un `actor` aportado por un agente no
resuelve autenticación del principal humano.

**TARGET — no implementado:** `apply → verify → reconcile` deberá revalidar
grant, expiración, digest, ReviewTask heads e identidades físicas inmediatamente
antes del efecto y conservar resultados conciliables.

No existe una interfaz de exportación ni un paquete ZIP de curación. `--json`
serializa la respuesta de una operación; no crea un artefacto durable.

## Separación de Code y desarrollo

Un repositorio, incluido NeoCortex, puede procesarse como corpus Code: lenguajes,
estructura, símbolos, relaciones y búsqueda. Las pruebas, lint, tipos y análisis
de seguridad pertenecen al desarrollo y se ejecutan con herramientas externas.

Dogfooding significa consultar NeoCortex como contenido y entregar evidencia a
Codex; no significa reintroducir una plataforma productiva que coordine sus
propios validadores.

## Criterio de completitud

La curación estará entregada cuando una muestra representativa pueda recorrer el
lifecycle completo, repetir sin rehacer trabajo, recuperarse de una interrupción
y dejar el plan, las decisiones, los efectos y el estado final consultables desde
los owners locales. Abstenerse de forma segura es necesario, pero no sustituye
resolver los casos soportados.

Las entregas y fechas se controlan en [ROADMAP_90_DAYS.md](ROADMAP_90_DAYS.md);
la arquitectura implementada se documenta en [ARCHITECTURE.md](ARCHITECTURE.md).
