# AGENTS.md — neocortex/api

Hereda el [contrato raíz](../../AGENTS.md). Este ámbito añade sólo su contrato;
no amplía permisos ni sustituye la solicitud del usuario.

Mantén fachadas tipadas, schemas cerrados y límites de autoridad; MCP review/decide escribe estado advisory y no equivale a read-only integral.

Consulta la [ficha interfaces](../../docs/subprojects/interfaces.md) para ownership,
fronteras y validación. Lee otras fichas únicamente si el cambio cruza su contrato;
un archivo conserva un escritor y Git/integración quedan con el coordinador.
