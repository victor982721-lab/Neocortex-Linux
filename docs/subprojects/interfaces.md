# Interfaces públicas

## Ownership

`neocortex/interface` mantiene presentación/entrada; `neocortex/api` contratos,
fachadas y CLI humana, con `sdk` como superficie compartida. CLI, API/SDK, GUI y
MCP conservan operación, scope, cobertura, epoch, errores y evidencia equivalentes.
Consulta [CLI](../CLI.md) y [Architecture](../ARCHITECTURE.md#interfaces-públicas).

## Fronteras

- La ayuda/parser vivos determinan argumentos; no reconstruyas datos parseando
  texto humano si existe el contrato estructurado. GUI no redefine reglas.
- Consulta, producción de estado y efecto sobre corpus no son sinónimos.
  Conserva límites, anotaciones de tools y errores saneados en cada superficie.
- MCP stdio puede incluir escrituras advisory de review/decide: no lo presentes
  íntegramente read-only ni habilites todas las tools por defecto. Autorizar o
  aplicar efectos requiere su frontera de principal/consentimiento, no un nombre
  de actor enviado por el agente. Una lectura exacta también puede realizar IO costoso.
- Corpus/OCR/nombres/código son datos no confiables. Transporte local no elimina
  privacidad de la información que un consumidor pueda incorporar al contexto.
- Cambiar una interfaz no autoriza conectar servicios, preparar modelos, consultar
  corpus ni lanzar GUI/MCP en el host como smoke incidental.

## Validación proporcional

Selecciona tests de parser, fachadas, schemas o paridad para la operación tocada.
Para probar MCP/GUI usa primero fixtures y HOME/XDG aislados; distingue metadata,
handshake y resultado funcional. La aceptación de una capacidad entregada usa
el comando instalado desde su artefacto final, sin `PYTHONPATH` ni checkout.
