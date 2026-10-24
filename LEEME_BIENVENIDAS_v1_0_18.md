# MAXIMO CONTROL GROUP — v1.0.18-candidate.1

Candidata del 24/10/2026. Base exclusiva: R028 v1.0.17, commit
660046d99aa312bcae6a54307009a3afc3362f15. No desplegada ni aprobada.

## Funcionamiento

Cada uno de los 12 grupos está vinculado a su bot en bienvenidas_grupos.json,
con IDs de grupo contrastados con la base real. Por indicación del usuario,
todos los mensajes de ese bot en su grupo se consideran bienvenidas. No se
necesitan firmas de slogans. Se vincula el ID numérico del emisor al observar
por primera vez su username oficial y después se comprueba el ID persistido.
No se adivina la identidad de administradores anónimos por su nombre visible.

Cuando llega otra bienvenida se conserva el nuevo mensaje y se elimina el
anterior de ese grupo. Video con pie de texto es un mensaje; álbumes se tratan
como un bloque. Duplicados y ediciones no reactivan saludos anteriores. Un
bloque antiguo recibido tarde se marca para eliminación. La referencia y los
borrados pendientes sobreviven a reinicios. Los fallos se reintentan; los
no eliminables aparecen en el módulo de raíz y en el registro del servidor.
No cambia automáticamente la duración del saludo más reciente.

Una ficha principal por user_id se publica en el grupo -1004303379365, tema 3.
Tiene identidad, perfil, ID, username si existe, Premium/idioma si se reciben,
primer ingreso registrado y último movimiento. Un mosaico de 12 botones
muestra dentro/fuera/sin verificar. Cada botón abre los detalles en privado
para el administrador: primer y último ingreso, última salida, reingresos y
última comprobación. El historial completo es paginado y distingue ingresos,
salidas y expulsiones cuando Telegram lo indica. No se almacena un teléfono
ni se inventan fechas que Telegram no entregó.

Los eventos proceden de chat_member, con la hora real de Telegram, una clave
por update_id y otra hora de recepción. Los eventos repetidos no duplican
registros; entradas/salidas/reingresos reales rápidos sí se conservan. Los
cambios de permisos sin cambio de pertenencia no crean entradas ni salidas.
Eventos atrasados no sustituyen un estado más reciente. Se consultan los 12
estados del usuario mediante una cola; un evento nuevo recibido durante la
consulta tiene prioridad. Errores de consulta no se convierten en ausencia.

La ficha se edita en lugar de crear otra en cada movimiento. Las ráfagas se
consolidan y las publicaciones se limitan a una cada 3,2 segundos. La cola de
verificación, publicaciones y eliminaciones queda en SQLite. Se respeta
RetryAfter. Una ficha eliminada se recrea al detectar el error de Telegram.

El grupo privado es un destino de registro, no un decimotercer grupo obligatorio.
Acceso: /orma → CENTRO DE CONTROL DE RAÍZ → BIENVENIDAS Y PASAPORTES.
Para consultar un usuario conocido, el administrador puede escribir en privado
/pasaporte seguido del ID. Los detalles y controles usan la autorización
administrativa existente. El administrador debe haber iniciado /start en Máximo
para recibir los detalles privados.

## Datos existentes

Se agregan únicamente tablas con prefijo bienvenida_. Se importan identidades
y movimientos oficiales una sola vez, sin reescribir las tablas antiguas.
No se publican miles de fichas históricas al arrancar: se publican cuando el
usuario tiene un movimiento nuevo o el administrador consulta su expediente.
Los estados históricos quedan sin verificar hasta recibir un evento actual o
consultarlos. La importación conserva el origen histórico de sus fechas, que
en R028 representan la hora registrada por el desarrollo anterior.

## Condiciones para la prueba real

Máximo necesita ser administrador en los 12 grupos, permiso para borrar
mensajes y recepción Bot-to-Bot habilitada en BotFather. También debe estar
agregado al nuevo grupo privado y poder publicar en el tema 3. La configuración
real de BotFather, permisos y existencia del tema no se pueden verificar con
el archivo de respaldo: deben comprobarse durante la prueba de JustRunMy.

La limpieza automática abarca mensajes observados desde la activación.
Bot API no ofrece un barrido arbitrario del historial. Los saludos anteriores
que no estén registrados requieren limpieza inicial manual. Telegram impide
borrar por Bot API mensajes de más de 48 horas; se informa el fallo, sin
prometer su borrado ni borrar otros mensajes. Esto también puede impedir
sustituir por borrado una bienvenida muy antigua tras un período sin ingresos.
No se ha implementado caducidad automática porque el usuario pidió conservar
la última bienvenida.

Se conserva la regla 12/12, publicidad, /orma, videos originales (que los envían
los otros bots), bots exentos y reglas de moderación. No se inicia una segunda
sesión de recepción de los bots de bienvenida. No se modifican credenciales.

## Validación local

18 pruebas con llamadas simuladas a Telegram, todas aprobadas. Cubren los
12 grupos, identidad del emisor, humanos y anonimato, duplicados, reinicios,
álbumes, mensajes atrasados, 14 llegadas concurrentes, errores de borrado,
plazo de 48 h, entradas/salidas/reingresos en el mismo segundo, cambios de
permisos, un usuario en los 12 grupos, mosaico de 12 botones, edición de ficha,
fallos de envío, revisiones concurrentes, consultas fallidas, carreras entre
consulta y evento, importación histórica, control de acceso y límites de API.

Prueba adicional sobre una copia real del respaldo: 2046 identidades y 4419
movimientos de los 12 grupos importados (los 3 del laboratorio quedan fuera).
Las 18 tablas originales conservan exactamente sus filas, y PRAGMA integrity_check
devuelve ok. Repetir initialize no duplica la importación ni publica fichas.
La base original conserva el SHA-256 declarado en manifest.json.

Comparación estructural de las funciones antiguas: solo cambia main (integración)
y teclado_centro_control_raiz (botón). Todas las funciones previas de moderación,
membresía, publicidad, administración y control mantienen su estructura.
La sintaxis de todos los módulos Python fue verificada.

Estas pruebas no arrancaron los bots ni usaron Telegram. El entorno local no
incluye python-telegram-bot; se usaron sustitutos de la API y un bucle asíncrono
sin sockets para probar la lógica. La prueba real de recepción y permisos queda
pendiente en JustRunMy. No se declara esta versión aprobada.

## Contenido del paquete y despliegue

Código completo de la candidata y esta documentación. No incluye base de datos,
.env, .git ni secretos nuevos. Conservar DATA_DIR, BOT_TOKEN, UNION_BOT_TOKEN y
las demás variables existentes. No reemplazar maximo_control.db con una base
vacía. Las tablas nuevas se crean automáticamente en la base persistente.

Primero preparar un checkout con el historial correcto de MaximoControlGroup,
confirmar el commit base y copiar solo los cambios de esta candidata. Crear un
commit candidato y desplegar únicamente en JustRunMy. Esperar pruebas reales y
aprobación expresa antes de GitHub, sincronización aprobada y respaldo en Oracle.
Los comandos se entregan de uno en uno según el procedimiento del usuario.

Archivos nuevos: bienvenidas.py, bienvenidas_grupos.json y este documento.
Archivos modificados: bot.py, VERSION y VERSION_INFO.json.

Referencias oficiales:
https://core.telegram.org/bots/features#bot-to-bot-communication
https://core.telegram.org/bots/api#deletemessage
https://core.telegram.org/bots/api#chatmemberupdated
