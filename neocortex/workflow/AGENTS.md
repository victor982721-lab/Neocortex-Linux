# AGENTS.md — neocortex/workflow

Hereda el [contrato raíz](../../AGENTS.md). Este ámbito añade sólo su contrato;
no amplía permisos ni sustituye la solicitud del usuario.

ReviewTask, AuthorizationGrant y file_action tienen autoridad y estados distintos; una decisión advisory no concede permiso para un efecto.

Consulta la [ficha curation-effects](../../docs/subprojects/curation-effects.md) para ownership,
fronteras y validación. Lee otras fichas únicamente si el cambio cruza su contrato;
un archivo conserva un escritor y Git/integración quedan con el coordinador.
