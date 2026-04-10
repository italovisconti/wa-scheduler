# wa-scheduler Plan

Plan actualizado para un producto self-hosted, single-user y single-account.

## Producto real

- una sola cuenta de WhatsApp por instalacion
- uso personal
- sin multiusuario complejo
- sin multi-cuenta
- sin infraestructura extra innecesaria

## Stack elegido

- FastAPI `0.135.3`
- SQLAlchemy `2.0.49`
- Jinja2 `3.1.6`
- `wacli` `0.2.0`
- SQLite por defecto

## Decision de arquitectura

`wacli` usa lock exclusivo sobre su store. Por eso `wa-scheduler` se organiza en dos piezas simples:

1. Web app FastAPI para CRUD y panel.
2. Worker serial que es el unico proceso que ejecuta `wacli`.

La web no necesita tiempo real ni procesos distribuidos para el MVP.

## MVP implementado

- dashboard con healthcheck
- import de contactos y chats por jobs
- CRUD basico de plantillas
- CRUD basico de schedules
- recurrencia `one_time`, `daily`, `weekly`, `monthly`
- materializacion de runs
- cola de salida
- worker serial
- retry manual de runs fallidos
- adaptador `wacli` alineado con su salida JSON actual

## Flujo operativo

1. Autenticar `wacli` una vez con `wacli auth`.
2. Encolar jobs de sync.
3. Ejecutar el worker.
4. Crear schedules desde la web.
5. El worker materializa runs y envia mensajes.

## Siguientes pasos naturales

1. editar y borrar schedules/templates
2. uploads reales de adjuntos desde la web
3. mejor soporte de tags y filtros
4. empaquetado Docker Compose
5. backups y observabilidad
