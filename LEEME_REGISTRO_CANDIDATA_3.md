# Auditoría de raíz y corrección — registro de usuarios, candidata 3

24/10/2026. Alcance vigente: únicamente registrar usuarios y sus movimientos en
el grupo privado. El usuario ya resolvió la eliminación de bienvenidas y pidió
que este desarrollo no se concentre en ella.

## Fallo raíz demostrado

Las candidatas 1 y 2 importan WelcomeService desde bienvenidas.py al iniciar
bot.py. Ese módulo necesita bienvenidas_grupos.json. Pero el Dockerfile heredado
solo copiaba requirements.txt, bot.py y agente_respaldo_remoto.py. Ninguna de
las dos primeras candidatas corrigió ese empaquetado.

Se reprodujo el contenido de la imagen siguiendo sus instrucciones COPY y se
intentó importar el módulo en un intérprete aislado: candidata 2 falla con
ModuleNotFoundError: No module named 'bienvenidas'. Este error se produce antes
del arranque de los bots, la creación de las tablas nuevas y la recepción de
ingresos. Construir la imagen y mostrar 'Application restarted' no prueba que
el proceso Python haya arrancado correctamente. Fue un error de la implementación
entregada, no una conclusión sobre la configuración que haya hecho el usuario.

La candidata 3 copia explícitamente el módulo y su JSON al contenedor. También
incluye una validación RUN durante el build: importa el módulo y exige 12 grupos
en el mapa. Con las instrucciones COPY corregidas, la reproducción aislada pasa.
No se realizó un docker build real ni se obtuvieron los logs privados de JustRunMy
localmente: el resultado demostrado es el fallo y su reparación en el contenido
de la imagen, no una afirmación de validación final del servicio desplegado.

## Otros problemas revisados

El enlace /c/4303379365/3 se interpretó inicialmente como un tema sin confirmar
si el grupo era foro. La revisión 2 ya distingue grupo normal y foro. La revisión
3 conserva esa corrección: en grupos normales no envía message_thread_id.

El registro previo solo recibía chat_member. La revisión 3 añade respaldo por
mensajes de servicio new_chat_members y left_chat_member. Ambos caminos se
consolidan para no duplicar el mismo movimiento; una entrada de varios usuarios
registra a cada uno. Entradas/salidas/reingresos reales en el mismo segundo se
conservan con identificadores distintos.

La primera ficha se publica antes de consultar los otros once grupos: no se
retiene todo el registro hasta terminar esas consultas. La ficha se actualiza
cuando llegan los resultados. Los estados no comprobados se muestran como tales.

Ahora la comprobación inicial del destino requiere un envío real: una sola
confirmación 'REGISTRO DE USUARIOS ACTIVO · CANDIDATA 3' por revisión. Si no puede
enviarla, queda el error y se avisa al administrador si su chat privado está
habilitado. La comprobación getChat por sí sola no se declara prueba de entrega.
Las fichas pendientes continúan persistidas para reintentar cuando exista acceso.

Se retiraron las llamadas de limpieza del observador y del trabajador. No se
procesan ni borran saludos de bienvenida. Ya no se necesita observar mensajes
de otros bots para registrar los ingresos o las salidas de usuarios.

Las reglas de publicidad, membresía 12/12, /orma y exclusiones conservan sus
funciones. La raíz añade solo el acceso REGISTRO DE USUARIOS. El destino privado
no es un grupo obligatorio adicional.

## Validación de esta revisión

18 pruebas aprobadas. Cubren: reproducción y corrección del empaquetado;
registro mediante chat_member; ingreso sin chat_member mediante mensaje de
servicio; deduplicación entre ambas fuentes; ingreso de varios usuarios; salidas;
reingresos rápidos; historial; un usuario en 12 grupos y una sola ficha; mosaico;
control de acceso; destino normal/foro; falta de acceso y recuperación; publicación
de confirmación una sola vez; errores de envío; conservación de revisión; consulta
con errores; prioridad de eventos reales; estados atrasados; importación idempotente;
preservación estructural de las funciones anteriores y limpieza desactivada.

Las API de Telegram se simularon en las pruebas. La validación del despliegue
requiere la respuesta del servicio real. No se usó getUpdates ni se consumió
la cola del bot desde herramientas de diagnóstico.

## Diez sitios oficiales consultados

1. Telegram Bot API — https://core.telegram.org/bots/api
   Condiciones de chat_member, mensajes de servicio, envío a temas y cola de updates.
2. Telegram, anuncio de comunicación entre bots — https://telegram.org/blog/ai-bot-revolution-11-new-features
   Confirma que la lectura entre bots es una función configurable. El registro
   de esta revisión no depende de esa función.
3. Documentación python-telegram-bot — https://docs.python-telegram-bot.org/en/stable/telegram.ext.application.html
   Orden y grupos de handlers y ciclo de ejecución de la aplicación. También se
   revisó https://docs.python-telegram-bot.org/en/stable/telegram.update.html:
   ALL_TYPES incluye CHAT_MEMBER; no se atribuye el fallo a una exclusión de ese tipo.
4. Repositorio oficial PTB — https://github.com/python-telegram-bot/python-telegram-bot
   Arquitectura asíncrona y backend HTTPX. No se afirmó haber inspeccionado un archivo
   de la etiqueta v22.8 que no pudo descargarse por esa ruta.
5. Documentación de Python — https://docs.python.org/3/library/asyncio-task.html
   Creación y conservación de tareas. El servicio mantiene una referencia a su tarea;
   no se encontró evidencia de pérdida por una tarea sin referencia.
6. Documentación de SQLite — https://www.sqlite.org/lockingv3.html
   Transacciones y bloqueo. Un fallo de acceso a Telegram no debe borrar el evento
   persistido. No se confundió un problema de despliegue con una corrupción SQLite.
7. Documentación HTTPX — https://www.python-httpx.org/advanced/timeouts/
   Diferencia entre tiempos de espera de conexión/lectura/escritura/pool.
8. Documentación Docker — https://docs.docker.com/reference/dockerfile/#copy
   COPY determina qué archivos entran en la imagen; CMD se ejecuta al iniciar.
   Es la referencia directamente relacionada con el fallo raíz reproducido.
9. Microsoft PowerShell — https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/invoke-restmethod
   Consulta REST para el diagnóstico independiente preparado anteriormente.
10. Distribución oficial PTB — https://pypi.org/project/python-telegram-bot/22.8/
    Compatibilidad Python 3.10+ y API 10.0 de la dependencia fijada. El Dockerfile
    usa Python 3.12: no se demuestra una incompatibilidad de versión Python.

## Qué queda por comprobar realmente

Que el contenedor con la corrección arranca y que aparece la confirmación de
entrega en el grupo privado. Para publicar, Máximo debe ser miembro del destino
con permiso suficiente; el programa no puede agregarse por sí mismo a un grupo
privado. Para estados chat_member debe ser administrador de los grupos de origen.
Estas condiciones no se dan por verificadas por una captura ni por un build exitoso.
La petición al /health del agente no pudo consultarse con la herramienta web:
no se presenta ese resultado como prueba de caída ni de buen funcionamiento.

## Paquete

Parche sobre la candidata 2 (9eaebe0). Cambia Dockerfile, bienvenidas.py, bot.py,
VERSION, VERSION_INFO.json y agrega esta documentación. No incluye base de datos,
.env ni cambios de credenciales. Las tablas persistentes existentes se conservan.
No publicado en GitHub ni aprobado. Primero desplegar la candidata en JustRunMy.
