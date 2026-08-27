# Guía para explicar PulseWatch en una entrevista

Esta guía permite presentar el mismo proyecto a alguien no técnico, a una persona de
recursos humanos o a un ingeniero backend sin cambiar la verdad ni memorizar palabras
complicadas.

## La explicación de 20 segundos

> PulseWatch vigila automáticamente otras APIs. Si una deja de responder o devuelve
> un resultado incorrecto, registra la caída, intenta recuperarse de errores
> temporales, abre un incidente y puede avisar a otro sistema. Cuando la API vuelve a
> funcionar, cierra el incidente y conserva el historial para medir confiabilidad.

## La explicación para una persona no técnica

Imagina un guardia que revisa varias puertas cada cierto tiempo:

- cada puerta es una API;
- el guardia es el scheduler;
- tocar la puerta es realizar un check;
- esperar y volver a tocar es la política de reintentos;
- anotar cuándo una puerta no abre es crear un incidente;
- avisar al encargado es enviar un webhook;
- el libro de novedades es la base de datos y la auditoría;
- el tablero de control es el dashboard.

El valor no está solo en decir “funciona” o “no funciona”. PulseWatch también permite
saber cuánto duró el problema, cuántas veces falló, cuál fue la última respuesta y
qué tan confiable fue el servicio durante un período.

## La explicación para un reclutador

> Construí una plataforma de monitoreo de APIs con Python y FastAPI para demostrar
> conocimientos de backend que van más allá de CRUD. Implementé procesamiento
> asíncrono, concurrencia limitada, reintentos selectivos, incidentes con apertura y
> recuperación, webhooks firmados, PostgreSQL/SQLite, migraciones Alembic,
> autenticación, métricas Prometheus, logs estructurados, Docker y CI. La suite tiene
> 101 pruebas deterministas y 97% de cobertura.

Lo importante es conectar cada tecnología con un problema:

| Tecnología | Problema que resuelve |
| --- | --- |
| FastAPI + Pydantic | Contrato HTTP claro y validación estricta |
| asyncio + HTTPX | Revisar varios servicios sin bloquear un hilo por cada uno |
| Semaphore | Evitar saturar el servidor o la red |
| SQLAlchemy async | Persistencia desacoplada y portable |
| PostgreSQL | Concurrencia y despliegue compartido en producción |
| SQLite | Inicio local sin infraestructura adicional |
| Alembic | Evolucionar el esquema de forma controlada |
| Prometheus | Exponer señales que una plataforma de operaciones puede recolectar |
| Docker | Empaquetar una ejecución reproducible y no root |
| GitHub Actions | Impedir que cambios inseguros o rotos lleguen a main |

## La explicación para un ingeniero

PulseWatch separa rutas, motor de checks, scheduler, notificaciones y repositorio.
Cada servicio guarda su próxima ejecución. El worker selecciona únicamente servicios
activos cuyo `next_check_at` ya venció y ejecuta el lote con concurrencia acotada.

Para cada endpoint:

1. valida que el destino no sea una dirección local o reservada, salvo autorización;
2. ejecuta `GET` o `HEAD` con timeout individual;
3. reintenta timeouts, errores de red, `429` y `5xx`;
4. evita reintentar errores deterministas como un `404` inesperado;
5. persiste el resultado y actualiza el incidente en una transacción;
6. emite una transición solo al abrir o resolver el incidente;
7. firma el webhook con HMAC-SHA256 y guarda el resultado de entrega.

El API puede escalar horizontalmente con el scheduler desactivado. El trabajo
programado vive en un worker separado y único. SQLAlchemy mantiene las consultas
portables; Alembic es la autoridad del esquema de producción.

## Cómo explicar una decisión importante

Usa esta estructura:

> Tenía el problema **X**. Elegí **Y** porque ofrece **Z**. La desventaja es **W**.
> Para este alcance fue razonable; el siguiente paso sería **Q**.

Ejemplo:

> Necesitaba revisar varios endpoints sin que uno lento detuviera a los demás. Elegí
> HTTPX asíncrono y un semáforo porque permiten concurrencia con un límite explícito.
> La desventaja es que un solo proceso sigue teniendo capacidad finita. Para una
> escala mucho mayor movería los checks a una cola distribuida con workers.

Esa forma demuestra criterio, no solo conocimiento de nombres.

## Demo de cinco minutos

### 1. Presentar el dashboard

Muestra:

- estado global;
- servicios programados;
- incidentes;
- historial de checks;
- estado del scheduler.

Di: “El dashboard consume el mismo API versionado que podría usar otra aplicación”.

### 2. Registrar un servicio

Explica los controles:

- estado HTTP esperado;
- timeout;
- número de reintentos;
- intervalo;
- etiquetas;
- activo/pausado.

### 3. Ejecutar un check correcto

Muestra el resultado y el resumen. Señala el request ID y las métricas, no solo el
color verde.

### 4. Simular una falla

Registra o cambia un endpoint de demostración para que responda un estado inesperado.
Muestra:

- intentos realizados;
- error normalizado;
- incidente abierto;
- contador de fallas;
- entrega de webhook, si está configurado.

### 5. Simular recuperación

Cuando el endpoint responda correctamente, muestra el incidente resuelto y explica
que no se crea uno nuevo para cada check fallido.

### 6. Cerrar con calidad

Ejecuta:

```bash
pytest --cov=app --cov-report=term-missing
ruff check .
mypy app
bandit -c pyproject.toml -r app
```

Di: “Los destinos HTTP se simulan en pruebas; la suite no depende de internet y por
eso es reproducible”.

## Preguntas probables y respuestas

### ¿Por qué no usaste solo un cron?

Un cron podría llamar un script, pero no modela fácilmente intervalos distintos por
servicio, estado del scheduler, ejecución manual, retención y observabilidad dentro
del mismo dominio. El worker mantiene esas decisiones explícitas.

### ¿Por qué SQLite y PostgreSQL?

SQLite permite clonar y ejecutar el proyecto sin levantar servicios adicionales.
PostgreSQL representa mejor un despliegue con API y worker separados. SQLAlchemy
comparte el dominio y CI prueba las migraciones sobre PostgreSQL.

### ¿Por qué reintentar algunos errores y otros no?

Un timeout o un `503` puede desaparecer en segundos; un `404` normalmente indica
una URL o expectativa equivocada. Reintentar todo aumenta carga y retrasa un
diagnóstico que probablemente no cambiará.

### ¿Por qué usar jitter?

Si muchas verificaciones fallan al mismo tiempo y todas esperan exactamente lo
mismo, vuelven a golpear el servicio simultáneamente. Un pequeño componente aleatorio
reduce ese efecto de rebaño.

### ¿Cómo evitas alertas repetidas?

El sistema avisa por transición: una vez al abrir el incidente y una vez al
resolverlo. Las fallas intermedias actualizan `failure_count`, pero no producen una
nueva apertura.

### ¿Qué pasa si el webhook falla?

El resultado del check y el incidente ya quedaron guardados. La notificación se
reintenta si el error es transitorio y su resultado se registra por separado. Una
falla de alertas no borra la evidencia del incidente.

### ¿Por qué un API key?

Es una solución pequeña y clara para un servicio de administración con un solo
operador. Uso comparación en tiempo constante y acepto header o Bearer. Para un
producto multiusuario agregaría identidad, sesiones/tokens, roles y permisos.

### ¿Cómo escala?

Las réplicas del API son stateless y comparten PostgreSQL. El scheduler actual debe
tener una sola réplica. Para aumentar la escala de checks incorporaría una cola,
workers horizontales y un lease/idempotencia distribuida.

### ¿Qué significa 97% de cobertura?

Significa que casi todas las instrucciones y decisiones de rama se ejecutan durante
las pruebas. No significa que no existan errores. Por eso también hay análisis
estático, pruebas de integración PostgreSQL, validación, revisión de seguridad y
límites documentados.

## Límites realistas: qué se mejoró y qué se conserva

Los límites no deben borrarse para parecer profesional. Deben distinguirse entre
“riesgo resuelto para este alcance” y “capacidad que pertenece a otra escala”.

| Tema | Estado actual | Por qué es razonable | Evolución real |
| --- | --- | --- | --- |
| Base de datos | SQLite local + PostgreSQL producción | Buen DX y persistencia compartida | Réplicas, pool y tuning |
| Scheduler | Worker separado, una réplica | Evita duplicados de forma simple | Lease distribuido o cola |
| Alertas | Webhook firmado y auditable | Se integra con Slack/email/PagerDuty | Canales y escalaciones nativas |
| Autenticación | API key administrativa | Apropiada para un operador | OIDC, usuarios y RBAC |
| SSRF | Bloqueos de URL/IP y opt-in privado | Detiene destinos obvios | Proxy de salida y allowlist |
| Métricas | Prometheus + logs + request ID | Operable en un entorno pequeño | Trazas OpenTelemetry |
| Disponibilidad | Uptime por checks | Fácil de entender y consultar | SLO por duración/error budget |
| Escala geográfica | Un punto de ejecución | Suficiente para el objetivo | Probes regionales |

La respuesta correcta en una entrevista no es “mi sistema soporta todo”. Es:

> Para el alcance actual elegí una solución simple, probada y observable. Conozco el
> punto exacto donde deja de ser suficiente y puedo describir la siguiente
> arquitectura.

## Errores que debes evitar al presentarlo

- No digas que es “igual a Datadog” o “listo para millones de servicios”.
- No recites tecnologías sin relacionarlas con decisiones.
- No llames incidente a cada fila fallida; explica el ciclo de vida.
- No digas que cobertura alta garantiza ausencia de bugs.
- No ocultes que el scheduler es single-worker.
- No actives destinos privados en una demo pública.
- No muestres una clave real en pantalla.

## Frase de cierre

> Este proyecto demuestra cómo diseño un backend alrededor de fallas reales:
> concurrencia controlada, persistencia consistente, seguridad, observabilidad,
> automatización, pruebas y una ruta de crecimiento honesta.
