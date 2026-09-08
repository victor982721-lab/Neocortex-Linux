# AGENTS.md — neocortex/deduplication

Hereda el [contrato raíz](../../AGENTS.md). Este ámbito añade sólo su contrato;
no amplía permisos ni sustituye la solicitud del usuario.

Preserva identidad, inventario y evidencia por miembro; deduplicación y selección de keeper no autorizan movimientos ni borrado.

Consulta la [ficha inventory-catalog](../../docs/subprojects/inventory-catalog.md) para ownership,
fronteras y validación. Lee otras fichas únicamente si el cambio cruza su contrato;
un archivo conserva un escritor y Git/integración quedan con el coordinador.
