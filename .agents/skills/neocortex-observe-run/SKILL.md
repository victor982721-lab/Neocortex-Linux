---
name: neocortex-observe-run
description: Observar o diagnosticar una corrida identificada de NeoCortex mediante progreso y procesos sin alterar owners cercados. No inicia --all, consultas SQLite ni nuevas corridas por defecto.
---

# Observar sin interferir

Identifica la corrida, unidad/invocation, wrapper, cgroup, transcript y rutas de
progreso existentes. Sigue [Operations](../../../docs/OPERATIONS.md) y el
[contrato de owners](../../../docs/subprojects/platform-state.md).

1. Prefiere stream estructurado y archivos de progreso previstos. Consulta el
   namespace real del host; una lista vacía en sandbox no acredita quiescencia.
   Si la observación del host no está disponible, informa estado terminal y
   quiescencia como no verificados, con el límite exacto y siguiente paso seguro.
2. No abras SQLite cercadas con clientes ordinarios, ni `mode=ro`, ni ejecutes
   status/health salvo garantía explícita de compatibilidad con la corrida activa.
3. Mantén checkout, SHA y entradas inmóviles mientras unidad, wrapper o cgroup
   estén activos. No borres WAL/SHM ni relajes fences para corregir observación.
4. Una interferencia invalida esa corrida como evidencia: registra causa y repite
   sólo la afectada cuando el alcance lo permita. No la conviertas en defecto del producto.
5. Después del estado terminal usa únicamente lectores/snapshots compatibles y
   acotados si la tarea necesita inspeccionar estado. Las pruebas del método usan
   fixtures/HOME/XDG aislados, nunca owners productivos como laboratorio.

Captura salidas grandes fuera del chat y entrega cambios relevantes, no sondeos
idénticos. Si se pide seguimiento futuro, usa la automatización autorizada con
silencio ante estado sin cambios; esta skill por sí sola no crea un monitor.
