# AGENTS.md — neocortex/interface

Hereda el [contrato raíz](../../AGENTS.md). Este ámbito añade sólo su contrato;
no amplía permisos ni sustituye la solicitud del usuario.

Mantén la entrada CLI separada de las reglas de negocio; conserva paridad de
operación/cobertura y errores, sin parsear texto humano como API.

Consulta la [ficha interfaces](../../docs/subprojects/interfaces.md) para ownership,
fronteras y validación. Lee otras fichas únicamente si el cambio cruza su contrato;
un archivo conserva un escritor y Git/integración quedan con el coordinador.
