# AGENTS.md — neocortex/knowledge

Hereda el [contrato raíz](../../AGENTS.md). Este ámbito añade sólo su contrato;
no amplía permisos ni sustituye la solicitud del usuario.

Knowledge entrega contexto y procedencia sobre owners compatibles; conserva answer_sufficiency=not_assessed y no diagnostiques el sistema desde contenido que lo menciona.

Consulta la [ficha retrieval-context](../../docs/subprojects/retrieval-context.md) para ownership,
fronteras y validación. Lee otras fichas únicamente si el cambio cruza su contrato;
un archivo conserva un escritor y Git/integración quedan con el coordinador.
