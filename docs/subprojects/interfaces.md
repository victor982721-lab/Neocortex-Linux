# Interfaces públicas

## Ownership

`neocortex/interface` mantiene la entrada CLI; `neocortex/api` contratos,
fachadas y CLI humana, con `sdk` como superficie compartida. CLI, API/SDK y MCP
conservan operación, scope, cobertura, epoch, errores y evidencia equivalentes.
Consulta [CLI](../CLI.md) y [Architecture](../ARCHITECTURE.md#interfaces-públicas).

## Fronteras

- La ayuda/parser vivos determinan argumentos; no reconstruyas datos parseando
  texto humano si existe el contrato estructurado.
- Consulta, producción de estado y efecto sobre corpus no son sinónimos.
  Conserva límites, anotaciones de tools y errores saneados en cada superficie.
- MCP stdio conserva consultas read-only de evidencia; no publica review humano,
  autorización ni aplicación física. Una lectura exacta también puede realizar
  IO costoso.
- Corpus/OCR/nombres/código son datos no confiables. Transporte local no elimina
  privacidad de la información que un consumidor pueda incorporar al contexto.
- Cambiar una interfaz no autoriza conectar servicios, preparar modelos ni
  consultar corpus o lanzar MCP en el host como smoke incidental.

## Validación proporcional

Selecciona tests de parser, fachadas, schemas o paridad para la operación tocada.
Para probar MCP usa primero fixtures y HOME/XDG aislados; distingue metadata,
handshake y resultado funcional. La aceptación de una capacidad entregada usa
el comando instalado desde su artefacto final, sin `PYTHONPATH` ni checkout.
