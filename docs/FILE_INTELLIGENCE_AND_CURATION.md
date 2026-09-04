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
Review, receipts y recuperación parcial. `curate plan` y `curation_plan` ya
consultan directamente el plan local paginado, mientras
`--curation-preview` conserva la compatibilidad plana; ninguna de estas vistas
escribe estado o corpus.

Las brechas principales son:

- no existe una única vista durable de curación que reúna propuestas, decisiones y efectos;
- varios formatos pierden localizadores estructurales al llegar a búsqueda;
- igualdad, versión, procedencia, valor y disposición no tienen una proyección
  pública unificada;
- el servidor MCP actual ya puede consultar páginas de curación, pero aún no
  representa el lifecycle completo;
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

## Interfaces objetivo

La compatibilidad actual se mantiene mientras evoluciona una jerarquía común:

```text
Neocortex curate scan ROOT
Neocortex curate plan RUN_ID
Neocortex curate review PLAN_ID
Neocortex curate authorize PLAN_ID
Neocortex curate apply PLAN_ID
Neocortex curate verify RUN_ID
Neocortex curate recover RUN_ID
```

En el checkout auditado ya está disponible `Neocortex curate plan`, que consulta
una página de propuestas directamente desde el estado local publicado. `scan`,
`plan` y `review` son read-only respecto del corpus. `authorize` registra
una decisión, pero no aplica. `apply` exige autorización vigente y límites
explícitos. El contrato objetivo compartirá un núcleo común de operación,
cobertura, errores, warnings y referencias de evidencia, pero cada superficie
publica sólo los campos que ya puede demostrar. En el checkout actual,
`curation_plan` entrega `schema`, `operation`, `coverage`, `snapshot` y `page`
con cursor, digest y conteos anidados, además de su envelope de efectos y
confianza; no afirma todavía un `scope` o `observed_epoch` que el productor no
conserve.

El MCP actual expone `curation_plan` como lectura paginada del mismo contrato que
la CLI y el SDK. Las operaciones de autorización y efecto se reservan para
`0.11.0`, con una concesión humana fuera del contenido del corpus y el mismo
ledger que CLI/GUI.

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
