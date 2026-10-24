# Registro exclusivo en su grupo — candidata 5

Base desplegada: candidata 3, commit 6abdbb7. Este parche es acumulado e incluye
las correcciones de candidata 4; no es necesario aplicarla por separado.

## Separación de interfaces

Todos los botones de fichas (grupos, movimientos, identidad, verificación y volver
a ficha principal) actúan exclusivamente en INGRESO DE USUARIOS A GRUPOS,
chat_id -1004303379365. Las vistas se editan en el mismo mensaje de la ficha,
sin enviar un detalle adicional al privado ni abrir el chat de Máximo.

La selección de vista se guarda por usuario en bienvenida_vistas. Si llega
un ingreso/salida mientras se consulta un grupo o una página del historial,
se actualiza esa vista. FICHA PRINCIPAL permite volver al resumen.
Los controles mantienen la autorización administrativa existente y verifican
que el botón pertenezca a la ficha activa de ese usuario en el destino.

Se retira el comando privado /pasaporte y el botón de registro que se había
agregado al centro de raíz. Se restaura el teclado original del centro de raíz
y el título FICHAS Y PERMISOS del control. Los errores del registro quedan en
sus tablas y en el log del servidor, sin enviar avisos al chat privado.
Los botones de registro que queden en mensajes privados antiguos no abren
ni editan nada: muestran una indicación de que el registro es exclusivo del grupo.
No se eliminan ni alteran las fichas originales de control de usuarios.

## Incluye lo pendiente de candidata 4

ID y USER explícitos; fechas de ingreso y salida visibles por cada grupo;
publicación sin enlaces tg://user?id que puedan ser bloqueados por privacidad;
filtro persistente de ingresos del período vivo y seguimiento de sus salidas
y reingresos. Se conserva la fecha del primer arranque efectivo del registro,
no se reinicia el período al actualizar. Los ingresos válidos de ese período
se conservan. No se publican miembros antiguos solo por consultar su pertenencia.

Las fichas válidas existentes se actualizan automáticamente una vez para usar
los nuevos botones y fechas. Se retiran únicamente fichas publicadas por este
módulo que no correspondan a un ingreso del período vivo. No se tocan los
saludos de bienvenida de otros bots. La limpieza de saludos sigue desactivada.

## Validación

26 pruebas locales aprobadas. Además de las comprobaciones anteriores de datos,
fechas, deduplicación, empaquetado, privacidad, cola y registro vivo, se verificó:
- Pulsar grupos, historial, identidad y ficha principal edita la misma ficha.
- No aumenta el número de mensajes enviados ni se envían detalles a un privado.
- Un evento posterior actualiza en el mismo grupo la vista elegida.
- Botones privados antiguos no alteran las pantallas del control.
- Los errores de destino/publicación no envían mensajes privados.
- El comando privado de registro ya no está registrado.
- Todas las funciones de la base original conservan su estructura excepto main,
  donde se integra el observador y el trabajador. El teclado de raíz es original.

Las API de Telegram se simularon. La prueba real de la candidata 5 queda
pendiente en JustRunMy. No se publica en GitHub ni Oracle sin aprobación.

## Aplicación

El parche contiene bienvenidas.py, bot.py, VERSION, VERSION_INFO.json y este
archivo. Se aplica sobre la carpeta de prueba actual. No incluye ni reemplaza
la base persistente, no cambia credenciales, publicidad ni reglas 12/12.
Las vistas y marcas de actualización se crean automáticamente en tablas propias.

Los mensajes privados antiguos ya enviados siguen formando parte del historial
del chat. Esta corrección impide nuevos envíos del registro a ese privado.
