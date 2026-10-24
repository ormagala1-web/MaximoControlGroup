# Candidata 6 — ficha individual viva y control individual

Base: candidata 5, commit 9de0667. Pendiente de despliegue y prueba en JustRunMy.

## Una ficha por usuario

Se conserva una sola referencia canónica por user_id para toda su actividad en
los 12 grupos. Las fechas e historial permanecen en la base a lo largo de días,
reinicios y reingresos. No se crea una ficha independiente por grupo.

Un nuevo ingreso, salida o reingreso desplaza la ficha al final del grupo de
registro: se envía la ficha actualizada y, solo después de confirmar el envío,
se sustituye la referencia y se retira la anterior. Si el envío falla, la ficha
anterior sigue visible y se mantiene el trabajo pendiente. Un evento repetido,
una verificación o la consulta de un botón no desplazan la ficha. Un evento
atrasado que no sea la última actividad no se presenta como actividad más reciente.

Telegram no permite borrar por Bot API ciertos mensajes antiguos, incluidos los
que superan 48 horas. Si impide retirar la ficha anterior, se intenta reemplazar
su contenido por una referencia a la ficha vigente, sin botones activos. Así solo
queda una ficha completa activa por usuario. Si tampoco permite editarla, queda
el error en la cola para revisión: no se promete un borrado que Telegram rechaza.

## Ficha estable

Nombre, ID, USER, estado general y última actividad permanecen en todas las vistas.
Debajo se muestran el grupo seleccionado, su Chat ID, estado, primer y último
ingreso, última salida, cantidad de reingresos y última comprobación.
El selector de 12 grupos cambia solamente esa sección. MOVIMIENTOS añade el
historial paginado manteniendo la ficha y sus fechas; FICHA PRINCIPAL permite
volver al detalle normal. Identidad no sustituye toda la ficha por otro panel.
Un movimiento nuevo devuelve la ficha al resumen del grupo de ese movimiento.

## Botones solicitados

ABRIR PERFIL utiliza el enlace público https://t.me/username cuando existe.
Para usuarios sin username público se conserva IDENTIDAD, que muestra ID/USER
sin ocultar la ficha ni forzar enlaces restringidos por privacidad.

CONTROL INDIVIDUAL /ORMA abre expresamente Máximo con un enlace /start que
identifica usuario y grupo seleccionado. Reutiliza guardar_captura_orma y
mostrar_ficha_orma_privada existentes; no crea otro sistema de permisos/control.
Solo los administradores autorizados pueden abrir el control. La captura queda
vinculada al administrador que abrió el enlace y al Chat ID real seleccionado.
Un movimiento de membresía no tiene mensaje humano de origen, por lo que se
registra mensaje_origen_id=0: no se usa el ID del registro como ID de un mensaje
del grupo de origen. Se limpian borradores previos del mismo administrador al
abrir el nuevo control; los borradores de otros administradores se conservan.

ELIMINAR ESTE MENSAJE elimina únicamente la ficha del grupo de registro.
No elimina al usuario, su historial, sus permisos ni mensajes en otros grupos.
Una verificación no la recrea por sí sola. Un movimiento nuevo posterior vuelve
a mostrar su ficha actualizada. Si llega un movimiento mientras se borra la
ficha, queda pendiente y no se pierde. Un botón de una ficha sustituida no puede
eliminar la ficha vigente.

## Separación de interfaces

Grupos, fechas, movimientos, identidad y verificación siguen dentro de
INGRESO DE USUARIOS A GRUPOS. Solo CONTROL INDIVIDUAL /ORMA abre el control privado,
por petición explícita del usuario. No se envían avisos automáticos ni detalles
del registro al privado. La limpieza de saludos de bienvenida sigue desactivada.
El usuario conserva esa función en su otro desarrollo.

## Validación local

37 pruebas aprobadas. Comprueban registro por dos fuentes, deduplicación,
reingresos en distintos días, una única referencia por usuario, posición por
actividad, fallos de envío, reemplazo de una ficha que no se puede borrar,
consultas que no desplazan la ficha, cabecera y fechas permanentes, Chat ID,
control enlazado al usuario/grupo correctos, autorización y borrado exclusivo,
carreras entre borrado y eventos, enlaces de perfil sin tg:// por ID,
registro vivo, conservación del inicio, recuperación de fichas pendientes,
privacidad y empaquetado.

Las funciones previas de la base conservan su estructura excepto main (integración)
y maximo_start (reconoce el enlace explícito de CONTROL INDIVIDUAL). Se añade
abrir_control_desde_registro. No se modifican las reglas 12/12, publicidad ni
las acciones existentes de control o moderación.

Las API de Telegram fueron simuladas. La verificación real de la candidata 6
queda pendiente. No se ejecutó Docker ni se cambió el servicio desde este entorno.
El Dockerfile correcto de candidata 3 se conserva.

## Paquete

Parche sobre 9de0667: bienvenidas.py, bot.py, VERSION, VERSION_INFO.json y este
documento. No incluye ni reemplaza la base persistente, .env o credenciales.
Se agregan campos propios de entrega/visibilidad en bienvenida_fichas y una
referencia de sustitución en bienvenida_fichas_anteriores, sin modificar tablas
originales de control. Primero probar únicamente en JustRunMy y esperar aprobación
expresa antes de GitHub y Oracle.
