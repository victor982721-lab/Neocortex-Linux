# AGENTS.md — neocortex/persistence

Hereda el [contrato raíz](../../AGENTS.md). Este ámbito añade sólo su contrato;
no amplía permisos ni sustituye la solicitud del usuario.

Preserva el kernel de lectura, publicación y fences; cada owner conserva schema y migraciones, y ningún lector ordinario sustituye el contrato SQLite.

Consulta la [ficha platform-state](../../docs/subprojects/platform-state.md) para ownership,
fronteras y validación. Lee otras fichas únicamente si el cambio cruza su contrato;
un archivo conserva un escritor y Git/integración quedan con el coordinador.
