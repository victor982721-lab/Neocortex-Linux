# AGENTS.md — neocortex/runtime

Hereda el [contrato raíz](../../AGENTS.md). Este ámbito añade sólo su contrato;
no amplía permisos ni sustituye la solicitud del usuario.

Controla presupuestos, lifecycle y progreso; no confundas concurrencia de agentes con admisión de workers ni quiescencia del sandbox con la del host.

Consulta la [ficha platform-state](../../docs/subprojects/platform-state.md) para ownership,
fronteras y validación. Lee otras fichas únicamente si el cambio cruza su contrato;
un archivo conserva un escritor y Git/integración quedan con el coordinador.
