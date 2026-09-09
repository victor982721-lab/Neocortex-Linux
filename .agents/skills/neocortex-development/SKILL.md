---
name: neocortex-development
description: Implementar o corregir una capacidad solicitada de NeoCortex con ruteo por dominio, ownership disjunto y validación local proporcional. No activa auditorías generales ni instalación por una consulta.
---

# Desarrollo acotado de NeoCortex

1. Lee AGENTS raíz y selecciona sólo el dominio afectado en
   [SUBPROJECTS](../../../docs/SUBPROJECTS.md). Usa el CURRENT y SSOT vigentes
   para retomar, sin convertir incidentes históricos en tareas nuevas.
2. Inspecciona Git y actividad de writers sin refrescar ni mutar fuentes durante
   una corrida cercada. Define resultado, ownership y regresión mínima; si hay
   frentes realmente independientes, delega sin compartir escrituras.
3. Implementa el cambio pedido en el flujo existente y usa herramientas de
   validación individual conforme a [desarrollo](../../../docs/subprojects/development-release.md).
   Aísla fixtures/estado; ni corpus real, proveedores, modelos ni gate agregador.
   Si usas fuente temporal, sigue la [receta de ejecución](../../../docs/subprojects/development-release.md#validar-una-fuente-temporal):
   cwd en esa fuente, tooling absoluto, HOME/XDG/settings privados y metadatos
   generados desde el mismo source, nunca copiados del checkout. Comprueba
   intérprete/root, colección o cobertura no vacía y un foco antes de ampliar;
   no soluciones rutas erróneas abriendo HOME ni descartes errores de tipos.
4. Integra y publica main según autorización permanente, preservando trabajo
   ajeno. La raíz verifica remoto/árbol y actualiza registros; reactivar un agente
   inactivo requiere `followup_task`, no sólo enviar un mensaje.

No conviertas código publicado en release instalada. Si la tarea sólo pide
información o documentación, no arranques una auditoría, ingesta ni instalación.
Goals y permisos siguen sus contratos superiores, no una macro de esta skill.
