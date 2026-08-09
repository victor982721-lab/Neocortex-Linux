# NeoCortex — handoff operativo actual

> Actualizado: 2026-08-09. Este archivo conserva su nombre histórico únicamente
> para mantener una ruta conocida; todo plan anterior de autoanálisis quedó
> detenido por instrucción de Víctor.

## Preferencia operativa de Víctor

- GitHub debe conservar sólo `main` como rama de trabajo visible.
- No crear PR ni ramas para evoluciones ordinarias de este proyecto personal.
- Una mejora aceptada se integra con un commit atómico directo en `main`, una
  release del SHA exacto, verificación del launcher público y push directo a
  `origin/main`.
- No usar `--apply` ni `--organization-apply` en Linux.

## Prioridad vigente

NeoCortex debe producir resultados utilizables sobre la información personal.
La tarea activa es indexar ZIP y ZIP anidados, distinguirlos inequívocamente de
archivos físicos y usar después esos resultados para ayudar a Víctor a organizar
su información.

La procedencia pública de un miembro anidado usa esta forma:

```text
contenedor.zip!/subcarpeta/otro.zip!/documento.txt
```

Archive y Knowledge deben declarar `location=archive_member inside_zip=1`,
además de contenedor, miembro, cadena y profundidad. Un hit normal de Knowledge
debe declarar `location=physical inside_zip=0`.

## Contrato de la implementación

- Ruta integrada `archive`, incluida en `--all` y seleccionable de forma
  independiente.
- Recorrido recursivo sin extraer miembros al filesystem.
- Texto consultable para texto/código, HTML/XML, PDF con texto nativo,
  DOCX/XLSX/PPTX, ODT/ODS/ODP y EPUB; los demás formatos permanecen visibles
  como metadatos.
- Estado `archive.sqlite3` schema 1, caché por identidad, refresh de ruta,
  reconciliación de cambios y poda sólo en corridas completas no filtradas.
- Límites predeterminados: profundidad 5, 20 000 miembros visibles, directorio
  central 32 MiB, 64 MiB por miembro, 512 MiB expandidos, 20 millones de
  caracteres y ratio de compresión 200.
- Rechazo o abstención ante traversal/rutas absolutas, nombres ambiguos,
  duplicados, cifrado, symlinks/especiales, compresión no soportada, ZIP
  corrupto o límites excedidos.
- Consultas read-only `--archive-status`, `--archive-search` y `--archive-list`;
  integración lexical con Knowledge como owner aditivo sólo cuando la base
  Archive existe.

## Próximos pasos, en orden

1. Terminar pruebas focales, Ruff/format, Mypy y Pyright sobre el cambio.
2. Validar 20–50 fixtures aislados mediante el comando público, incluida una
   cadena de al menos tres ZIP, búsqueda, distinción físico/ZIP y replay de
   caché.
3. Crear un solo commit atómico en `main` y construir/activar una release Linux
   del SHA exacto mediante `tools/release_linux.py`.
4. Ejecutar un preflight read-only del corpus real y un piloto autorizado de
   hasta 20–50 ZIP durante un máximo de 10–15 minutos, sin mutar archivos.
5. Si el piloto es útil y seguro, hacer push directo de `main`, comprobar CI de
   push y dejar `HEAD`, `origin/main` y la release exacta alineados.
6. Presentar a Víctor resultados concretos de búsqueda para comenzar la
   organización asistida; ningún score o inferencia autoriza movimientos.

## Criterio de cierre

No declarar la capacidad terminada sólo por código o pruebas. Deben coincidir
el SHA de `main`, la release activa y el launcher `Neocortex`; el piloto debe
mostrar miembros anidados consultables, rutas explícitas, replay de caché,
originales intactos y ningún estado incompatible. Cualquier limitación restante
se informa con precisión.
