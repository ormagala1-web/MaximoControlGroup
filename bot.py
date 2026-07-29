import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
UNION_BOT_TOKEN = os.environ["UNION_BOT_TOKEN"]

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
DATABASE_PATH = os.path.join(DATA_DIR, "maximo_control.db")

TOTAL_GRUPOS_OBLIGATORIOS = 7
AVISO_MEMBRESIA_SEGUNDOS = 60
UNION_BOT_USERNAME = "UnionMembresia_bot"
GRUPO_PRUEBAS_USERNAME = "Orma_Pruebas"
ZONA_PERU = ZoneInfo("America/Lima")

GRUPOS_OFICIALES = [
    (1, "DISTRITO STREAMING UNIVERSAL 🌎🌍", "DistritoStreamingUniversal", "DistritoStreamingUniversal_Bot"),
    (2, "STREAMING DIGITAL PERUCHO 🇵🇪", "StreamingDigitalPerucho", "StreamingDigitalPerucho_bot"),
    (3, "PERÚ ENTRETENIMIENTO STREAMING 🇵🇪", "PeruEntretenimientoStreaming", "PeruEntretenimientoStreaming_Bot"),
    (4, "MUNDO CACHINERO STREAMING 🌎", "MundoCachineroStreaming", "MUCASTBOT"),
    (5, "🌎 UNIVERSO CIBERNÉTICO PERÚ 🇵🇪", "mundocibertetico", "UniversoCibertneticoPeru_bot"),
    (6, "💻 Metaverso Streaming Perú 🇵🇪", "metaversostreaminggo", "MetaversoPeru_bot"),
    (7, "🎭 MUNDO STREAMING PERÚ 🇵🇪", "mymundostreaming", "MundoStreamingPeru_bot"),
]

# Bots oficiales excluidos DE RAÍZ del control 7/7.
# Cualquier otro bot, usuario o administrador sí queda sujeto a la regla.
BOTS_OFICIALES_EXENTOS = {
    "distritostreaminguniversal_bot",
    "streamingdigitalperucho_bot",
    "peruentretenimientostreaming_bot",
    "mucastbot",
    "universocibertneticoperu_bot",
    "metaversoperu_bot",
    "mundostreamingperu_bot",
    "maximocontrolgroup_bot",
    "unionmembresia_bot",
}

MAXIMO_APP_REF = None
UNION_APP_REF = None

# Un solo aviso temporal por usuario y grupo.
# Clave: (chat_id, user_id) -> message_id
AVISOS_MEMBRESIA_ACTIVOS = {}

# Panel privado reutilizable para las capturas /orma.
PANELES_ORMA = {}
CAPTURAS_ORMA = {}

# Entradas temporales del panel de Control Publicitario.
# Clave: propietario_id -> {"captura_id": int, "campo": str}
ENTRADAS_CONTROL_PUBLICIDAD = {}

# Un solo aviso publicitario temporal por identidad y grupo.
AVISOS_PUBLICIDAD_ACTIVOS = {}

AVISO_PUBLICIDAD_SEGUNDOS = 30

# Tipos controlables. TEXTO puro continúa siendo libre.
TIPOS_PUBLICIDAD_CONTROLABLE = {
    "FOTO",
    "VIDEO",
    "GIF/ANIMACIÓN",
    "DOCUMENTO",
    "TEXTO + ENLACE",
    "CUSTOM EMOJI",
}


def conectar_db():
    conexion = sqlite3.connect(DATABASE_PATH)
    conexion.row_factory = sqlite3.Row
    return conexion


def columna_existe(conexion, tabla, columna):
    columnas = {
        fila["name"]
        for fila in conexion.execute(f"PRAGMA table_info({tabla})").fetchall()
    }
    return columna in columnas


def inicializar_base_datos():
    os.makedirs(DATA_DIR, exist_ok=True)

    with conectar_db() as conexion:
        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS usuarios_membresia (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                nombre TEXT,
                union_bot_iniciado INTEGER NOT NULL DEFAULT 0,
                fecha_primer_contacto TEXT NOT NULL,
                fecha_actualizacion TEXT NOT NULL
            )
            """
        )

        if not columna_existe(conexion, "usuarios_membresia", "union_panel_message_id"):
            conexion.execute(
                "ALTER TABLE usuarios_membresia ADD COLUMN union_panel_message_id INTEGER"
            )

        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS control_publicidad_identidades (
                identidad_tipo TEXT NOT NULL,
                identidad_id INTEGER NOT NULL,
                modo TEXT NOT NULL DEFAULT 'HEREDADO',
                separacion_segundos INTEGER,
                limite_hora INTEGER,
                limite_dia INTEGER,
                limite_semana INTEGER,
                limite_mes INTEGER,
                limite_anio INTEGER,
                controlar_foto INTEGER NOT NULL DEFAULT 1,
                controlar_video INTEGER NOT NULL DEFAULT 1,
                controlar_gif INTEGER NOT NULL DEFAULT 1,
                controlar_documento INTEGER NOT NULL DEFAULT 1,
                controlar_enlace INTEGER NOT NULL DEFAULT 1,
                controlar_custom_emoji INTEGER NOT NULL DEFAULT 1,
                fecha_actualizacion TEXT NOT NULL,
                PRIMARY KEY (identidad_tipo, identidad_id)
            )
            """
        )

        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS eventos_publicidad_control (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identidad_tipo TEXT NOT NULL,
                identidad_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                chat_username TEXT,
                chat_nombre TEXT,
                message_id INTEGER NOT NULL,
                tipo_contenido TEXT NOT NULL,
                decision TEXT NOT NULL,
                motivo TEXT,
                fecha_evento TEXT NOT NULL
            )
            """
        )

        conexion.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_eventos_publicidad_identidad_fecha
            ON eventos_publicidad_control (
                identidad_tipo,
                identidad_id,
                fecha_evento
            )
            """
        )

        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS actividad_grupo (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identidad_tipo TEXT NOT NULL,
                identidad_id INTEGER NOT NULL,
                username TEXT,
                nombre TEXT,
                es_bot INTEGER NOT NULL DEFAULT 0,
                chat_id INTEGER NOT NULL,
                chat_username TEXT,
                chat_nombre TEXT,
                message_id INTEGER NOT NULL,
                tipo_contenido TEXT NOT NULL,
                contiene_enlace INTEGER NOT NULL DEFAULT 0,
                fecha_evento TEXT NOT NULL,
                UNIQUE(chat_id, message_id, identidad_tipo, identidad_id)
            )
            """
        )

        conexion.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_actividad_identidad_fecha
            ON actividad_grupo (identidad_tipo, identidad_id, fecha_evento)
            """
        )

        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS movimientos_grupo (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                nombre TEXT,
                chat_id INTEGER NOT NULL,
                chat_username TEXT,
                chat_nombre TEXT,
                tipo_movimiento TEXT NOT NULL,
                fecha_evento TEXT NOT NULL
            )
            """
        )

        conexion.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_movimientos_usuario_fecha
            ON movimientos_grupo (user_id, fecha_evento)
            """
        )

        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS paneles_orma (
                propietario_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                fecha_actualizacion TEXT NOT NULL
            )
            """
        )

        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS capturas_orma (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                propietario_id INTEGER NOT NULL,
                objetivo_tipo TEXT NOT NULL,
                objetivo_id INTEGER NOT NULL,
                objetivo_username TEXT,
                objetivo_nombre TEXT,
                objetivo_es_bot INTEGER NOT NULL DEFAULT 0,
                chat_id INTEGER NOT NULL,
                chat_username TEXT,
                chat_nombre TEXT,
                mensaje_origen_id INTEGER NOT NULL,
                fecha_captura TEXT NOT NULL
            )
            """
        )

        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS grupos_obligatorios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                nombre TEXT NOT NULL,
                enlace TEXT NOT NULL,
                helpdesk_username TEXT,
                obligatorio INTEGER NOT NULL DEFAULT 1,
                activo INTEGER NOT NULL DEFAULT 1,
                orden INTEGER NOT NULL,
                fecha_creacion TEXT NOT NULL
            )
            """
        )

        ahora = datetime.now(timezone.utc).isoformat()

        for orden, nombre, username, helpdesk in GRUPOS_OFICIALES:
            conexion.execute(
                """
                INSERT INTO grupos_obligatorios (
                    username, nombre, enlace, helpdesk_username,
                    obligatorio, activo, orden, fecha_creacion
                )
                VALUES (?, ?, ?, ?, 1, 1, ?, ?)
                ON CONFLICT(username)
                DO UPDATE SET
                    nombre = excluded.nombre,
                    enlace = excluded.enlace,
                    helpdesk_username = excluded.helpdesk_username,
                    obligatorio = 1,
                    activo = 1,
                    orden = excluded.orden
                """,
                (
                    username,
                    nombre,
                    f"https://t.me/{username}",
                    helpdesk,
                    orden,
                    ahora,
                ),
            )

        conexion.commit()


def registrar_usuario_membresia(user, union_bot_iniciado=False):
    ahora = datetime.now(timezone.utc).isoformat()
    nombre = " ".join(
        parte for parte in [user.first_name, user.last_name] if parte
    ).strip()

    with conectar_db() as conexion:
        existente = conexion.execute(
            "SELECT * FROM usuarios_membresia WHERE user_id = ?",
            (user.id,),
        ).fetchone()

        if existente:
            iniciado = (
                1
                if union_bot_iniciado or bool(existente["union_bot_iniciado"])
                else 0
            )
            conexion.execute(
                """
                UPDATE usuarios_membresia
                SET username = ?,
                    nombre = ?,
                    union_bot_iniciado = ?,
                    fecha_actualizacion = ?
                WHERE user_id = ?
                """,
                (user.username, nombre, iniciado, ahora, user.id),
            )
        else:
            conexion.execute(
                """
                INSERT INTO usuarios_membresia (
                    user_id, username, nombre, union_bot_iniciado,
                    fecha_primer_contacto, fecha_actualizacion
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user.id,
                    user.username,
                    nombre,
                    1 if union_bot_iniciado else 0,
                    ahora,
                    ahora,
                ),
            )

        conexion.commit()


def obtener_usuario_membresia_db(user_id):
    with conectar_db() as conexion:
        return conexion.execute(
            "SELECT * FROM usuarios_membresia WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def guardar_union_panel_message_id(user_id, message_id):
    with conectar_db() as conexion:
        conexion.execute(
            """
            UPDATE usuarios_membresia
            SET union_panel_message_id = ?,
                fecha_actualizacion = ?
            WHERE user_id = ?
            """,
            (
                message_id,
                datetime.now(timezone.utc).isoformat(),
                user_id,
            ),
        )
        conexion.commit()


def obtener_grupos_obligatorios_db():
    with conectar_db() as conexion:
        return conexion.execute(
            """
            SELECT *
            FROM grupos_obligatorios
            WHERE obligatorio = 1
              AND activo = 1
            ORDER BY orden ASC
            """
        ).fetchall()


def estado_es_miembro(chat_member):
    estado = str(chat_member.status or "").lower()

    if estado in {"member", "administrator", "creator"}:
        return True

    if estado == "restricted":
        return bool(getattr(chat_member, "is_member", False))

    return False


async def obtener_estado_membresia_7de7(user_id):
    if MAXIMO_APP_REF is None:
        raise RuntimeError("MaximoControlGroup todavía no está inicializado.")

    grupos = obtener_grupos_obligatorios_db()
    faltantes = []
    completados = []
    errores = []

    for grupo in grupos:
        chat_ref = f"@{grupo['username']}"

        try:
            miembro = await MAXIMO_APP_REF.bot.get_chat_member(
                chat_id=chat_ref,
                user_id=user_id,
            )

            if estado_es_miembro(miembro):
                completados.append(grupo)
            else:
                faltantes.append(grupo)

        except TelegramError as error:
            logging.exception(
                "No se pudo comprobar user=%s en %s",
                user_id,
                chat_ref,
            )
            errores.append((grupo, str(error)))
            faltantes.append(grupo)

    return {
        "total": len(grupos),
        "completados": completados,
        "faltantes": faltantes,
        "errores": errores,
        "completo": (
            len(grupos) == TOTAL_GRUPOS_OBLIGATORIOS
            and not faltantes
        ),
    }


def texto_union_membresia(estado):
    total = estado["total"]
    completos = len(estado["completados"])

    if estado["completo"]:
        return (
            "✅ <b>MEMBRESÍA COMPLETA</b>\n\n"
            f"Progreso: <b>{completos}/{total}</b>\n\n"
            "Ya perteneces a todos los grupos oficiales requeridos.\n\n"
            "💬 Puedes participar con texto normal.\n"
            "🛡️ La publicidad quedará sujeta al Control Publicitario General "
            "cuando activemos ese módulo."
        )

    lineas = [
        "🔐 <b>MEMBRESÍA DE USUARIO</b>",
        "",
        f"Progreso: <b>{completos}/{total}</b>",
        "",
        "Te faltan estos grupos:",
        "",
    ]

    for grupo in estado["faltantes"]:
        lineas.append(f"❌ {grupo['nombre']}")

    lineas.extend(
        [
            "",
            "Usa los botones de abajo para ingresar.",
            "Luego pulsa <b>🔄 VERIFICAR MEMBRESÍA</b>.",
        ]
    )

    if estado["errores"]:
        lineas.extend(
            [
                "",
                "⚠️ Alguna comprobación no pudo confirmarse. "
                "Por seguridad permanece como pendiente.",
            ]
        )

    return "\n".join(lineas)


def teclado_union_membresia(estado):
    filas = []

    for grupo in estado["faltantes"]:
        filas.append(
            [
                InlineKeyboardButton(
                    f"➕ {grupo['nombre']}",
                    url=grupo["enlace"],
                )
            ]
        )

    filas.append(
        [
            InlineKeyboardButton(
                "🔄 VERIFICAR MEMBRESÍA",
                callback_data="union_verificar",
            )
        ]
    )

    return InlineKeyboardMarkup(filas)


async def mostrar_o_actualizar_panel_union(user_id):
    if UNION_APP_REF is None:
        return False

    estado = await obtener_estado_membresia_7de7(user_id)
    texto = texto_union_membresia(estado)
    teclado = teclado_union_membresia(estado)

    usuario_db = obtener_usuario_membresia_db(user_id)
    message_id = (
        usuario_db["union_panel_message_id"]
        if usuario_db
        else None
    )

    if message_id:
        try:
            await UNION_APP_REF.bot.edit_message_text(
                chat_id=user_id,
                message_id=message_id,
                text=texto,
                parse_mode="HTML",
                reply_markup=teclado,
            )
            return True

        except TelegramError as error:
            # Telegram devuelve "Message is not modified" cuando el panel
            # ya contiene exactamente el estado actual. Eso NO significa
            # que el panel esté perdido, así que no debemos crear otro.
            if "message is not modified" in str(error).lower():
                return True

            # Para cualquier otro error (por ejemplo, el usuario borró
            # manualmente el panel), se crea uno nuevo más abajo.
            logging.info(
                "No se pudo editar panel privado user=%s, message_id=%s: %s",
                user_id,
                message_id,
                error,
            )

    try:
        enviado = await UNION_APP_REF.bot.send_message(
            chat_id=user_id,
            text=texto,
            parse_mode="HTML",
            reply_markup=teclado,
        )
        guardar_union_panel_message_id(user_id, enviado.message_id)
        return True
    except TelegramError:
        logging.info(
            "No se pudo escribir por privado a user=%s; "
            "probablemente todavía no inició @UnionMembresia_bot.",
            user_id,
        )
        return False


async def eliminar_mensaje_despues(mensaje, segundos):
    try:
        await asyncio.sleep(segundos)
        await mensaje.delete()
    except (TelegramError, asyncio.CancelledError):
        pass


def username_usuario(user):
    return (getattr(user, "username", None) or "").lstrip("@").lower()


def es_bot_oficial_exento(user):
    return bool(
        getattr(user, "is_bot", False)
        and username_usuario(user) in BOTS_OFICIALES_EXENTOS
    )


def nombre_visible_usuario(user):
    nombre = " ".join(
        parte
        for parte in [
            getattr(user, "first_name", None),
            getattr(user, "last_name", None),
        ]
        if parte
    ).strip()

    return nombre or "Sin nombre visible"


def etiqueta_tipo_usuario(user):
    return "BOT" if getattr(user, "is_bot", False) else "USUARIO"


def etiqueta_rol_chat_member(chat_member):
    estado = str(getattr(chat_member, "status", "") or "").lower()

    mapa = {
        "creator": "Propietario",
        "administrator": "Administrador",
        "member": "Miembro",
        "restricted": "Restringido",
        "left": "Fuera del grupo",
        "kicked": "Expulsado",
    }

    return mapa.get(estado, estado or "Desconocido")


async def obtener_rol_en_grupo(chat_id, user_id):
    if MAXIMO_APP_REF is None:
        return "Desconocido"

    try:
        miembro = await MAXIMO_APP_REF.bot.get_chat_member(
            chat_id=chat_id,
            user_id=user_id,
        )
        return etiqueta_rol_chat_member(miembro)
    except TelegramError:
        return "Desconocido"


def username_chat(chat):
    return (getattr(chat, "username", None) or "").lstrip("@").lower()


def usernames_grupos_oficiales():
    return {username.lower() for _, _, username, _ in GRUPOS_OFICIALES}


def es_grupo_controlado(chat):
    username = username_chat(chat)
    return (
        username == GRUPO_PRUEBAS_USERNAME.lower()
        or username in usernames_grupos_oficiales()
    )


async def eliminar_aviso_membresia_programado(
    bot,
    chat_id,
    user_id,
    message_id,
    segundos,
):
    try:
        await asyncio.sleep(segundos)

        clave = (chat_id, user_id)
        actual = AVISOS_MEMBRESIA_ACTIVOS.get(clave)

        # Solo borra el aviso que siga siendo el vigente para ese usuario/grupo.
        if actual != message_id:
            return

        try:
            await bot.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )
        except TelegramError:
            pass

        AVISOS_MEMBRESIA_ACTIVOS.pop(clave, None)

    except asyncio.CancelledError:
        pass


async def mostrar_aviso_union_temporal(
    context,
    chat_id,
    usuario,
    estado,
):
    user_id = usuario.id
    clave = (chat_id, user_id)
    anterior_id = AVISOS_MEMBRESIA_ACTIVOS.get(clave)

    # Evita acumular avisos si insiste varias veces en el mismo grupo.
    if anterior_id:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=anterior_id,
            )
        except TelegramError:
            pass

    username = (
        f"@{usuario.username}"
        if getattr(usuario, "username", None)
        else "Sin @username"
    )
    nombre = nombre_visible_usuario(usuario)
    tipo = etiqueta_tipo_usuario(usuario)
    rol = await obtener_rol_en_grupo(chat_id, user_id)
    progreso = f"{len(estado['completados'])}/{estado['total']}"

    texto_aviso = (
        "🔒 <b>MEMBRESÍA PENDIENTE</b>\n\n"
        f"👤 <b>Nombre:</b> {nombre}\n"
        f"🔗 <b>Usuario:</b> {username}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"🏷️ <b>Tipo:</b> {tipo}\n"
        f"🛡️ <b>Rol:</b> {rol}\n"
        f"📊 <b>Membresía:</b> {progreso}\n\n"
        "Para participar debes completar tu membresía en los 7 grupos oficiales.\n\n"
        "Pulsa el botón para continuar de forma privada."
    )

    # Primero enviamos el aviso para obtener su message_id.
    aviso = await context.bot.send_message(
        chat_id=chat_id,
        text=texto_aviso,
        parse_mode="HTML",
    )

    # El payload identifica el aviso exacto que originó el acceso.
    payload = f"m_{chat_id}_{aviso.message_id}_{user_id}"
    enlace_union = (
        f"https://t.me/{UNION_BOT_USERNAME}?start={payload}"
    )

    await context.bot.edit_message_reply_markup(
        chat_id=chat_id,
        message_id=aviso.message_id,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔐 COMPLETAR MEMBRESÍA",
                        url=enlace_union,
                    )
                ]
            ]
        ),
    )

    AVISOS_MEMBRESIA_ACTIVOS[clave] = aviso.message_id

    asyncio.create_task(
        eliminar_aviso_membresia_programado(
            context.bot,
            chat_id,
            user_id,
            aviso.message_id,
            AVISO_MEMBRESIA_SEGUNDOS,
        )
    )



def obtener_control_identidad_db(identidad_tipo, identidad_id):
    ahora = datetime.now(timezone.utc).isoformat()

    with conectar_db() as conexion:
        fila = conexion.execute(
            """
            SELECT *
            FROM control_publicidad_identidades
            WHERE identidad_tipo = ?
              AND identidad_id = ?
            LIMIT 1
            """,
            (identidad_tipo, identidad_id),
        ).fetchone()

        if fila:
            return fila

        conexion.execute(
            """
            INSERT INTO control_publicidad_identidades (
                identidad_tipo,
                identidad_id,
                modo,
                fecha_actualizacion
            )
            VALUES (?, ?, 'HEREDADO', ?)
            """,
            (identidad_tipo, identidad_id, ahora),
        )
        conexion.commit()

        return conexion.execute(
            """
            SELECT *
            FROM control_publicidad_identidades
            WHERE identidad_tipo = ?
              AND identidad_id = ?
            """,
            (identidad_tipo, identidad_id),
        ).fetchone()


def actualizar_control_identidad_db(
    identidad_tipo,
    identidad_id,
    **campos,
):
    permitidos = {
        "modo",
        "separacion_segundos",
        "limite_hora",
        "limite_dia",
        "limite_semana",
        "limite_mes",
        "limite_anio",
        "controlar_foto",
        "controlar_video",
        "controlar_gif",
        "controlar_documento",
        "controlar_enlace",
        "controlar_custom_emoji",
    }

    datos = {
        clave: valor
        for clave, valor in campos.items()
        if clave in permitidos
    }

    if not datos:
        return False

    obtener_control_identidad_db(identidad_tipo, identidad_id)
    datos["fecha_actualizacion"] = datetime.now(timezone.utc).isoformat()

    partes = []
    valores = []

    for clave, valor in datos.items():
        partes.append(f"{clave} = ?")
        valores.append(valor)

    valores.extend([identidad_tipo, identidad_id])

    with conectar_db() as conexion:
        cursor = conexion.execute(
            f"""
            UPDATE control_publicidad_identidades
            SET {", ".join(partes)}
            WHERE identidad_tipo = ?
              AND identidad_id = ?
            """,
            valores,
        )
        conexion.commit()
        return cursor.rowcount > 0


def resetear_control_identidad_db(identidad_tipo, identidad_id):
    with conectar_db() as conexion:
        conexion.execute(
            """
            DELETE FROM control_publicidad_identidades
            WHERE identidad_tipo = ?
              AND identidad_id = ?
            """,
            (identidad_tipo, identidad_id),
        )
        conexion.commit()

    obtener_control_identidad_db(identidad_tipo, identidad_id)


def contiene_custom_emoji(mensaje):
    entidades = list(getattr(mensaje, "entities", None) or [])
    entidades += list(getattr(mensaje, "caption_entities", None) or [])

    for entidad in entidades:
        tipo = str(getattr(entidad, "type", "") or "").lower()
        if tipo == "custom_emoji":
            return True

    return False


def tipo_publicitario_mensaje(mensaje, usuario=None):
    # Bots externos: cualquier publicación se considera controlable.
    if usuario is not None and getattr(usuario, "is_bot", False):
        if es_bot_oficial_exento(usuario):
            return None
        tipo = clasificar_contenido_mensaje(mensaje)
        return tipo if tipo != "OTRO" else "BOT"

    tipo = clasificar_contenido_mensaje(mensaje)

    if tipo in {
        "FOTO",
        "VIDEO",
        "GIF/ANIMACIÓN",
        "DOCUMENTO",
        "TEXTO + ENLACE",
    }:
        return tipo

    if contiene_custom_emoji(mensaje):
        return "CUSTOM EMOJI"

    # Principio fundamental: texto puro normal siempre libre.
    return None


def tipo_habilitado_por_config(tipo, cfg):
    mapa = {
        "FOTO": "controlar_foto",
        "VIDEO": "controlar_video",
        "GIF/ANIMACIÓN": "controlar_gif",
        "DOCUMENTO": "controlar_documento",
        "TEXTO + ENLACE": "controlar_enlace",
        "CUSTOM EMOJI": "controlar_custom_emoji",
    }

    # Para bots externos, cualquier formato no reconocido específicamente
    # continúa bajo control.
    campo = mapa.get(tipo)
    if campo is None:
        return True

    return bool(cfg[campo])


def registrar_evento_publicidad_db(
    identidad_tipo,
    identidad_id,
    chat,
    message_id,
    tipo_contenido,
    decision,
    motivo=None,
):
    with conectar_db() as conexion:
        conexion.execute(
            """
            INSERT INTO eventos_publicidad_control (
                identidad_tipo,
                identidad_id,
                chat_id,
                chat_username,
                chat_nombre,
                message_id,
                tipo_contenido,
                decision,
                motivo,
                fecha_evento
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identidad_tipo,
                identidad_id,
                chat.id,
                getattr(chat, "username", None),
                getattr(chat, "title", None),
                message_id,
                tipo_contenido,
                decision,
                motivo,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conexion.commit()


def limites_periodos_publicidad():
    ahora = datetime.now(ZONA_PERU)
    inicio_hora = ahora - timedelta(hours=1)
    inicio_dia = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    inicio_semana = inicio_dia - timedelta(days=inicio_dia.weekday())
    inicio_mes = inicio_dia.replace(day=1)
    inicio_anio = inicio_dia.replace(month=1, day=1)

    def utc_iso(fecha):
        return fecha.astimezone(timezone.utc).isoformat()

    return {
        "hora": utc_iso(inicio_hora),
        "dia": utc_iso(inicio_dia),
        "semana": utc_iso(inicio_semana),
        "mes": utc_iso(inicio_mes),
        "anio": utc_iso(inicio_anio),
    }


def contar_publicidad_permitida_db(identidad_tipo, identidad_id, desde):
    with conectar_db() as conexion:
        fila = conexion.execute(
            """
            SELECT COUNT(*) AS total
            FROM eventos_publicidad_control
            WHERE identidad_tipo = ?
              AND identidad_id = ?
              AND decision = 'PERMITIDA'
              AND fecha_evento >= ?
            """,
            (identidad_tipo, identidad_id, desde),
        ).fetchone()

    return int(fila["total"] if fila else 0)


def ultima_publicidad_permitida_db(identidad_tipo, identidad_id):
    with conectar_db() as conexion:
        fila = conexion.execute(
            """
            SELECT fecha_evento
            FROM eventos_publicidad_control
            WHERE identidad_tipo = ?
              AND identidad_id = ?
              AND decision = 'PERMITIDA'
            ORDER BY id DESC
            LIMIT 1
            """,
            (identidad_tipo, identidad_id),
        ).fetchone()

    return fila["fecha_evento"] if fila else None


def resumen_uso_publicidad_db(identidad_tipo, identidad_id):
    limites = limites_periodos_publicidad()
    return {
        clave: contar_publicidad_permitida_db(
            identidad_tipo,
            identidad_id,
            inicio,
        )
        for clave, inicio in limites.items()
    }


def evaluar_control_publicidad(identidad_tipo, identidad_id, tipo_contenido):
    cfg = obtener_control_identidad_db(identidad_tipo, identidad_id)
    modo = str(cfg["modo"] or "HEREDADO").upper()

    if modo == "EXCLUIDO":
        return True, "EXCLUIDO DEL CONTROL", cfg, None

    if modo == "ILIMITADO":
        return True, "PUBLICIDAD ILIMITADA", cfg, None

    if modo == "BLOQUEADO":
        return False, "PUBLICIDAD BLOQUEADA", cfg, None

    if not tipo_habilitado_por_config(tipo_contenido, cfg):
        return True, f"TIPO {tipo_contenido} EXCLUIDO", cfg, None

    # HEREDADO queda preparado para el próximo bloque global.
    # Mientras no exista regla global, no impone límites individuales.
    if modo == "HEREDADO":
        return True, "HEREDADO · SIN REGLA GLOBAL ACTIVA TODAVÍA", cfg, None

    # PERSONALIZADO
    ahora = datetime.now(timezone.utc)

    separacion = cfg["separacion_segundos"]
    if separacion is not None and int(separacion) > 0:
        ultima = ultima_publicidad_permitida_db(identidad_tipo, identidad_id)
        if ultima:
            try:
                fecha_ultima = datetime.fromisoformat(ultima)
                if fecha_ultima.tzinfo is None:
                    fecha_ultima = fecha_ultima.replace(tzinfo=timezone.utc)
                disponible = fecha_ultima + timedelta(seconds=int(separacion))
                if ahora < disponible:
                    return (
                        False,
                        "SEPARACIÓN MÍNIMA NO CUMPLIDA",
                        cfg,
                        disponible,
                    )
            except (TypeError, ValueError):
                pass

    limites = limites_periodos_publicidad()
    campos = [
        ("hora", "limite_hora", "LÍMITE POR HORA"),
        ("dia", "limite_dia", "LÍMITE DIARIO"),
        ("semana", "limite_semana", "LÍMITE SEMANAL"),
        ("mes", "limite_mes", "LÍMITE MENSUAL"),
        ("anio", "limite_anio", "LÍMITE ANUAL"),
    ]

    for periodo, campo, etiqueta in campos:
        limite = cfg[campo]
        if limite is None:
            continue

        usados = contar_publicidad_permitida_db(
            identidad_tipo,
            identidad_id,
            limites[periodo],
        )

        if usados >= int(limite):
            return False, etiqueta, cfg, None

    return True, "DENTRO DE LOS LÍMITES", cfg, None


def texto_valor_limite(valor):
    return "SIN LÍMITE" if valor is None else str(valor)


def texto_separacion(segundos):
    if segundos is None:
        return "SIN SEPARACIÓN"

    segundos = int(segundos)

    if segundos % 3600 == 0 and segundos >= 3600:
        horas = segundos // 3600
        return f"{horas} h"

    if segundos % 60 == 0:
        return f"{segundos // 60} min"

    return f"{segundos} s"


def teclado_control_publicidad(captura_id, cfg):
    modo = str(cfg["modo"] or "HEREDADO").upper()

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"⚙️ MODO: {modo}",
                callback_data=f"orma_pub_modo:{captura_id}",
            )
        ],
        [
            InlineKeyboardButton(
                f"⏱ SEPARACIÓN: {texto_separacion(cfg['separacion_segundos'])}",
                callback_data=f"orma_pub_sep:{captura_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "🔢 LÍMITES",
                callback_data=f"orma_pub_limites:{captura_id}",
            ),
            InlineKeyboardButton(
                "🎛 TIPOS",
                callback_data=f"orma_pub_tipos:{captura_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "♻️ RESTAURAR HEREDADO",
                callback_data=f"orma_pub_reset:{captura_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ RETROCEDER",
                callback_data=f"orma_ficha:{captura_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "🏠 MENÚ PRINCIPAL",
                callback_data="orma_menu_principal",
            ),
            InlineKeyboardButton(
                "🗑 CERRAR",
                callback_data="orma_cerrar",
            ),
        ],
    ])


async def texto_control_publicidad(captura):
    cfg = obtener_control_identidad_db(
        captura["objetivo_tipo"],
        captura["objetivo_id"],
    )
    uso = resumen_uso_publicidad_db(
        captura["objetivo_tipo"],
        captura["objetivo_id"],
    )

    return (
        "📣 <b>CONTROL PUBLICITARIO INDIVIDUAL</b>\\n\\n"
        f"👤 <b>{captura['objetivo_nombre'] or 'Sin nombre'}</b>\\n"
        f"🆔 <code>{captura['objetivo_id']}</code>\\n\\n"
        f"⚙️ Modo: <b>{cfg['modo']}</b>\\n"
        f"⏱ Separación: <b>{texto_separacion(cfg['separacion_segundos'])}</b>\\n\\n"
        "🔢 <b>Límites personalizados</b>\\n"
        f"• Hora: <b>{texto_valor_limite(cfg['limite_hora'])}</b>\\n"
        f"• Día: <b>{texto_valor_limite(cfg['limite_dia'])}</b>\\n"
        f"• Semana: <b>{texto_valor_limite(cfg['limite_semana'])}</b>\\n"
        f"• Mes: <b>{texto_valor_limite(cfg['limite_mes'])}</b>\\n"
        f"• Año: <b>{texto_valor_limite(cfg['limite_anio'])}</b>\\n\\n"
        "📊 <b>Uso registrado</b>\\n"
        f"• Última hora: <b>{uso['hora']}</b>\\n"
        f"• Hoy: <b>{uso['dia']}</b>\\n"
        f"• Semana: <b>{uso['semana']}</b>\\n"
        f"• Mes: <b>{uso['mes']}</b>\\n"
        f"• Año: <b>{uso['anio']}</b>\\n\\n"
        "💬 El texto normal puro permanece libre."
    )


async def eliminar_aviso_publicidad_programado(
    bot,
    chat_id,
    identidad_id,
    message_id,
):
    try:
        await asyncio.sleep(AVISO_PUBLICIDAD_SEGUNDOS)
        clave = (chat_id, identidad_id)

        if AVISOS_PUBLICIDAD_ACTIVOS.get(clave) != message_id:
            return

        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass

        AVISOS_PUBLICIDAD_ACTIVOS.pop(clave, None)
    except asyncio.CancelledError:
        pass


async def mostrar_aviso_publicidad_temporal(
    context,
    chat,
    identidad_id,
    nombre,
    username,
    tipo_identidad,
    tipo_contenido,
    motivo,
    disponible=None,
):
    clave = (chat.id, identidad_id)
    anterior = AVISOS_PUBLICIDAD_ACTIVOS.get(clave)

    if anterior:
        try:
            await context.bot.delete_message(
                chat_id=chat.id,
                message_id=anterior,
            )
        except TelegramError:
            pass

    extra = ""
    if disponible is not None:
        extra = (
            "\\n⏳ Próxima disponibilidad: "
            f"<b>{formatear_fecha_peru(disponible.isoformat())}</b>"
        )

    user_text = f"@{username}" if username else "Sin @username"

    aviso = await context.bot.send_message(
        chat_id=chat.id,
        text=(
            "⛔ <b>PUBLICIDAD NO PERMITIDA</b>\\n\\n"
            f"👤 <b>Nombre:</b> {nombre}\\n"
            f"🔗 <b>Usuario:</b> {user_text}\\n"
            f"🆔 <b>ID:</b> <code>{identidad_id}</code>\\n"
            f"🏷️ <b>Tipo:</b> {tipo_identidad}\\n"
            f"📦 <b>Contenido:</b> {tipo_contenido}\\n"
            f"⚠️ <b>Motivo:</b> {motivo}"
            f"{extra}\\n\\n"
            "💬 Puedes continuar escribiendo texto normal."
        ),
        parse_mode="HTML",
    )

    AVISOS_PUBLICIDAD_ACTIVOS[clave] = aviso.message_id

    asyncio.create_task(
        eliminar_aviso_publicidad_programado(
            context.bot,
            chat.id,
            identidad_id,
            aviso.message_id,
        )
    )


def limites_periodos_actividad():
    ahora_local = datetime.now(ZONA_PERU)

    inicio_hora = ahora_local - __import__("datetime").timedelta(hours=1)
    inicio_dia = ahora_local.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    inicio_semana = inicio_dia - __import__("datetime").timedelta(
        days=inicio_dia.weekday()
    )
    inicio_mes = inicio_dia.replace(day=1)

    def utc_iso(fecha):
        return fecha.astimezone(timezone.utc).isoformat()

    return {
        "hora": utc_iso(inicio_hora),
        "dia": utc_iso(inicio_dia),
        "semana": utc_iso(inicio_semana),
        "mes": utc_iso(inicio_mes),
    }


def detectar_enlace_mensaje(mensaje):
    entidades = list(getattr(mensaje, "entities", None) or [])
    entidades += list(getattr(mensaje, "caption_entities", None) or [])

    for entidad in entidades:
        tipo = str(getattr(entidad, "type", "") or "").lower()
        if tipo in {"url", "text_link"}:
            return True

    contenido = " ".join(
        parte
        for parte in [
            getattr(mensaje, "text", None),
            getattr(mensaje, "caption", None),
        ]
        if parte
    )

    return bool(
        __import__("re").search(
            r"(https?://|www\.|t\.me/|wa\.me/)",
            contenido,
            flags=__import__("re").IGNORECASE,
        )
    )


def clasificar_contenido_mensaje(mensaje):
    if getattr(mensaje, "photo", None):
        return "FOTO"

    if getattr(mensaje, "video", None):
        return "VIDEO"

    if getattr(mensaje, "animation", None):
        return "GIF/ANIMACIÓN"

    if getattr(mensaje, "document", None):
        return "DOCUMENTO"

    if getattr(mensaje, "audio", None):
        return "AUDIO"

    if getattr(mensaje, "voice", None):
        return "VOZ"

    if getattr(mensaje, "sticker", None):
        return "STICKER"

    if getattr(mensaje, "video_note", None):
        return "VIDEO NOTA"

    if detectar_enlace_mensaje(mensaje):
        return "TEXTO + ENLACE"

    if getattr(mensaje, "text", None):
        return "TEXTO"

    return "OTRO"


def guardar_actividad_db(
    identidad_tipo,
    identidad_id,
    username,
    nombre,
    es_bot,
    chat,
    message_id,
    tipo_contenido,
    contiene_enlace,
):
    ahora = datetime.now(timezone.utc).isoformat()

    with conectar_db() as conexion:
        conexion.execute(
            """
            INSERT OR IGNORE INTO actividad_grupo (
                identidad_tipo, identidad_id,
                username, nombre, es_bot,
                chat_id, chat_username, chat_nombre,
                message_id, tipo_contenido,
                contiene_enlace, fecha_evento
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identidad_tipo,
                int(identidad_id),
                username,
                nombre,
                1 if es_bot else 0,
                chat.id,
                getattr(chat, "username", None),
                getattr(chat, "title", None),
                int(message_id),
                tipo_contenido,
                1 if contiene_enlace else 0,
                ahora,
            ),
        )
        conexion.commit()


def resumen_actividad_db(identidad_tipo, identidad_id):
    limites = limites_periodos_actividad()

    with conectar_db() as conexion:
        resultado = {}

        for clave, inicio in limites.items():
            fila = conexion.execute(
                """
                SELECT COUNT(*) AS total
                FROM actividad_grupo
                WHERE identidad_tipo = ?
                  AND identidad_id = ?
                  AND fecha_evento >= ?
                """,
                (identidad_tipo, identidad_id, inicio),
            ).fetchone()

            resultado[clave] = int(fila["total"] if fila else 0)

        tipos = conexion.execute(
            """
            SELECT tipo_contenido, COUNT(*) AS total
            FROM actividad_grupo
            WHERE identidad_tipo = ?
              AND identidad_id = ?
              AND fecha_evento >= ?
            GROUP BY tipo_contenido
            ORDER BY total DESC, tipo_contenido ASC
            """,
            (
                identidad_tipo,
                identidad_id,
                limites["mes"],
            ),
        ).fetchall()

        grupos = conexion.execute(
            """
            SELECT
                COALESCE(chat_nombre, chat_username, CAST(chat_id AS TEXT)) AS grupo,
                COUNT(*) AS total
            FROM actividad_grupo
            WHERE identidad_tipo = ?
              AND identidad_id = ?
              AND fecha_evento >= ?
            GROUP BY chat_id
            ORDER BY total DESC
            """,
            (
                identidad_tipo,
                identidad_id,
                limites["mes"],
            ),
        ).fetchall()

    resultado["tipos_mes"] = tipos
    resultado["grupos_mes"] = grupos
    return resultado


def guardar_movimiento_db(user, chat, tipo_movimiento):
    ahora = datetime.now(timezone.utc).isoformat()
    nombre = nombre_visible_usuario(user)

    with conectar_db() as conexion:
        # Evita duplicar el mismo cambio si Telegram entrega actualizaciones
        # repetidas en un intervalo muy corto.
        ultimo = conexion.execute(
            """
            SELECT tipo_movimiento, fecha_evento
            FROM movimientos_grupo
            WHERE user_id = ?
              AND chat_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user.id, chat.id),
        ).fetchone()

        if ultimo and ultimo["tipo_movimiento"] == tipo_movimiento:
            try:
                fecha_ultimo = datetime.fromisoformat(ultimo["fecha_evento"])
                if fecha_ultimo.tzinfo is None:
                    fecha_ultimo = fecha_ultimo.replace(tzinfo=timezone.utc)
                if (
                    datetime.now(timezone.utc) - fecha_ultimo
                ).total_seconds() < 10:
                    return False
            except (TypeError, ValueError):
                pass

        conexion.execute(
            """
            INSERT INTO movimientos_grupo (
                user_id, username, nombre,
                chat_id, chat_username, chat_nombre,
                tipo_movimiento, fecha_evento
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user.id,
                user.username,
                nombre,
                chat.id,
                getattr(chat, "username", None),
                getattr(chat, "title", None),
                tipo_movimiento,
                ahora,
            ),
        )
        conexion.commit()

    return True


def resumen_movimientos_db(user_id):
    with conectar_db() as conexion:
        fila = conexion.execute(
            """
            SELECT
                SUM(CASE WHEN tipo_movimiento = 'ENTRADA' THEN 1 ELSE 0 END) AS entradas,
                SUM(CASE WHEN tipo_movimiento = 'SALIDA' THEN 1 ELSE 0 END) AS salidas,
                MIN(CASE WHEN tipo_movimiento = 'ENTRADA' THEN fecha_evento END) AS primera_entrada,
                MAX(CASE WHEN tipo_movimiento = 'ENTRADA' THEN fecha_evento END) AS ultima_entrada,
                MAX(CASE WHEN tipo_movimiento = 'SALIDA' THEN fecha_evento END) AS ultima_salida
            FROM movimientos_grupo
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

        por_grupo = conexion.execute(
            """
            SELECT
                COALESCE(chat_nombre, chat_username, CAST(chat_id AS TEXT)) AS grupo,
                SUM(CASE WHEN tipo_movimiento = 'ENTRADA' THEN 1 ELSE 0 END) AS entradas,
                SUM(CASE WHEN tipo_movimiento = 'SALIDA' THEN 1 ELSE 0 END) AS salidas
            FROM movimientos_grupo
            WHERE user_id = ?
            GROUP BY chat_id
            ORDER BY grupo
            """,
            (user_id,),
        ).fetchall()

    return {
        "entradas": int((fila["entradas"] or 0) if fila else 0),
        "salidas": int((fila["salidas"] or 0) if fila else 0),
        "primera_entrada": fila["primera_entrada"] if fila else None,
        "ultima_entrada": fila["ultima_entrada"] if fila else None,
        "ultima_salida": fila["ultima_salida"] if fila else None,
        "por_grupo": por_grupo,
    }


def formatear_fecha_peru(fecha_iso):
    if not fecha_iso:
        return "No disponible"
    try:
        fecha = datetime.fromisoformat(str(fecha_iso))
        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)
        fecha = fecha.astimezone(ZONA_PERU)
        texto = fecha.strftime("%d/%m/%Y · %I:%M %p")
        return texto.replace("AM", "a. m.").replace("PM", "p. m.")
    except (TypeError, ValueError):
        return str(fecha_iso)


def guardar_panel_orma_db(propietario_id, message_id):
    with conectar_db() as conexion:
        conexion.execute(
            """
            INSERT INTO paneles_orma (propietario_id, message_id, fecha_actualizacion)
            VALUES (?, ?, ?)
            ON CONFLICT(propietario_id) DO UPDATE SET
                message_id = excluded.message_id,
                fecha_actualizacion = excluded.fecha_actualizacion
            """,
            (propietario_id, int(message_id), datetime.now(timezone.utc).isoformat()),
        )
        conexion.commit()


def obtener_panel_orma_db(propietario_id):
    with conectar_db() as conexion:
        fila = conexion.execute(
            "SELECT message_id FROM paneles_orma WHERE propietario_id = ? LIMIT 1",
            (propietario_id,),
        ).fetchone()
    return int(fila["message_id"]) if fila else None


def eliminar_panel_orma_db(propietario_id):
    with conectar_db() as conexion:
        conexion.execute("DELETE FROM paneles_orma WHERE propietario_id = ?", (propietario_id,))
        conexion.commit()


def contar_capturas_objetivo_orma(objetivo_tipo, objetivo_id):
    with conectar_db() as conexion:
        fila = conexion.execute(
            "SELECT COUNT(*) AS total FROM capturas_orma WHERE objetivo_tipo = ? AND objetivo_id = ?",
            (objetivo_tipo, objetivo_id),
        ).fetchone()
    return int(fila["total"] if fila else 0)


def guardar_captura_orma(propietario_id, objetivo_tipo, objetivo_id,
                         objetivo_username, objetivo_nombre, objetivo_es_bot,
                         chat, mensaje_origen_id):
    ahora = datetime.now(timezone.utc).isoformat()
    with conectar_db() as conexion:
        cursor = conexion.execute(
            """
            INSERT INTO capturas_orma (
                propietario_id, objetivo_tipo, objetivo_id,
                objetivo_username, objetivo_nombre, objetivo_es_bot,
                chat_id, chat_username, chat_nombre,
                mensaje_origen_id, fecha_captura
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                propietario_id, objetivo_tipo, objetivo_id,
                objetivo_username, objetivo_nombre,
                1 if objetivo_es_bot else 0,
                chat.id, getattr(chat, "username", None),
                getattr(chat, "title", None),
                mensaje_origen_id, ahora,
            ),
        )
        captura_id = cursor.lastrowid
        conexion.commit()
    return captura_id


def obtener_captura_orma(captura_id):
    with conectar_db() as conexion:
        return conexion.execute(
            "SELECT * FROM capturas_orma WHERE id = ?",
            (captura_id,),
        ).fetchone()


def teclado_ficha_orma(captura_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 ACTUALIZAR FICHA", callback_data=f"orma_ficha:{captura_id}")],
        [
            InlineKeyboardButton("🔐 MEMBRESÍA", callback_data=f"orma_membresia:{captura_id}"),
            InlineKeyboardButton("📣 PUBLICIDAD", callback_data=f"orma_publicidad:{captura_id}"),
        ],
        [
            InlineKeyboardButton("📊 ACTIVIDAD", callback_data=f"orma_actividad:{captura_id}"),
            InlineKeyboardButton("🚪 ENTRADAS / SALIDAS", callback_data=f"orma_movimientos:{captura_id}"),
        ],
        [
            InlineKeyboardButton("🏠 MENÚ PRINCIPAL", callback_data="orma_menu_principal"),
            InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar"),
        ],
    ])


async def construir_texto_ficha_orma(captura):
    objetivo_id = captura["objetivo_id"]
    username = f"@{captura['objetivo_username']}" if captura["objetivo_username"] else "Sin @username"
    nombre = captura["objetivo_nombre"] or "Sin nombre visible"
    rol = await obtener_rol_en_grupo(captura["chat_id"], objetivo_id)

    progreso = "No aplica"
    faltantes = 0
    total = TOTAL_GRUPOS_OBLIGATORIOS
    if captura["objetivo_tipo"] in {"USUARIO", "BOT"}:
        try:
            estado = await obtener_estado_membresia_7de7(objetivo_id)
            completos = len(estado["completados"])
            total = estado["total"]
            faltantes = len(estado["faltantes"])
            progreso = f"{completos}/{total}"
        except Exception:
            logging.exception("Error obteniendo membresía para /orma objetivo=%s", objetivo_id)
            progreso = "No disponible"

    oficial = captura["objetivo_tipo"] == "BOT" and (captura["objetivo_username"] or "").lower() in BOTS_OFICIALES_EXENTOS
    if oficial:
        condicion = "✅ BOT OFICIAL · EXENTO DE RAÍZ"
    elif progreso == f"{total}/{total}":
        condicion = "🟢 HABILITADO"
    elif progreso == "No aplica":
        condicion = "⚪ IDENTIDAD DE CHAT/CANAL"
    elif progreso == "No disponible":
        condicion = "🟡 ESTADO NO DISPONIBLE"
    else:
        condicion = f"🔴 MEMBRESÍA INCOMPLETA · faltan {faltantes}"

    capturas_totales = contar_capturas_objetivo_orma(captura["objetivo_tipo"], objetivo_id)

    return (
        "🛡️ <b>FICHA DE CONTROL /ORMA</b>\n\n"
        "👤 <b>IDENTIDAD</b>\n"
        f"• Nombre: <b>{nombre}</b>\n"
        f"• Usuario: <b>{username}</b>\n"
        f"• ID: <code>{objetivo_id}</code>\n"
        f"• Tipo: <b>{captura['objetivo_tipo']}</b>\n"
        f"• Rol en grupo origen: <b>{rol}</b>\n\n"
        "🔐 <b>ESTADO GENERAL</b>\n"
        f"• Membresía: <b>{progreso}</b>\n"
        f"• Condición: <b>{condicion}</b>\n\n"
        "📍 <b>ORIGEN DE LA CAPTURA</b>\n"
        f"• Grupo: <b>{captura['chat_nombre'] or captura['chat_username'] or captura['chat_id']}</b>\n"
        f"• Mensaje: <code>{captura['mensaje_origen_id']}</code>\n"
        f"• Fecha: <b>{formatear_fecha_peru(captura['fecha_captura'])}</b>\n"
        f"• Capturas registradas de esta identidad: <b>{capturas_totales}</b>\n\n"
        "Selecciona una herramienta."
    )


async def mostrar_ficha_orma_privada(bot, propietario_id, captura_id):
    captura = obtener_captura_orma(captura_id)
    if not captura or captura["propietario_id"] != propietario_id:
        return False

    texto = await construir_texto_ficha_orma(captura)
    panel_id = PANELES_ORMA.get(propietario_id) or obtener_panel_orma_db(propietario_id)

    if panel_id:
        try:
            await bot.edit_message_text(
                chat_id=propietario_id,
                message_id=panel_id,
                text=texto,
                parse_mode="HTML",
                reply_markup=teclado_ficha_orma(captura_id),
            )
            return True
        except TelegramError as error:
            if "message is not modified" in str(error).lower():
                return True

    try:
        enviado = await bot.send_message(
            chat_id=propietario_id,
            text=texto,
            parse_mode="HTML",
            reply_markup=teclado_ficha_orma(captura_id),
        )
        PANELES_ORMA[propietario_id] = enviado.message_id
        guardar_panel_orma_db(propietario_id, enviado.message_id)
        return True
    except TelegramError:
        logging.exception("No se pudo abrir ficha /orma para %s", propietario_id)
        return False


async def orma_comando(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.effective_message
    ejecutor = update.effective_user
    chat = update.effective_chat

    if (
        not mensaje or not ejecutor or not chat
        or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}
        or not es_grupo_controlado(chat)
    ):
        return

    # /orma nunca queda visible.
    try:
        await mensaje.delete()
    except TelegramError:
        logging.exception("No se pudo eliminar /orma en chat=%s", chat.id)

    origen = mensaje.reply_to_message
    if origen is None:
        return

    objetivo = origen.from_user
    sender_chat = origen.sender_chat

    if objetivo is not None:
        tipo = "BOT" if objetivo.is_bot else "USUARIO"
        objetivo_id = objetivo.id
        objetivo_username = objetivo.username
        objetivo_nombre = nombre_visible_usuario(objetivo)
        objetivo_es_bot = objetivo.is_bot
        registrar_usuario_membresia(objetivo)
    elif sender_chat is not None:
        tipo = "CANAL/CHAT"
        objetivo_id = sender_chat.id
        objetivo_username = sender_chat.username
        objetivo_nombre = sender_chat.title or "Sin nombre visible"
        objetivo_es_bot = False
    else:
        return

    captura_id = guardar_captura_orma(
        ejecutor.id, tipo, objetivo_id, objetivo_username,
        objetivo_nombre, objetivo_es_bot, chat, origen.message_id,
    )
    CAPTURAS_ORMA[ejecutor.id] = captura_id
    await mostrar_ficha_orma_privada(context.bot, ejecutor.id, captura_id)


async def orma_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    usuario = update.effective_user
    if not query or not usuario:
        return

    data = query.data or ""

    if data == "orma_cerrar":
        await query.answer()
        try:
            await query.message.delete()
        except TelegramError:
            pass
        PANELES_ORMA.pop(usuario.id, None)
        eliminar_panel_orma_db(usuario.id)
        return

    if data == "orma_menu_principal":
        await query.answer()
        try:
            await query.edit_message_text(
                "🛡️ <b>MÁXIMO CONTROL GROUP</b>\n\n"
                "Panel administrativo.\n\n"
                "Usa <code>/orma</code> respondiendo un mensaje en un grupo controlado.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar")
                ]]),
            )
        except TelegramError:
            pass
        return

    if data.startswith("orma_ficha:"):
        try:
            captura_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer()
            return
        await query.answer()
        await mostrar_ficha_orma_privada(context.bot, usuario.id, captura_id)
        return

    if data.startswith("orma_membresia:"):
        try:
            captura_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer()
            return
        captura = obtener_captura_orma(captura_id)
        if not captura or captura["propietario_id"] != usuario.id:
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        await query.answer()

        if captura["objetivo_tipo"] not in {"USUARIO", "BOT"}:
            texto_membresia = (
                "🔐 <b>MEMBRESÍA</b>\n\n"
                "Esta identidad es un canal/chat y no puede evaluarse con la regla de usuario 7/7."
            )
        else:
            estado = await obtener_estado_membresia_7de7(captura["objetivo_id"])
            lineas = [
                "🔐 <b>MEMBRESÍA 7/7</b>", "",
                f"Progreso: <b>{len(estado['completados'])}/{estado['total']}</b>", "",
            ]
            if estado["completo"]:
                lineas.append("✅ Pertenece a los 7 grupos oficiales.")
            else:
                lineas.append("❌ <b>Grupos faltantes:</b>")
                for grupo in estado["faltantes"]:
                    lineas.append(f"• {grupo['nombre']}")
            texto_membresia = "\n".join(lineas)

        await query.edit_message_text(
            texto_membresia,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 VERIFICAR AHORA", callback_data=f"orma_membresia:{captura_id}")],
                [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_ficha:{captura_id}")],
                [
                    InlineKeyboardButton("🏠 MENÚ PRINCIPAL", callback_data="orma_menu_principal"),
                    InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar"),
                ],
            ]),
        )
        return

    if data.startswith("orma_actividad:"):
        try:
            captura_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer()
            return

        captura = obtener_captura_orma(captura_id)
        if not captura or captura["propietario_id"] != usuario.id:
            await query.answer("Ficha no disponible.", show_alert=True)
            return

        await query.answer()

        resumen = resumen_actividad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )

        lineas = [
            "📊 <b>ACTIVIDAD REGISTRADA</b>",
            "",
            "🕐 <b>Volumen</b>",
            f"• Última hora: <b>{resumen['hora']}</b>",
            f"• Hoy: <b>{resumen['dia']}</b>",
            f"• Semana: <b>{resumen['semana']}</b>",
            f"• Mes: <b>{resumen['mes']}</b>",
            "",
            "📦 <b>Tipos de contenido este mes</b>",
        ]

        if resumen["tipos_mes"]:
            for fila in resumen["tipos_mes"][:8]:
                lineas.append(
                    f"• {fila['tipo_contenido']}: <b>{fila['total']}</b>"
                )
        else:
            lineas.append("• Sin actividad registrada todavía.")

        lineas.extend(["", "📍 <b>Actividad por grupo este mes</b>"])

        if resumen["grupos_mes"]:
            for fila in resumen["grupos_mes"][:7]:
                lineas.append(
                    f"• {fila['grupo']}: <b>{fila['total']}</b>"
                )
        else:
            lineas.append("• Sin actividad registrada todavía.")

        lineas.extend([
            "",
            "ℹ️ El historial comienza desde la activación de este bloque; "
            "no reconstruye mensajes anteriores.",
        ])

        await query.edit_message_text(
            "\n".join(lineas),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔄 ACTUALIZAR",
                        callback_data=f"orma_actividad:{captura_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ RETROCEDER",
                        callback_data=f"orma_ficha:{captura_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 MENÚ PRINCIPAL",
                        callback_data="orma_menu_principal",
                    ),
                    InlineKeyboardButton(
                        "🗑 CERRAR",
                        callback_data="orma_cerrar",
                    ),
                ],
            ]),
        )
        return

    if data.startswith("orma_movimientos:"):
        try:
            captura_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer()
            return

        captura = obtener_captura_orma(captura_id)
        if not captura or captura["propietario_id"] != usuario.id:
            await query.answer("Ficha no disponible.", show_alert=True)
            return

        await query.answer()

        if captura["objetivo_tipo"] not in {"USUARIO", "BOT"}:
            lineas = [
                "🚪 <b>ENTRADAS / SALIDAS</b>",
                "",
                "Esta identidad es un canal/chat y no tiene historial "
                "de membresía de usuario.",
            ]
        else:
            resumen = resumen_movimientos_db(captura["objetivo_id"])

            lineas = [
                "🚪 <b>ENTRADAS / SALIDAS</b>",
                "",
                f"➕ Entradas registradas: <b>{resumen['entradas']}</b>",
                f"➖ Salidas registradas: <b>{resumen['salidas']}</b>",
                "",
                f"🟢 Primera entrada observada: "
                f"<b>{formatear_fecha_peru(resumen['primera_entrada'])}</b>",
                f"🔄 Última entrada: "
                f"<b>{formatear_fecha_peru(resumen['ultima_entrada'])}</b>",
                f"🔴 Última salida: "
                f"<b>{formatear_fecha_peru(resumen['ultima_salida'])}</b>",
                "",
                "📍 <b>Por grupo</b>",
            ]

            if resumen["por_grupo"]:
                for fila in resumen["por_grupo"][:7]:
                    lineas.append(
                        f"• {fila['grupo']}: "
                        f"➕ {int(fila['entradas'] or 0)} · "
                        f"➖ {int(fila['salidas'] or 0)}"
                    )
            else:
                lineas.append("• Sin movimientos registrados todavía.")

            lineas.extend([
                "",
                "ℹ️ Solo se contabilizan movimientos observados desde "
                "la activación de este registro.",
            ])

        await query.edit_message_text(
            "\n".join(lineas),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔄 ACTUALIZAR",
                        callback_data=f"orma_movimientos:{captura_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ RETROCEDER",
                        callback_data=f"orma_ficha:{captura_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 MENÚ PRINCIPAL",
                        callback_data="orma_menu_principal",
                    ),
                    InlineKeyboardButton(
                        "🗑 CERRAR",
                        callback_data="orma_cerrar",
                    ),
                ],
            ]),
        )
        return

    if data.startswith("orma_publicidad:"):
        captura_id = int(data.split(":", 1)[1])
        captura = obtener_captura_orma(captura_id)

        if not captura or captura["propietario_id"] != usuario.id:
            await query.answer("Ficha no disponible.", show_alert=True)
            return

        await query.answer()
        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )
        await query.edit_message_text(
            await texto_control_publicidad(captura),
            parse_mode="HTML",
            reply_markup=teclado_control_publicidad(captura_id, cfg),
        )
        return

    if data.startswith("orma_pub_modo:"):
        captura_id = int(data.split(":", 1)[1])
        await query.answer()
        await query.edit_message_text(
            "⚙️ <b>MODO DE CONTROL</b>\\n\\n"
            "HEREDADO: usará la regla global cuando la activemos.\\n"
            "PERSONALIZADO: aplica límites propios.\\n"
            "ILIMITADO: registra pero no limita.\\n"
            "BLOQUEADO: elimina toda publicidad controlable.\\n"
            "EXCLUIDO: no aplica control publicitario.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("HEREDADO", callback_data=f"orma_pub_setmodo:{captura_id}:HEREDADO"),
                    InlineKeyboardButton("PERSONALIZADO", callback_data=f"orma_pub_setmodo:{captura_id}:PERSONALIZADO"),
                ],
                [
                    InlineKeyboardButton("ILIMITADO", callback_data=f"orma_pub_setmodo:{captura_id}:ILIMITADO"),
                    InlineKeyboardButton("BLOQUEADO", callback_data=f"orma_pub_setmodo:{captura_id}:BLOQUEADO"),
                ],
                [
                    InlineKeyboardButton("EXCLUIDO", callback_data=f"orma_pub_setmodo:{captura_id}:EXCLUIDO"),
                ],
                [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_publicidad:{captura_id}")],
                [
                    InlineKeyboardButton("🏠 MENÚ PRINCIPAL", callback_data="orma_menu_principal"),
                    InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar"),
                ],
            ]),
        )
        return

    if data.startswith("orma_pub_setmodo:"):
        _, captura_txt, modo = data.split(":", 2)
        captura_id = int(captura_txt)
        captura = obtener_captura_orma(captura_id)

        actualizar_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            modo=modo,
        )
        await query.answer(f"Modo: {modo}")
        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )
        await query.edit_message_text(
            await texto_control_publicidad(captura),
            parse_mode="HTML",
            reply_markup=teclado_control_publicidad(captura_id, cfg),
        )
        return

    if data.startswith("orma_pub_sep:"):
        captura_id = int(data.split(":", 1)[1])
        await query.answer()
        await query.edit_message_text(
            "⏱ <b>SEPARACIÓN ENTRE PUBLICIDADES</b>\\n\\n"
            "Selecciona el tiempo mínimo entre una publicidad permitida "
            "y la siguiente.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("SIN SEPARACIÓN", callback_data=f"orma_pub_setsep:{captura_id}:none"),
                ],
                [
                    InlineKeyboardButton("5 min", callback_data=f"orma_pub_setsep:{captura_id}:300"),
                    InlineKeyboardButton("10 min", callback_data=f"orma_pub_setsep:{captura_id}:600"),
                    InlineKeyboardButton("15 min", callback_data=f"orma_pub_setsep:{captura_id}:900"),
                ],
                [
                    InlineKeyboardButton("30 min", callback_data=f"orma_pub_setsep:{captura_id}:1800"),
                    InlineKeyboardButton("1 h", callback_data=f"orma_pub_setsep:{captura_id}:3600"),
                ],
                [
                    InlineKeyboardButton("✍️ PERSONALIZAR MINUTOS", callback_data=f"orma_pub_input:{captura_id}:separacion_minutos"),
                ],
                [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_publicidad:{captura_id}")],
            ]),
        )
        return

    if data.startswith("orma_pub_setsep:"):
        _, captura_txt, valor = data.split(":", 2)
        captura_id = int(captura_txt)
        captura = obtener_captura_orma(captura_id)
        segundos = None if valor == "none" else int(valor)

        actualizar_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            modo="PERSONALIZADO",
            separacion_segundos=segundos,
        )
        await query.answer("Separación actualizada")
        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )
        await query.edit_message_text(
            await texto_control_publicidad(captura),
            parse_mode="HTML",
            reply_markup=teclado_control_publicidad(captura_id, cfg),
        )
        return

    if data.startswith("orma_pub_limites:"):
        captura_id = int(data.split(":", 1)[1])
        captura = obtener_captura_orma(captura_id)
        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )
        await query.answer()
        await query.edit_message_text(
            "🔢 <b>LÍMITES DE PUBLICIDAD</b>\\n\\n"
            f"Hora: <b>{texto_valor_limite(cfg['limite_hora'])}</b>\\n"
            f"Día: <b>{texto_valor_limite(cfg['limite_dia'])}</b>\\n"
            f"Semana: <b>{texto_valor_limite(cfg['limite_semana'])}</b>\\n"
            f"Mes: <b>{texto_valor_limite(cfg['limite_mes'])}</b>\\n"
            f"Año: <b>{texto_valor_limite(cfg['limite_anio'])}</b>\\n\\n"
            "Pulsa un periodo y escribe el máximo. "
            "El número escrito se borrará automáticamente.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("HORA", callback_data=f"orma_pub_input:{captura_id}:limite_hora"),
                    InlineKeyboardButton("DÍA", callback_data=f"orma_pub_input:{captura_id}:limite_dia"),
                ],
                [
                    InlineKeyboardButton("SEMANA", callback_data=f"orma_pub_input:{captura_id}:limite_semana"),
                    InlineKeyboardButton("MES", callback_data=f"orma_pub_input:{captura_id}:limite_mes"),
                    InlineKeyboardButton("AÑO", callback_data=f"orma_pub_input:{captura_id}:limite_anio"),
                ],
                [
                    InlineKeyboardButton("♾ QUITAR TODOS LOS LÍMITES", callback_data=f"orma_pub_sinlimites:{captura_id}"),
                ],
                [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_publicidad:{captura_id}")],
            ]),
        )
        return

    if data.startswith("orma_pub_sinlimites:"):
        captura_id = int(data.split(":", 1)[1])
        captura = obtener_captura_orma(captura_id)
        actualizar_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            modo="PERSONALIZADO",
            limite_hora=None,
            limite_dia=None,
            limite_semana=None,
            limite_mes=None,
            limite_anio=None,
        )
        await query.answer("Límites eliminados")
        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )
        await query.edit_message_text(
            await texto_control_publicidad(captura),
            parse_mode="HTML",
            reply_markup=teclado_control_publicidad(captura_id, cfg),
        )
        return

    if data.startswith("orma_pub_tipos:"):
        captura_id = int(data.split(":", 1)[1])
        captura = obtener_captura_orma(captura_id)
        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )
        await query.answer()

        def marca(campo):
            return "✅" if bool(cfg[campo]) else "❌"

        await query.edit_message_text(
            "🎛 <b>TIPOS CONTROLADOS</b>\\n\\n"
            "✅ = entra al control de cupos/separación\\n"
            "❌ = queda libre para esta identidad\\n\\n"
            "El texto normal puro siempre permanece libre.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"{marca('controlar_foto')} FOTO", callback_data=f"orma_pub_toggle:{captura_id}:controlar_foto"),
                    InlineKeyboardButton(f"{marca('controlar_video')} VIDEO", callback_data=f"orma_pub_toggle:{captura_id}:controlar_video"),
                ],
                [
                    InlineKeyboardButton(f"{marca('controlar_gif')} GIF", callback_data=f"orma_pub_toggle:{captura_id}:controlar_gif"),
                    InlineKeyboardButton(f"{marca('controlar_documento')} DOCUMENTO", callback_data=f"orma_pub_toggle:{captura_id}:controlar_documento"),
                ],
                [
                    InlineKeyboardButton(f"{marca('controlar_enlace')} ENLACE", callback_data=f"orma_pub_toggle:{captura_id}:controlar_enlace"),
                ],
                [
                    InlineKeyboardButton(f"{marca('controlar_custom_emoji')} PREMIUM EMOJI", callback_data=f"orma_pub_toggle:{captura_id}:controlar_custom_emoji"),
                ],
                [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_publicidad:{captura_id}")],
            ]),
        )
        return

    if data.startswith("orma_pub_toggle:"):
        _, captura_txt, campo = data.split(":", 2)
        captura_id = int(captura_txt)
        captura = obtener_captura_orma(captura_id)
        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )
        nuevo = 0 if bool(cfg[campo]) else 1
        actualizar_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            modo="PERSONALIZADO",
            **{campo: nuevo},
        )
        await query.answer("Actualizado")

        # Reabrir submenú tipos.
        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )

        def marca(c):
            return "✅" if bool(cfg[c]) else "❌"

        await query.edit_message_text(
            "🎛 <b>TIPOS CONTROLADOS</b>\\n\\n"
            "✅ = entra al control de cupos/separación\\n"
            "❌ = queda libre para esta identidad\\n\\n"
            "El texto normal puro siempre permanece libre.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"{marca('controlar_foto')} FOTO", callback_data=f"orma_pub_toggle:{captura_id}:controlar_foto"),
                    InlineKeyboardButton(f"{marca('controlar_video')} VIDEO", callback_data=f"orma_pub_toggle:{captura_id}:controlar_video"),
                ],
                [
                    InlineKeyboardButton(f"{marca('controlar_gif')} GIF", callback_data=f"orma_pub_toggle:{captura_id}:controlar_gif"),
                    InlineKeyboardButton(f"{marca('controlar_documento')} DOCUMENTO", callback_data=f"orma_pub_toggle:{captura_id}:controlar_documento"),
                ],
                [
                    InlineKeyboardButton(f"{marca('controlar_enlace')} ENLACE", callback_data=f"orma_pub_toggle:{captura_id}:controlar_enlace"),
                ],
                [
                    InlineKeyboardButton(f"{marca('controlar_custom_emoji')} PREMIUM EMOJI", callback_data=f"orma_pub_toggle:{captura_id}:controlar_custom_emoji"),
                ],
                [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_publicidad:{captura_id}")],
            ]),
        )
        return

    if data.startswith("orma_pub_reset:"):
        captura_id = int(data.split(":", 1)[1])
        captura = obtener_captura_orma(captura_id)
        resetear_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )
        await query.answer("Restaurado a HEREDADO")
        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )
        await query.edit_message_text(
            await texto_control_publicidad(captura),
            parse_mode="HTML",
            reply_markup=teclado_control_publicidad(captura_id, cfg),
        )
        return

    if data.startswith("orma_pub_input:"):
        _, captura_txt, campo = data.split(":", 2)
        captura_id = int(captura_txt)

        ENTRADAS_CONTROL_PUBLICIDAD[usuario.id] = {
            "captura_id": captura_id,
            "campo": campo,
        }

        await query.answer()
        etiqueta = {
            "separacion_minutos": "minutos de separación",
            "limite_hora": "máximo por hora",
            "limite_dia": "máximo por día",
            "limite_semana": "máximo por semana",
            "limite_mes": "máximo por mes",
            "limite_anio": "máximo por año",
        }.get(campo, "valor")

        await query.edit_message_text(
            "✍️ <b>VALOR PERSONALIZADO</b>\\n\\n"
            f"Escribe ahora el <b>{etiqueta}</b>.\\n\\n"
            "Envía un número entero igual o mayor que 0. "
            "Tu mensaje se eliminará automáticamente.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "❌ CANCELAR",
                        callback_data=f"orma_pub_cancelinput:{captura_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ RETROCEDER",
                        callback_data=f"orma_publicidad:{captura_id}",
                    )
                ],
            ]),
        )
        return

    if data.startswith("orma_pub_cancelinput:"):
        captura_id = int(data.split(":", 1)[1])
        ENTRADAS_CONTROL_PUBLICIDAD.pop(usuario.id, None)
        await query.answer("Cancelado")
        captura = obtener_captura_orma(captura_id)
        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )
        await query.edit_message_text(
            await texto_control_publicidad(captura),
            parse_mode="HTML",
            reply_markup=teclado_control_publicidad(captura_id, cfg),
        )
        return



async def control_publicidad_individual_grupos(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    mensaje = update.effective_message
    chat = update.effective_chat

    if (
        not mensaje
        or not chat
        or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}
        or not es_grupo_controlado(chat)
    ):
        return

    usuario = mensaje.from_user
    sender_chat = mensaje.sender_chat

    if usuario is not None:
        if es_bot_oficial_exento(usuario):
            return

        identidad_tipo = "BOT" if usuario.is_bot else "USUARIO"
        identidad_id = usuario.id
        username = usuario.username
        nombre = nombre_visible_usuario(usuario)

        # La membresía 7/7 sigue siendo la primera puerta.
        estado = await obtener_estado_membresia_7de7(usuario.id)
        if not estado["completo"]:
            return

        tipo_contenido = tipo_publicitario_mensaje(mensaje, usuario)

    elif sender_chat is not None:
        identidad_tipo = "CANAL/CHAT"
        identidad_id = sender_chat.id
        username = sender_chat.username
        nombre = sender_chat.title or "Sin nombre visible"
        tipo_contenido = tipo_publicitario_mensaje(mensaje, None)

    else:
        return

    # Texto puro no entra al motor.
    if tipo_contenido is None:
        return

    permitido, motivo, cfg, disponible = evaluar_control_publicidad(
        identidad_tipo,
        identidad_id,
        tipo_contenido,
    )

    if permitido:
        registrar_evento_publicidad_db(
            identidad_tipo,
            identidad_id,
            chat,
            mensaje.message_id,
            tipo_contenido,
            "PERMITIDA",
            motivo,
        )
        return

    try:
        await mensaje.delete()
    except TelegramError:
        logging.exception(
            "No se pudo eliminar publicidad bloqueada identidad=%s chat=%s",
            identidad_id,
            chat.id,
        )

    registrar_evento_publicidad_db(
        identidad_tipo,
        identidad_id,
        chat,
        mensaje.message_id,
        tipo_contenido,
        "BLOQUEADA",
        motivo,
    )

    await mostrar_aviso_publicidad_temporal(
        context=context,
        chat=chat,
        identidad_id=identidad_id,
        nombre=nombre,
        username=username,
        tipo_identidad=identidad_tipo,
        tipo_contenido=tipo_contenido,
        motivo=motivo,
        disponible=disponible,
    )


async def registrar_actividad_grupo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    mensaje = update.effective_message
    chat = update.effective_chat

    if (
        not mensaje
        or not chat
        or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}
        or not es_grupo_controlado(chat)
    ):
        return

    # Los comandos operativos no forman parte de las métricas de actividad.
    if getattr(mensaje, "text", None) and mensaje.text.startswith("/"):
        return

    usuario = mensaje.from_user
    sender_chat = mensaje.sender_chat

    if usuario is not None:
        identidad_tipo = "BOT" if usuario.is_bot else "USUARIO"
        identidad_id = usuario.id
        username = usuario.username
        nombre = nombre_visible_usuario(usuario)
        es_bot = usuario.is_bot
    elif sender_chat is not None:
        identidad_tipo = "CANAL/CHAT"
        identidad_id = sender_chat.id
        username = sender_chat.username
        nombre = sender_chat.title or "Sin nombre visible"
        es_bot = False
    else:
        return

    guardar_actividad_db(
        identidad_tipo=identidad_tipo,
        identidad_id=identidad_id,
        username=username,
        nombre=nombre,
        es_bot=es_bot,
        chat=chat,
        message_id=mensaje.message_id,
        tipo_contenido=clasificar_contenido_mensaje(mensaje),
        contiene_enlace=detectar_enlace_mensaje(mensaje),
    )


async def registrar_cambio_membresia_grupo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    cambio = update.chat_member
    if cambio is None:
        return

    chat = cambio.chat

    if (
        chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}
        or not es_grupo_controlado(chat)
    ):
        return

    usuario = cambio.new_chat_member.user
    anterior_es_miembro = estado_es_miembro(cambio.old_chat_member)
    nuevo_es_miembro = estado_es_miembro(cambio.new_chat_member)

    if not anterior_es_miembro and nuevo_es_miembro:
        guardar_movimiento_db(usuario, chat, "ENTRADA")
        registrar_usuario_membresia(usuario)
        return

    if anterior_es_miembro and not nuevo_es_miembro:
        guardar_movimiento_db(usuario, chat, "SALIDA")
        registrar_usuario_membresia(usuario)


# =========================================================
# BOT MODERADOR: @MaximoControlGroup_bot
# =========================================================

async def maximo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.effective_message
    usuario = update.effective_user

    if not mensaje or not usuario:
        return

    registrar_usuario_membresia(usuario)

    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        try:
            await mensaje.delete()
        except TelegramError:
            pass

        texto = (
            "🛡️ <b>MÁXIMO CONTROL GROUP</b>\n\n"
            "Panel administrativo.\n\n"
            "Usa <code>/orma</code> respondiendo un mensaje "
            "en cualquiera de los grupos controlados."
        )
        teclado = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar")
        ]])

        panel_id = PANELES_ORMA.get(usuario.id) or obtener_panel_orma_db(usuario.id)
        if panel_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=usuario.id,
                    message_id=panel_id,
                    text=texto,
                    parse_mode="HTML",
                    reply_markup=teclado,
                )
                return
            except TelegramError as error:
                if "message is not modified" in str(error).lower():
                    return

        enviado = await context.bot.send_message(
            chat_id=usuario.id,
            text=texto,
            parse_mode="HTML",
            reply_markup=teclado,
        )
        PANELES_ORMA[usuario.id] = enviado.message_id
        guardar_panel_orma_db(usuario.id, enviado.message_id)


async def maximo_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.effective_message
    if not mensaje:
        return

    await mensaje.reply_text(
        "✅ MaximoControlGroup operativo\n"
        "🔐 Membresía: 7/7 activa\n"
        "🌐 Moderación: 7 grupos oficiales + @Orma_Pruebas\n"
        "🌐 Regla 7/7: usuarios, administradores y bots externos\n"        "✅ Bots oficiales: excluidos de raíz\n"        "🚫 Castigos/baneos: desactivados\n"
        "🛡️ Control publicitario general: pendiente"
    )


async def control_membresia_grupos(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    mensaje = update.effective_message
    usuario = update.effective_user
    chat = update.effective_chat

    if (
        not mensaje
        or not usuario
        or not chat
        or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}
        or not es_grupo_controlado(chat)
    ):
        return

    # ÚNICA EXCEPCIÓN: bots oficiales definidos de raíz.
    # Todo lo demás (usuarios, administradores y bots externos) cumple 7/7.
    if es_bot_oficial_exento(usuario):
        return

    registrar_usuario_membresia(usuario)

    estado = await obtener_estado_membresia_7de7(usuario.id)

    if estado["completo"]:
        return

    try:
        await mensaje.delete()
    except TelegramError:
        logging.exception(
            "No se pudo eliminar mensaje de user=%s en @%s",
            usuario.id,
            GRUPO_PRUEBAS_USERNAME,
        )

    usuario_db = obtener_usuario_membresia_db(usuario.id)
    union_iniciado = bool(
        usuario_db and usuario_db["union_bot_iniciado"]
    )

    if union_iniciado:
        # Mantiene actualizado su panel privado, pero el aviso del grupo
        # también aparece durante 2 minutos según la regla 7/7 definida.
        await mostrar_o_actualizar_panel_union(usuario.id)

    await mostrar_aviso_union_temporal(
        context=context,
        chat_id=chat.id,
        usuario=usuario,
        estado=estado,
    )



async def borrar_aviso_origen_desde_payload(
    context,
    usuario_id,
):
    """
    Payload esperado:
        m_<chat_id>_<message_id>_<user_id>

    Solo borra el aviso si el deep-link pertenece al mismo usuario
    que acaba de iniciar @UnionMembresia_bot.
    """
    if not context.args:
        return

    payload = context.args[0]

    if not payload.startswith("m_"):
        return

    partes = payload.split("_")

    if len(partes) != 4:
        return

    try:
        chat_id = int(partes[1])
        message_id = int(partes[2])
        payload_user_id = int(partes[3])
    except ValueError:
        return

    if payload_user_id != usuario_id:
        return

    if MAXIMO_APP_REF is None:
        return

    try:
        await MAXIMO_APP_REF.bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
    except TelegramError:
        # Puede haberse borrado ya por el temporizador de 60 segundos.
        pass

    clave = (chat_id, usuario_id)

    if AVISOS_MEMBRESIA_ACTIVOS.get(clave) == message_id:
        AVISOS_MEMBRESIA_ACTIVOS.pop(clave, None)



async def procesar_entrada_control_publicidad(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    mensaje = update.effective_message
    usuario = update.effective_user
    chat = update.effective_chat

    if (
        not mensaje
        or not usuario
        or not chat
        or chat.type != ChatType.PRIVATE
    ):
        return

    entrada = ENTRADAS_CONTROL_PUBLICIDAD.get(usuario.id)
    if not entrada:
        return

    # Cualquier texto/número escrito para operar el panel desaparece.
    try:
        await mensaje.delete()
    except TelegramError:
        pass

    valor_texto = str(mensaje.text or "").strip()

    try:
        valor = int(valor_texto)
        if valor < 0:
            raise ValueError
    except ValueError:
        # No dejamos mensaje de error adicional: reutilizamos el panel.
        panel_id = PANELES_ORMA.get(usuario.id) or obtener_panel_orma_db(usuario.id)
        if panel_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=usuario.id,
                    message_id=panel_id,
                    text=(
                        "❌ <b>VALOR NO VÁLIDO</b>\\n\\n"
                        "Escribe únicamente un número entero igual o mayor que 0.\\n"
                        "El mensaje será eliminado automáticamente."
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "❌ CANCELAR",
                            callback_data=f"orma_pub_cancelinput:{entrada['captura_id']}",
                        )
                    ]]),
                )
            except TelegramError:
                pass
        return

    captura = obtener_captura_orma(entrada["captura_id"])
    if not captura or captura["propietario_id"] != usuario.id:
        ENTRADAS_CONTROL_PUBLICIDAD.pop(usuario.id, None)
        return

    campo = entrada["campo"]

    if campo == "separacion_minutos":
        cambios = {"separacion_segundos": valor * 60}
    else:
        cambios = {campo: valor}

    cambios["modo"] = "PERSONALIZADO"

    actualizar_control_identidad_db(
        captura["objetivo_tipo"],
        captura["objetivo_id"],
        **cambios,
    )

    ENTRADAS_CONTROL_PUBLICIDAD.pop(usuario.id, None)

    cfg = obtener_control_identidad_db(
        captura["objetivo_tipo"],
        captura["objetivo_id"],
    )
    panel_id = PANELES_ORMA.get(usuario.id) or obtener_panel_orma_db(usuario.id)

    if panel_id:
        try:
            await context.bot.edit_message_text(
                chat_id=usuario.id,
                message_id=panel_id,
                text=await texto_control_publicidad(captura),
                parse_mode="HTML",
                reply_markup=teclado_control_publicidad(
                    entrada["captura_id"],
                    cfg,
                ),
            )
        except TelegramError:
            pass


# =========================================================
# BOT PRIVADO: @UnionMembresia_bot
# =========================================================

async def union_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.effective_message
    usuario = update.effective_user
    chat = update.effective_chat

    if (
        not mensaje
        or not usuario
        or not chat
        or chat.type != ChatType.PRIVATE
    ):
        return

    registrar_usuario_membresia(
        usuario,
        union_bot_iniciado=True,
    )

    # Si el usuario llegó desde el botón del aviso del grupo,
    # eliminamos ese aviso inmediatamente al recibir el /start.
    await borrar_aviso_origen_desde_payload(
        context,
        usuario.id,
    )

    try:
        await mensaje.delete()
    except TelegramError:
        pass

    await mostrar_o_actualizar_panel_union(usuario.id)


async def union_membresia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await union_start(update, context)


async def union_grupos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await union_start(update, context)


async def union_verificar_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    usuario = update.effective_user

    if not query or not usuario:
        return

    await query.answer("Verificando los 7 grupos…")

    registrar_usuario_membresia(
        usuario,
        union_bot_iniciado=True,
    )

    estado = await obtener_estado_membresia_7de7(usuario.id)

    try:
        await query.edit_message_text(
            texto_union_membresia(estado),
            parse_mode="HTML",
            reply_markup=teclado_union_membresia(estado),
        )
        guardar_union_panel_message_id(
            usuario.id,
            query.message.message_id,
        )

    except TelegramError as error:
        if "message is not modified" in str(error).lower():
            guardar_union_panel_message_id(
                usuario.id,
                query.message.message_id,
            )
            return

        await mostrar_o_actualizar_panel_union(usuario.id)


# =========================================================
# ARRANQUE DE AMBOS BOTS
# =========================================================

async def iniciar_aplicacion(application: Application):
    await application.initialize()
    await application.start()

    if application.updater is None:
        raise RuntimeError("La aplicación no tiene Updater disponible.")

    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


async def detener_aplicacion(application: Application):
    if application.updater is not None:
        await application.updater.stop()

    await application.stop()
    await application.shutdown()


async def main():
    global MAXIMO_APP_REF, UNION_APP_REF

    inicializar_base_datos()

    maximo_app = Application.builder().token(BOT_TOKEN).build()
    union_app = Application.builder().token(UNION_BOT_TOKEN).build()

    MAXIMO_APP_REF = maximo_app
    UNION_APP_REF = union_app

    maximo_app.add_handler(CommandHandler("start", maximo_start))
    maximo_app.add_handler(CommandHandler("estado", maximo_estado))
    maximo_app.add_handler(CommandHandler("orma", orma_comando))
    maximo_app.add_handler(
        CallbackQueryHandler(orma_callback, pattern=r"^orma_")
    )
    maximo_app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND,
            control_membresia_grupos,
        ),
        group=0,
    )
    maximo_app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND,
            control_publicidad_individual_grupos,
        ),
        group=1,
    )
    maximo_app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND,
            registrar_actividad_grupo,
        ),
        group=2,
    )
    maximo_app.add_handler(
        ChatMemberHandler(
            registrar_cambio_membresia_grupo,
            ChatMemberHandler.CHAT_MEMBER,
        ),
        group=3,
    )
    maximo_app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            procesar_entrada_control_publicidad,
        ),
        group=4,
    )

    union_app.add_handler(CommandHandler("start", union_start))
    union_app.add_handler(CommandHandler("membresia", union_membresia))
    union_app.add_handler(CommandHandler("grupos", union_grupos))
    union_app.add_handler(
        CallbackQueryHandler(
            union_verificar_callback,
            pattern=r"^union_verificar$",
        )
    )

    await iniciar_aplicacion(maximo_app)
    await iniciar_aplicacion(union_app)

    logging.info("@MaximoControlGroup_bot iniciado.")
    logging.info("@UnionMembresia_bot iniciado.")
    logging.info("Membresía obligatoria configurada: 7/7.")
    logging.info("Regla 7/7 universal activa en los 7 grupos oficiales.")
    logging.info("Registro de actividad y movimientos del Bloque 3 activo.")
    logging.info("Control Publicitario Individual del Bloque 4 activo.")
    logging.info("Bots oficiales exentos de raíz: %s", sorted(BOTS_OFICIALES_EXENTOS))
    logging.info("@%s permanece como laboratorio de pruebas.", GRUPO_PRUEBAS_USERNAME)

    try:
        await asyncio.Event().wait()
    finally:
        await detener_aplicacion(union_app)
        await detener_aplicacion(maximo_app)


if __name__ == "__main__":
    asyncio.run(main())
