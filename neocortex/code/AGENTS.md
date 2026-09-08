# AGENTS.md — neocortex/code

Hereda el [contrato raíz](../../AGENTS.md). Este ámbito añade sólo su contrato;
no amplía permisos ni sustituye la solicitud del usuario.

Code trata código como contenido; no añade autoauditoría, ejecución del corpus, proveedores ni agregadores de validación. Preserva DDL histórico como dato de migración.

Consulta la [ficha code-content](../../docs/subprojects/code-content.md) para ownership,
fronteras y validación. Lee otras fichas únicamente si el cambio cruza su contrato;
un archivo conserva un escritor y Git/integración quedan con el coordinador.
