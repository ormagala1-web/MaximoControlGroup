# Corrección candidata 2 — Máximo v1.0.18

Base candidata: 5df0dea. Estado: prueba pendiente en JustRunMy.

Se corrige el envío al grupo privado -1004303379365: el número 3 del enlace
no se presupone como tema cuando el grupo no tiene foros. Antes de publicar
se consulta getChat y la pertenencia de Máximo. Si es grupo normal no se envía
message_thread_id. Si tiene temas se usa el tema 3; si no existe o está cerrado,
se conserva la ficha pendiente y se informa el error, sin enviarla a otro lugar.

El módulo comprueba el acceso al arrancar y cada minuto, incluso sin fichas en
cola. La pantalla de raíz fuerza otra comprobación y muestra la ruta detectada.
Si falta acceso, notifica al administrador configurado (si ha iniciado el bot)
sin repetir el mismo aviso en cada reintento. Los eventos recibidos siguen en
SQLite y las fichas pendientes se vuelven a intentar tras resolver el acceso.

La captura del grupo muestra un solo miembro. Es necesario agregar
@MaximoControlGroup_bot como administrador al grupo INGRESO DE USUARIOS A GRUPOS.
El programa no puede incorporarse por su cuenta a un grupo privado.

21 pruebas locales aprobadas: se mantienen las 18 pruebas anteriores y se
agregan comprobaciones de grupo normal, foro, ausencia de acceso, aviso único,
conservación de cola, recuperación al resolver permisos y rechazo sin rol de
administrador (varios de estos casos se agrupan en una prueba).
Las llamadas a Telegram se simularon: no prueban los permisos reales del servidor.

Esta corrección no cambia el registro de membresía, los grupos oficiales,
las exclusiones, los anuncios ni la limpieza de bienvenidas de candidata 1.
No incluye base de datos ni modifica credenciales. No está desplegada.

Instalación: aplicar el parche sobre la carpeta de prueba con commit 5df0dea,
revisar los cambios, crear otro commit candidato y desplegar solo en JustRunMy.
Agregar Máximo como administrador al destino y comprobar la ruta desde raíz.
Verificar el evento del usuario 7979183832 si quedó registrado; no dar por
recuperado un evento que el bot no haya recibido.
