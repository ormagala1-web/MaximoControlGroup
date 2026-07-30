import os
import asyncio
import logging
import sqlite3
import html
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
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0") or 0)

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
DATABASE_PATH = os.path.join(DATA_DIR, "maximo_control.db")

TOTAL_GRUPOS_OBLIGATORIOS = 7
AVISO_MEMBRESIA_SEGUNDOS = 60
UNION_BOT_USERNAME = "UnionMembresia_bot"
GRUPO_PRUEBAS_USERNAME = "Orma_Pruebas"
ZONA_PERU = ZoneInfo("America/Lima")

GRUPOS_OFICIALES = [
    (1, "DISTRITO STREAMING UNIVERSAL ðŸŒŽðŸŒ", "DistritoStreamingUniversal", "DistritoStreamingUniversal_Bot"),
    (2, "STREAMING DIGITAL PERUCHO ðŸ‡µðŸ‡ª", "StreamingDigitalPerucho", "StreamingDigitalPerucho_bot"),
    (3, "PERÃš ENTRETENIMIENTO STREAMING ðŸ‡µðŸ‡ª", "PeruEntretenimientoStreaming", "PeruEntretenimientoStreaming_Bot"),
    (4, "MUNDO CACHINERO STREAMING ðŸŒŽ", "MundoCachineroStreaming", "MUCASTBOT"),
    (5, "ðŸŒŽ UNIVERSO CIBERNÃ‰TICO PERÃš ðŸ‡µðŸ‡ª", "mundocibertetico", "UniversoCibertneticoPeru_bot"),
    (6, "ðŸ’» Metaverso Streaming PerÃº ðŸ‡µðŸ‡ª", "metaversostreaminggo", "MetaversoPeru_bot"),
    (7, "ðŸŽ­ MUNDO STREAMING PERÃš ðŸ‡µðŸ‡ª", "mymundostreaming", "MundoStreamingPeru_bot"),
]

# Bots oficiales excluidos DE RAÃZ del control 7/7.
# Cualquier otro bot, usuario o administrador sÃ­ queda sujeto a la regla.
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

# Tipos controlables. TEXTO puro continÃºa siendo libre.
TIPOS_PUBLICIDAD_CONTROLABLE = {
    "FOTO",
    "VIDEO",
    "GIF/ANIMACIÃ“N",
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

        columnas_monitoreo = {
            "grupos_actuales": "INTEGER NOT NULL DEFAULT 0",
            "maximo_grupos": "INTEGER NOT NULL DEFAULT 0",
            "alcanzo_7de7": "INTEGER NOT NULL DEFAULT 0",
            "perdio_grupos": "INTEGER NOT NULL DEFAULT 0",
            "total_verificaciones": "INTEGER NOT NULL DEFAULT 0",
            "fecha_primera_verificacion": "TEXT",
            "fecha_ultima_verificacion": "TEXT",
            "origen_chat_id": "INTEGER",
            "origen_username": "TEXT",
            "origen_nombre": "TEXT",
            "fecha_ultimo_acceso": "TEXT",
        }

        for columna, definicion in columnas_monitoreo.items():
            if not columna_existe(conexion, "usuarios_membresia", columna):
                conexion.execute(
                    f"ALTER TABLE usuarios_membresia ADD COLUMN {columna} {definicion}"
                )

        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS paneles_union_admin (
                admin_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                fecha_actualizacion TEXT NOT NULL
            )
            """
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


def guardar_origen_union_db(user_id, chat_id, username=None, nombre=None):
    ahora = datetime.now(timezone.utc).isoformat()
    with conectar_db() as conexion:
        conexion.execute(
            """
            UPDATE usuarios_membresia
            SET origen_chat_id = ?,
                origen_username = ?,
                origen_nombre = ?,
                fecha_ultimo_acceso = ?,
                fecha_actualizacion = ?
            WHERE user_id = ?
            """,
            (chat_id, username, nombre, ahora, ahora, user_id),
        )
        conexion.commit()


def registrar_verificacion_membresia_db(user_id, estado):
    ahora = datetime.now(timezone.utc).isoformat()
    actuales = len(estado["completados"])

    with conectar_db() as conexion:
        fila = conexion.execute(
            "SELECT * FROM usuarios_membresia WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if not fila:
            return

        maximo_anterior = int(fila["maximo_grupos"] or 0)
        alcanzo_antes = bool(fila["alcanzo_7de7"])
        maximo_nuevo = max(maximo_anterior, actuales)
        alcanzo_ahora = alcanzo_antes or actuales >= TOTAL_GRUPOS_OBLIGATORIOS
        perdio = bool(fila["perdio_grupos"]) or (
            alcanzo_ahora and actuales < TOTAL_GRUPOS_OBLIGATORIOS
        )
        primera = fila["fecha_primera_verificacion"] or ahora

        conexion.execute(
            """
            UPDATE usuarios_membresia
            SET grupos_actuales = ?,
                maximo_grupos = ?,
                alcanzo_7de7 = ?,
                perdio_grupos = ?,
                total_verificaciones = COALESCE(total_verificaciones, 0) + 1,
                fecha_primera_verificacion = ?,
                fecha_ultima_verificacion = ?,
                fecha_ultimo_acceso = ?,
                fecha_actualizacion = ?
            WHERE user_id = ?
            """,
            (
                actuales,
                maximo_nuevo,
                1 if alcanzo_ahora else 0,
                1 if perdio else 0,
                primera,
                ahora,
                ahora,
                ahora,
                user_id,
            ),
        )
        conexion.commit()


def guardar_panel_union_admin_db(admin_id, message_id):
    with conectar_db() as conexion:
        conexion.execute(
            """
            INSERT INTO paneles_union_admin (admin_id, message_id, fecha_actualizacion)
            VALUES (?, ?, ?)
            ON CONFLICT(admin_id) DO UPDATE SET
                message_id = excluded.message_id,
                fecha_actualizacion = excluded.fecha_actualizacion
            """,
            (admin_id, message_id, datetime.now(timezone.utc).isoformat()),
        )
        conexion.commit()


def obtener_panel_union_admin_db(admin_id):
    with conectar_db() as conexion:
        fila = conexion.execute(
            "SELECT message_id FROM paneles_union_admin WHERE admin_id = ?",
            (admin_id,),
        ).fetchone()
    return int(fila["message_id"]) if fila else None


def eliminar_panel_union_admin_db(admin_id):
    with conectar_db() as conexion:
        conexion.execute(
            "DELETE FROM paneles_union_admin WHERE admin_id = ?",
            (admin_id,),
        )
        conexion.commit()


def resumen_monitoreo_union_db():
    with conectar_db() as conexion:
        totales = conexion.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN union_bot_iniciado = 1 AND total_verificaciones = 0 THEN 1 ELSE 0 END) AS sin_verificar,
                SUM(CASE WHEN grupos_actuales = 0 THEN 1 ELSE 0 END) AS cero,
                SUM(CASE WHEN grupos_actuales BETWEEN 1 AND 6 THEN 1 ELSE 0 END) AS proceso,
                SUM(CASE WHEN grupos_actuales = 7 THEN 1 ELSE 0 END) AS completos,
                SUM(CASE WHEN perdio_grupos = 1 THEN 1 ELSE 0 END) AS perdieron
            FROM usuarios_membresia
            WHERE union_bot_iniciado = 1
            """
        ).fetchone()

        origenes = conexion.execute(
            """
            SELECT
                COALESCE(origen_nombre, origen_username, 'Acceso directo / sin origen') AS origen,
                COUNT(*) AS total
            FROM usuarios_membresia
            WHERE union_bot_iniciado = 1
            GROUP BY origen_chat_id, origen_username, origen_nombre
            ORDER BY total DESC, origen ASC
            """
        ).fetchall()

        recientes = conexion.execute(
            """
            SELECT user_id, username, nombre, grupos_actuales, fecha_ultimo_acceso
            FROM usuarios_membresia
            WHERE union_bot_iniciado = 1
            ORDER BY COALESCE(fecha_ultimo_acceso, fecha_actualizacion) DESC
            LIMIT 8
            """
        ).fetchall()

    return {"totales": totales, "origenes": origenes, "recientes": recientes}


def texto_monitoreo_union():
    resumen = resumen_monitoreo_union_db()
    t = resumen["totales"]

    lineas = [
        "ðŸ“Š <b>MONITOREO DE MEMBRESÃA</b>",
        "",
        f"ðŸ‘¥ Usuarios registrados: <b>{int(t['total'] or 0)}</b>",
        f"â³ Iniciaron sin verificar: <b>{int(t['sin_verificar'] or 0)}</b>",
        f"ðŸ”´ Estado 0/7: <b>{int(t['cero'] or 0)}</b>",
        f"ðŸŸ¡ Estado 1â€“6/7: <b>{int(t['proceso'] or 0)}</b>",
        f"âœ… Estado 7/7: <b>{int(t['completos'] or 0)}</b>",
        f"â†©ï¸ Perdieron grupos despuÃ©s: <b>{int(t['perdieron'] or 0)}</b>",
        "",
        "ðŸ“ <b>ORIGEN DE LOS ACCESOS</b>",
    ]

    if resumen["origenes"]:
        for fila in resumen["origenes"]:
            lineas.append(
                f"â€¢ {html.escape(str(fila['origen']))}: <b>{int(fila['total'])}</b>"
            )
    else:
        lineas.append("â€¢ TodavÃ­a sin registros")

    lineas.extend(["", "ðŸ• <b>ACCESOS RECIENTES</b>"])

    if resumen["recientes"]:
        for fila in resumen["recientes"]:
            identidad = fila["username"] or fila["nombre"] or str(fila["user_id"])
            lineas.append(
                f"â€¢ {html.escape(str(identidad))} Â· "
                f"<b>{int(fila['grupos_actuales'] or 0)}/7</b> Â· "
                f"{formatear_fecha_peru(fila['fecha_ultimo_acceso'])}"
            )
    else:
        lineas.append("â€¢ TodavÃ­a sin registros")

    lineas.extend(["", "Actualizado: " + formatear_fecha_peru(datetime.now(timezone.utc).isoformat())])
    return "\n".join(lineas)


def teclado_monitoreo_union():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ðŸ”„ ACTUALIZAR", callback_data="union_admin_actualizar")],
        [InlineKeyboardButton("ðŸ—‘ CERRAR", callback_data="union_admin_cerrar")],
    ])


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
        raise RuntimeError("MaximoControlGroup todavÃ­a no estÃ¡ inicializado.")

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
            "âœ… <b>MEMBRESÃA COMPLETA</b>\n\n"
            f"Progreso: <b>{completos}/{total}</b>\n\n"
            "Ya perteneces a todos los grupos oficiales requeridos.\n\n"
            "ðŸ’¬ Puedes participar con texto normal.\n"
            "ðŸ›¡ï¸ La publicidad quedarÃ¡ sujeta al Control Publicitario General "
            "cuando activemos ese mÃ³dulo."
        )

    lineas = [
        "ðŸ” <b>MEMBRESÃA DE USUARIO</b>",
        "",
        f"Progreso: <b>{completos}/{total}</b>",
        "",
        "Te faltan estos grupos:",
        "",
    ]

    for grupo in estado["faltantes"]:
        lineas.append(f"âŒ {grupo['nombre']}")

    lineas.extend(
        [
            "",
            "Usa los botones de abajo para ingresar.",
            "Luego pulsa <b>ðŸ”„ VERIFICAR MEMBRESÃA</b>.",
        ]
    )

    if estado["errores"]:
        lineas.extend(
            [
                "",
                "âš ï¸ Alguna comprobaciÃ³n no pudo confirmarse. "
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
                    f"âž• {grupo['nombre']}",
                    url=grupo["enlace"],
                )
            ]
        )

    filas.append(
        [
            InlineKeyboardButton(
                "ðŸ”„ VERIFICAR MEMBRESÃA",
                callback_data="union_verificar",
            )
        ]
    )

    return InlineKeyboardMarkup(filas)


async def mostrar_o_actualizar_panel_union(user_id):
    if UNION_APP_REF is None:
        return False

    estado = await obtener_estado_membresia_7de7(user_id)
    registrar_verificacion_membresia_db(user_id, estado)
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
            # que el panel estÃ© perdido, asÃ­ que no debemos crear otro.
            if "message is not modified" in str(error).lower():
                return True

            # Para cualquier otro error (por ejemplo, el usuario borrÃ³
            # manualmente el panel), se crea uno nuevo mÃ¡s abajo.
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
            "probablemente todavÃ­a no iniciÃ³ @UnionMembresia_bot.",
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
        "ðŸ”’ <b>MEMBRESÃA PENDIENTE</b>\n\n"
        f"ðŸ‘¤ <b>Nombre:</b> {nombre}\n"
        f"ðŸ”— <b>Usuario:</b> {username}\n"
        f"ðŸ†” <b>ID:</b> <code>{user_id}</code>\n"
        f"ðŸ·ï¸ <b>Tipo:</b> {tipo}\n"
        f"ðŸ›¡ï¸ <b>Rol:</b> {rol}\n"
        f"ðŸ“Š <b>MembresÃ­a:</b> {progreso}\n\n"
        "Para participar debes completar tu membresÃ­a en los 7 grupos oficiales.\n\n"
        "Pulsa el botÃ³n para continuar de forma privada."
    )

    # Primero enviamos el aviso para obtener su message_id.
    aviso = await context.bot.send_message(
        chat_id=chat_id,
        text=texto_aviso,
        parse_mode="HTML",
    )

    # El payload identifica el aviso exacto que originÃ³ el acceso.
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
                        "ðŸ” COMPLETAR MEMBRESÃA",
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
    # Bots externos: cualquier publicaciÃ³n se considera controlable.
    if usuario is not None and getattr(usuario, "is_bot", False):
        if es_bot_oficial_exento(usuario):
            return None
        tipo = clasificar_contenido_mensaje(mensaje)
        return tipo if tipo != "OTRO" else "BOT"

    tipo = clasificar_contenido_mensaje(mensaje)

    if tipo in {
        "FOTO",
        "VIDEO",
        "GIF/ANIMACIÃ“N",
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
        "GIF/ANIMACIÃ“N": "controlar_gif",
        "DOCUMENTO": "controlar_documento",
        "TEXTO + ENLACE": "controlar_enlace",
        "CUSTOM EMOJI": "controlar_custom_emoji",
    }

    # Para bots externos, cualquier formato no reconocido especÃ­ficamente
    # continÃºa bajo control.
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


def formatear_intervalo_segundos(segundos):
    if segundos is None:
        return "No disponible"

    try:
        segundos = int(round(float(segundos)))
    except (TypeError, ValueError):
        return "No disponible"

    if segundos < 60:
        return f"{segundos} s"

    minutos = segundos // 60

    if minutos < 60:
        return f"{minutos} min"

    horas, minutos_restantes = divmod(minutos, 60)

    if horas < 24:
        if minutos_restantes:
            return f"{horas} h {minutos_restantes} min"
        return f"{horas} h"

    dias, horas_restantes = divmod(horas, 24)

    if horas_restantes:
        return f"{dias} d {horas_restantes} h"

    return f"{dias} d"


def resumen_frecuencia_publicidad_db(
    identidad_tipo,
    identidad_id,
    max_eventos=20,
):
    """
    Analiza intentos/publicaciones que el motor clasificÃ³ como publicidad.
    Incluye permitidas y rechazadas para reflejar el comportamiento real
    de la identidad, no solamente lo que finalmente quedÃ³ visible.
    """
    ahora = datetime.now(timezone.utc)
    desde_hora = (ahora - timedelta(hours=1)).isoformat()
    desde_24h = (ahora - timedelta(hours=24)).isoformat()
    inicio_dia_peru = datetime.now(ZONA_PERU).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ).astimezone(timezone.utc).isoformat()

    with conectar_db() as conexion:
        fila = conexion.execute(
            """
            SELECT
                SUM(CASE WHEN fecha_evento >= ? THEN 1 ELSE 0 END) AS ultima_hora,
                SUM(CASE WHEN fecha_evento >= ? THEN 1 ELSE 0 END) AS ultimas_24h,
                SUM(CASE WHEN fecha_evento >= ? THEN 1 ELSE 0 END) AS hoy,
                SUM(
                    CASE
                        WHEN fecha_evento >= ? AND decision = 'PERMITIDA'
                        THEN 1 ELSE 0
                    END
                ) AS permitidas_hora,
                SUM(
                    CASE
                        WHEN fecha_evento >= ? AND decision <> 'PERMITIDA'
                        THEN 1 ELSE 0
                    END
                ) AS rechazadas_hora,
                MAX(fecha_evento) AS ultima_publicidad
            FROM eventos_publicidad_control
            WHERE identidad_tipo = ?
              AND identidad_id = ?
            """,
            (
                desde_hora,
                desde_24h,
                inicio_dia_peru,
                desde_hora,
                desde_hora,
                identidad_tipo,
                identidad_id,
            ),
        ).fetchone()

        eventos = conexion.execute(
            """
            SELECT fecha_evento, decision, tipo_contenido
            FROM eventos_publicidad_control
            WHERE identidad_tipo = ?
              AND identidad_id = ?
            ORDER BY fecha_evento DESC
            LIMIT ?
            """,
            (
                identidad_tipo,
                identidad_id,
                int(max_eventos),
            ),
        ).fetchall()

        grupos_hora = conexion.execute(
            """
            SELECT
                COALESCE(
                    chat_nombre,
                    chat_username,
                    CAST(chat_id AS TEXT)
                ) AS grupo,
                COUNT(*) AS total
            FROM eventos_publicidad_control
            WHERE identidad_tipo = ?
              AND identidad_id = ?
              AND fecha_evento >= ?
            GROUP BY chat_id
            ORDER BY total DESC, grupo ASC
            """,
            (
                identidad_tipo,
                identidad_id,
                desde_hora,
            ),
        ).fetchall()

    fechas = []

    for evento in reversed(eventos):
        try:
            fecha = datetime.fromisoformat(evento["fecha_evento"])
            if fecha.tzinfo is None:
                fecha = fecha.replace(tzinfo=timezone.utc)
            fechas.append(fecha.astimezone(timezone.utc))
        except (TypeError, ValueError):
            continue

    intervalos = []

    for anterior, actual in zip(fechas, fechas[1:]):
        segundos = (actual - anterior).total_seconds()
        if segundos >= 0:
            intervalos.append(segundos)

    promedio_segundos = (
        sum(intervalos) / len(intervalos)
        if intervalos
        else None
    )

    ultimo_intervalo = (
        intervalos[-1]
        if intervalos
        else None
    )

    ultima_hora = int((fila["ultima_hora"] or 0) if fila else 0)
    ultimas_24h = int((fila["ultimas_24h"] or 0) if fila else 0)
    hoy = int((fila["hoy"] or 0) if fila else 0)

    # Promedio equivalente de publicaciones por hora en las Ãºltimas 24 h.
    promedio_por_hora_24h = round(ultimas_24h / 24, 2)

    return {
        "ultima_hora": ultima_hora,
        "ultimas_24h": ultimas_24h,
        "hoy": hoy,
        "permitidas_hora": int(
            (fila["permitidas_hora"] or 0) if fila else 0
        ),
        "rechazadas_hora": int(
            (fila["rechazadas_hora"] or 0) if fila else 0
        ),
        "ultima_publicidad": (
            fila["ultima_publicidad"] if fila else None
        ),
        "promedio_intervalo_segundos": promedio_segundos,
        "ultimo_intervalo_segundos": ultimo_intervalo,
        "promedio_por_hora_24h": promedio_por_hora_24h,
        "muestra_intervalos": len(intervalos),
        "grupos_hora": grupos_hora,
    }


def texto_ritmo_publicitario(resumen):
    if resumen["muestra_intervalos"] <= 0:
        frecuencia = "AÃºn sin muestra suficiente"
        ultimo_intervalo = "No disponible"
    else:
        frecuencia = (
            "1 cada "
            f"{formatear_intervalo_segundos(resumen['promedio_intervalo_segundos'])}"
        )
        ultimo_intervalo = formatear_intervalo_segundos(
            resumen["ultimo_intervalo_segundos"]
        )

    return {
        "frecuencia": frecuencia,
        "ultimo_intervalo": ultimo_intervalo,
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

    # HEREDADO queda preparado para el prÃ³ximo bloque global.
    # Mientras no exista regla global, no impone lÃ­mites individuales.
    if modo == "HEREDADO":
        return True, "HEREDADO Â· SIN REGLA GLOBAL ACTIVA TODAVÃA", cfg, None

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
                        "SEPARACIÃ“N MÃNIMA NO CUMPLIDA",
                        cfg,
                        disponible,
                    )
            except (TypeError, ValueError):
                pass

    limites = limites_periodos_publicidad()
    campos = [
        ("hora", "limite_hora", "LÃMITE POR HORA"),
        ("dia", "limite_dia", "LÃMITE DIARIO"),
        ("semana", "limite_semana", "LÃMITE SEMANAL"),
        ("mes", "limite_mes", "LÃMITE MENSUAL"),
        ("anio", "limite_anio", "LÃMITE ANUAL"),
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

    return True, "DENTRO DE LOS LÃMITES", cfg, None


def texto_valor_limite(valor):
    return "SIN LÃMITE" if valor is None else str(valor)


def texto_separacion(segundos):
    if segundos is None:
        return "SIN SEPARACIÃ“N"

    segundos = int(segundos)

    if segundos % 3600 == 0 and segundos >= 3600:
        horas = segundos // 3600
        return f"{horas} h"

    if segundos % 60 == 0:
        return f"{segundos // 60} min"

    return f"{segundos} s"


def resumen_tipos_controlados(cfg):
    pares = [
        ("Foto", "controlar_foto"),
        ("Video", "controlar_video"),
        ("GIF", "controlar_gif"),
        ("Documento", "controlar_documento"),
        ("Enlace", "controlar_enlace"),
        ("Custom emoji", "controlar_custom_emoji"),
    ]
    activos = [nombre for nombre, campo in pares if bool(cfg[campo])]
    libres = [nombre for nombre, campo in pares if not bool(cfg[campo])]
    return activos, libres


def proxima_disponibilidad_separacion(identidad_tipo, identidad_id, cfg):
    separacion = cfg["separacion_segundos"]
    if separacion is None or int(separacion) <= 0:
        return None

    ultima = ultima_publicidad_permitida_db(identidad_tipo, identidad_id)
    if not ultima:
        return None

    try:
        fecha = datetime.fromisoformat(ultima)
        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)
        disponible = fecha + timedelta(seconds=int(separacion))
        if disponible > datetime.now(timezone.utc):
            return disponible
    except (TypeError, ValueError):
        return None

    return None


def captura_pertenece_propietario(captura, propietario_id):
    return bool(
        captura
        and int(captura["propietario_id"]) == int(propietario_id)
    )


def teclado_control_publicidad(captura_id, cfg):
    modo = str(cfg["modo"] or "HEREDADO").upper()

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"âš™ï¸ MODO: {modo}",
                callback_data=f"orma_pub_modo:{captura_id}",
            )
        ],
        [
            InlineKeyboardButton(
                f"â± SEPARACIÃ“N: {texto_separacion(cfg['separacion_segundos'])}",
                callback_data=f"orma_pub_sep:{captura_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "ðŸ”¢ LÃMITES",
                callback_data=f"orma_pub_limites:{captura_id}",
            ),
            InlineKeyboardButton(
                "ðŸŽ› TIPOS",
                callback_data=f"orma_pub_tipos:{captura_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "â™»ï¸ RESTAURAR HEREDADO",
                callback_data=f"orma_pub_reset:{captura_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "â¬…ï¸ RETROCEDER",
                callback_data=f"orma_ficha:{captura_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "ðŸ  MENÃš PRINCIPAL",
                callback_data="orma_menu_principal",
            ),
            InlineKeyboardButton(
                "ðŸ—‘ CERRAR",
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
    ritmo = resumen_frecuencia_publicidad_db(
        captura["objetivo_tipo"],
        captura["objetivo_id"],
    )
    ritmo_txt = texto_ritmo_publicitario(ritmo)
    activos, libres = resumen_tipos_controlados(cfg)
    disponible = proxima_disponibilidad_separacion(
        captura["objetivo_tipo"],
        captura["objetivo_id"],
        cfg,
    )

    modo = str(cfg["modo"] or "HEREDADO").upper()
    if modo == "HEREDADO":
        efecto = "UsarÃ¡ la regla general cuando el mÃ³dulo global estÃ© activo"
    elif modo == "PERSONALIZADO":
        efecto = "Aplica separaciÃ³n, lÃ­mites y tipos propios"
    elif modo == "ILIMITADO":
        efecto = "Registra publicidad, pero no la limita"
    elif modo == "BLOQUEADO":
        efecto = "Elimina toda publicidad controlable"
    else:
        efecto = "Fuera del Control Publicitario Individual"

    proxima = (
        formatear_fecha_peru(disponible.isoformat())
        if disponible is not None
        else "Disponible ahora / no aplica"
    )

    return (
        "ðŸ“£ <b>CONTROL PUBLICITARIO INDIVIDUAL</b>\n\n"
        f"ðŸ‘¤ <b>{captura['objetivo_nombre'] or 'Sin nombre'}</b>\n"
        f"ðŸ†” <code>{captura['objetivo_id']}</code>\n"
        f"ðŸ·ï¸ Tipo: <b>{captura['objetivo_tipo']}</b>\n\n"

        "âš™ï¸ <b>ESTADO DEL CONTROL</b>\n"
        f"â€¢ Modo: <b>{modo}</b>\n"
        f"â€¢ Efecto: <b>{efecto}</b>\n"
        f"â€¢ SeparaciÃ³n: <b>{texto_separacion(cfg['separacion_segundos'])}</b>\n"
        f"â€¢ PrÃ³xima por separaciÃ³n: <b>{proxima}</b>\n\n"

        "ðŸ”¢ <b>LÃMITES PERSONALIZADOS</b>\n"
        f"â€¢ Hora: <b>{texto_valor_limite(cfg['limite_hora'])}</b> "
        f"Â· usados {uso['hora']}\n"
        f"â€¢ DÃ­a: <b>{texto_valor_limite(cfg['limite_dia'])}</b> "
        f"Â· usados {uso['dia']}\n"
        f"â€¢ Semana: <b>{texto_valor_limite(cfg['limite_semana'])}</b> "
        f"Â· usados {uso['semana']}\n"
        f"â€¢ Mes: <b>{texto_valor_limite(cfg['limite_mes'])}</b> "
        f"Â· usados {uso['mes']}\n"
        f"â€¢ AÃ±o: <b>{texto_valor_limite(cfg['limite_anio'])}</b> "
        f"Â· usados {uso['anio']}\n\n"

        "ðŸŽ› <b>TIPOS</b>\n"
        f"â€¢ Controlados: <b>{', '.join(activos) if activos else 'NINGUNO'}</b>\n"
        f"â€¢ Libres por excepciÃ³n: <b>{', '.join(libres) if libres else 'NINGUNO'}</b>\n"
        "â€¢ Texto normal puro: <b>SIEMPRE LIBRE</b>\n\n"

        "ðŸ“Š <b>COMPORTAMIENTO OBSERVADO</b>\n"
        f"â€¢ Publicidad Ãºltima hora: <b>{ritmo['ultima_hora']}</b>\n"
        f"â€¢ Ãšltimas 24 h: <b>{ritmo['ultimas_24h']}</b>\n"
        f"â€¢ Frecuencia: <b>{ritmo_txt['frecuencia']}</b>\n"
        f"â€¢ Ãšltima publicidad: "
        f"<b>{formatear_fecha_peru(ritmo['ultima_publicidad'])}</b>\n\n"

        "Selecciona quÃ© deseas modificar."
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
            "\\nâ³ PrÃ³xima disponibilidad: "
            f"<b>{formatear_fecha_peru(disponible.isoformat())}</b>"
        )

    user_text = f"@{username}" if username else "Sin @username"

    aviso = await context.bot.send_message(
        chat_id=chat.id,
        text=(
            "â›” <b>PUBLICIDAD NO PERMITIDA</b>\\n\\n"
            f"ðŸ‘¤ <b>Nombre:</b> {nombre}\\n"
            f"ðŸ”— <b>Usuario:</b> {user_text}\\n"
            f"ðŸ†” <b>ID:</b> <code>{identidad_id}</code>\\n"
            f"ðŸ·ï¸ <b>Tipo:</b> {tipo_identidad}\\n"
            f"ðŸ“¦ <b>Contenido:</b> {tipo_contenido}\\n"
            f"âš ï¸ <b>Motivo:</b> {motivo}"
            f"{extra}\\n\\n"
            "ðŸ’¬ Puedes continuar escribiendo texto normal."
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
        return "GIF/ANIMACIÃ“N"

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
        texto = fecha.strftime("%d/%m/%Y Â· %I:%M %p")
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


def obtener_resumen_identidad_orma(objetivo_tipo, objetivo_id):
    """
    Resumen compacto para la ficha principal /orma.
    Usa Ãºnicamente informaciÃ³n que MÃ¡ximo ya ha observado/registrado.
    """
    with conectar_db() as conexion:
        actividad = conexion.execute(
            """
            SELECT
                COUNT(*) AS total,
                MIN(fecha_evento) AS primera_actividad,
                MAX(fecha_evento) AS ultima_actividad
            FROM actividad_grupo
            WHERE identidad_tipo = ?
              AND identidad_id = ?
            """,
            (objetivo_tipo, objetivo_id),
        ).fetchone()

        publicidad = conexion.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN decision = 'PERMITIDA' THEN 1 ELSE 0 END) AS permitidas,
                SUM(CASE WHEN decision <> 'PERMITIDA' THEN 1 ELSE 0 END) AS bloqueadas
            FROM eventos_publicidad_control
            WHERE identidad_tipo = ?
              AND identidad_id = ?
            """,
            (objetivo_tipo, objetivo_id),
        ).fetchone()

    usuario_db = (
        obtener_usuario_membresia_db(objetivo_id)
        if objetivo_tipo in {"USUARIO", "BOT"}
        else None
    )

    movimientos = (
        resumen_movimientos_db(objetivo_id)
        if objetivo_tipo in {"USUARIO", "BOT"}
        else {
            "entradas": 0,
            "salidas": 0,
            "primera_entrada": None,
            "ultima_entrada": None,
            "ultima_salida": None,
            "por_grupo": [],
        }
    )

    actividad_periodos = resumen_actividad_db(objetivo_tipo, objetivo_id)

    return {
        "primer_contacto": (
            usuario_db["fecha_primer_contacto"]
            if usuario_db
            else None
        ),
        "ultima_actualizacion_identidad": (
            usuario_db["fecha_actualizacion"]
            if usuario_db
            else None
        ),
        "actividad_total": int(
            actividad["total"] if actividad and actividad["total"] else 0
        ),
        "primera_actividad": (
            actividad["primera_actividad"] if actividad else None
        ),
        "ultima_actividad": (
            actividad["ultima_actividad"] if actividad else None
        ),
        "actividad_hora": actividad_periodos["hora"],
        "actividad_dia": actividad_periodos["dia"],
        "actividad_semana": actividad_periodos["semana"],
        "actividad_mes": actividad_periodos["mes"],
        "publicidad_total": int(
            publicidad["total"] if publicidad and publicidad["total"] else 0
        ),
        "publicidad_permitida": int(
            publicidad["permitidas"]
            if publicidad and publicidad["permitidas"]
            else 0
        ),
        "publicidad_bloqueada": int(
            publicidad["bloqueadas"]
            if publicidad and publicidad["bloqueadas"]
            else 0
        ),
        "ritmo_publicidad": resumen_frecuencia_publicidad_db(
            objetivo_tipo,
            objetivo_id,
        ),
        "entradas": movimientos["entradas"],
        "salidas": movimientos["salidas"],
        "primera_entrada": movimientos["primera_entrada"],
        "ultima_entrada": movimientos["ultima_entrada"],
        "ultima_salida": movimientos["ultima_salida"],
    }


def texto_modo_publicidad_ficha(objetivo_tipo, objetivo_id):
    try:
        cfg = obtener_control_identidad_db(objetivo_tipo, objetivo_id)
        modo = str(cfg["modo"] or "HEREDADO").upper()
        separacion = texto_separacion(cfg["separacion_segundos"])
        return f"{modo} Â· {separacion}"
    except Exception:
        logging.exception(
            "No se pudo obtener control publicitario para ficha objetivo=%s",
            objetivo_id,
        )
        return "No disponible"


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
        [InlineKeyboardButton("ðŸ”„ ACTUALIZAR FICHA", callback_data=f"orma_ficha:{captura_id}")],
        [
            InlineKeyboardButton("ðŸ” MEMBRESÃA", callback_data=f"orma_membresia:{captura_id}"),
            InlineKeyboardButton("ðŸ“£ PUBLICIDAD", callback_data=f"orma_publicidad:{captura_id}"),
        ],
        [
            InlineKeyboardButton("ðŸ“Š ACTIVIDAD", callback_data=f"orma_actividad:{captura_id}"),
            InlineKeyboardButton("ðŸšª ENTRADAS / SALIDAS", callback_data=f"orma_movimientos:{captura_id}"),
        ],
        [
            InlineKeyboardButton("ðŸ  MENÃš PRINCIPAL", callback_data="orma_menu_principal"),
            InlineKeyboardButton("ðŸ—‘ CERRAR", callback_data="orma_cerrar"),
        ],
    ])


async def construir_texto_ficha_orma(captura):
    objetivo_id = captura["objetivo_id"]
    username = (
        f"@{captura['objetivo_username']}"
        if captura["objetivo_username"]
        else "Sin @username"
    )
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
            logging.exception(
                "Error obteniendo membresÃ­a para /orma objetivo=%s",
                objetivo_id,
            )
            progreso = "No disponible"

    oficial = (
        captura["objetivo_tipo"] == "BOT"
        and (captura["objetivo_username"] or "").lower()
        in BOTS_OFICIALES_EXENTOS
    )

    if oficial:
        condicion = "âœ… BOT OFICIAL Â· EXENTO DE RAÃZ"
    elif progreso == f"{total}/{total}":
        condicion = "ðŸŸ¢ HABILITADO"
    elif progreso == "No aplica":
        condicion = "âšª IDENTIDAD DE CHAT/CANAL"
    elif progreso == "No disponible":
        condicion = "ðŸŸ¡ ESTADO NO DISPONIBLE"
    else:
        condicion = f"ðŸ”´ MEMBRESÃA INCOMPLETA Â· faltan {faltantes}"

    resumen = obtener_resumen_identidad_orma(
        captura["objetivo_tipo"],
        objetivo_id,
    )

    capturas_totales = contar_capturas_objetivo_orma(
        captura["objetivo_tipo"],
        objetivo_id,
    )

    modo_publicidad = texto_modo_publicidad_ficha(
        captura["objetivo_tipo"],
        objetivo_id,
    )

    primera_observacion = (
        resumen["primer_contacto"]
        or resumen["primera_actividad"]
        or captura["fecha_captura"]
    )

    ultima_observacion = (
        resumen["ultima_actividad"]
        or resumen["ultima_actualizacion_identidad"]
        or captura["fecha_captura"]
    )

    return (
        "ðŸ›¡ï¸ <b>FICHA AVANZADA /ORMA</b>\n\n"

        "ðŸ‘¤ <b>IDENTIDAD</b>\n"
        f"â€¢ Nombre: <b>{nombre}</b>\n"
        f"â€¢ Usuario: <b>{username}</b>\n"
        f"â€¢ ID: <code>{objetivo_id}</code>\n"
        f"â€¢ Tipo: <b>{captura['objetivo_tipo']}</b>\n"
        f"â€¢ Rol en grupo origen: <b>{rol}</b>\n\n"

        "ðŸ” <b>ESTADO GENERAL</b>\n"
        f"â€¢ MembresÃ­a: <b>{progreso}</b>\n"
        f"â€¢ CondiciÃ³n: <b>{condicion}</b>\n"
        f"â€¢ Control publicidad: <b>{modo_publicidad}</b>\n\n"

        "ðŸ“Š <b>ACTIVIDAD OBSERVADA</b>\n"
        f"â€¢ Ãšltima hora: <b>{resumen['actividad_hora']}</b>\n"
        f"â€¢ Hoy: <b>{resumen['actividad_dia']}</b>\n"
        f"â€¢ Semana: <b>{resumen['actividad_semana']}</b>\n"
        f"â€¢ Mes: <b>{resumen['actividad_mes']}</b>\n"
        f"â€¢ Total registrado: <b>{resumen['actividad_total']}</b>\n\n"

        "ðŸ“£ <b>PUBLICIDAD REGISTRADA</b>\n"
        f"â€¢ Total evaluada: <b>{resumen['publicidad_total']}</b>\n"
        f"â€¢ Permitida: <b>{resumen['publicidad_permitida']}</b>\n"
        f"â€¢ Rechazada/controlada: <b>{resumen['publicidad_bloqueada']}</b>\n"
        f"â€¢ Ãšltima hora: <b>{resumen['ritmo_publicidad']['ultima_hora']}</b> "
        f"(âœ… {resumen['ritmo_publicidad']['permitidas_hora']} Â· "
        f"â›” {resumen['ritmo_publicidad']['rechazadas_hora']})\n"
        f"â€¢ Ãšltimas 24 h: <b>{resumen['ritmo_publicidad']['ultimas_24h']}</b>\n"
        f"â€¢ Ritmo promedio: <b>{texto_ritmo_publicitario(resumen['ritmo_publicidad'])['frecuencia']}</b>\n"
        f"â€¢ Ãšltimo intervalo: <b>{texto_ritmo_publicitario(resumen['ritmo_publicidad'])['ultimo_intervalo']}</b>\n"
        f"â€¢ Ãšltima publicidad: <b>{formatear_fecha_peru(resumen['ritmo_publicidad']['ultima_publicidad'])}</b>\n\n"

        "ðŸšª <b>MOVIMIENTOS OBSERVADOS</b>\n"
        f"â€¢ Entradas: <b>{resumen['entradas']}</b>\n"
        f"â€¢ Salidas: <b>{resumen['salidas']}</b>\n"
        f"â€¢ Primera entrada: <b>{formatear_fecha_peru(resumen['primera_entrada'])}</b>\n"
        f"â€¢ Ãšltima entrada: <b>{formatear_fecha_peru(resumen['ultima_entrada'])}</b>\n"
        f"â€¢ Ãšltima salida: <b>{formatear_fecha_peru(resumen['ultima_salida'])}</b>\n\n"

        "ðŸ• <b>SEGUIMIENTO</b>\n"
        f"â€¢ Primera observaciÃ³n: <b>{formatear_fecha_peru(primera_observacion)}</b>\n"
        f"â€¢ Ãšltima actividad: <b>{formatear_fecha_peru(ultima_observacion)}</b>\n"
        f"â€¢ Capturas /orma: <b>{capturas_totales}</b>\n\n"

        "ðŸ“ <b>ORIGEN DE ESTA CAPTURA</b>\n"
        f"â€¢ Grupo: <b>{captura['chat_nombre'] or captura['chat_username'] or captura['chat_id']}</b>\n"
        f"â€¢ Mensaje: <code>{captura['mensaje_origen_id']}</code>\n"
        f"â€¢ Fecha: <b>{formatear_fecha_peru(captura['fecha_captura'])}</b>\n\n"

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
        ENTRADAS_CONTROL_PUBLICIDAD.pop(usuario.id, None)
        await query.answer()
        try:
            await query.message.delete()
        except TelegramError:
            pass
        PANELES_ORMA.pop(usuario.id, None)
        eliminar_panel_orma_db(usuario.id)
        return

    if data == "orma_menu_principal":
        ENTRADAS_CONTROL_PUBLICIDAD.pop(usuario.id, None)
        await query.answer()
        try:
            await query.edit_message_text(
                "ðŸ›¡ï¸ <b>MÃXIMO CONTROL GROUP</b>\n\n"
                "Centro privado de administraciÃ³n.\n\n"
                "ðŸ“Œ Responde cualquier mensaje en un grupo controlado "
                "con <code>/orma</code> para abrir su expediente.\n\n"
                "Los comandos y datos escritos se eliminan automÃ¡ticamente "
                "despuÃ©s de ser procesados.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("ðŸ—‘ CERRAR", callback_data="orma_cerrar")
                ]]),
            )
        except TelegramError:
            pass
        return

    if data.startswith("orma_ficha:"):
        ENTRADAS_CONTROL_PUBLICIDAD.pop(usuario.id, None)
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
                "ðŸ” <b>MEMBRESÃA</b>\n\n"
                "Esta identidad es un canal/chat y no puede evaluarse con la regla de usuario 7/7."
            )
        else:
            estado = await obtener_estado_membresia_7de7(captura["objetivo_id"])
            lineas = [
                "ðŸ” <b>MEMBRESÃA 7/7</b>", "",
                f"Progreso: <b>{len(estado['completados'])}/{estado['total']}</b>", "",
            ]
            if estado["completo"]:
                lineas.append("âœ… Pertenece a los 7 grupos oficiales.")
            else:
                lineas.append("âŒ <b>Grupos faltantes:</b>")
                for grupo in estado["faltantes"]:
                    lineas.append(f"â€¢ {grupo['nombre']}")
            texto_membresia = "\n".join(lineas)

        await query.edit_message_text(
            texto_membresia,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("ðŸ”„ VERIFICAR AHORA", callback_data=f"orma_membresia:{captura_id}")],
                [InlineKeyboardButton("â¬…ï¸ RETROCEDER", callback_data=f"orma_ficha:{captura_id}")],
                [
                    InlineKeyboardButton("ðŸ  MENÃš PRINCIPAL", callback_data="orma_menu_principal"),
                    InlineKeyboardButton("ðŸ—‘ CERRAR", callback_data="orma_cerrar"),
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
            "ðŸ“Š <b>ACTIVIDAD REGISTRADA</b>",
            "",
            "ðŸ• <b>Volumen</b>",
            f"â€¢ Ãšltima hora: <b>{resumen['hora']}</b>",
            f"â€¢ Hoy: <b>{resumen['dia']}</b>",
            f"â€¢ Semana: <b>{resumen['semana']}</b>",
            f"â€¢ Mes: <b>{resumen['mes']}</b>",
            "",
            "ðŸ“¦ <b>Tipos de contenido este mes</b>",
        ]

        if resumen["tipos_mes"]:
            for fila in resumen["tipos_mes"][:8]:
                lineas.append(
                    f"â€¢ {fila['tipo_contenido']}: <b>{fila['total']}</b>"
                )
        else:
            lineas.append("â€¢ Sin actividad registrada todavÃ­a.")

        lineas.extend(["", "ðŸ“ <b>Actividad por grupo este mes</b>"])

        if resumen["grupos_mes"]:
            for fila in resumen["grupos_mes"][:7]:
                lineas.append(
                    f"â€¢ {fila['grupo']}: <b>{fila['total']}</b>"
                )
        else:
            lineas.append("â€¢ Sin actividad registrada todavÃ­a.")

        ritmo_pub = resumen_frecuencia_publicidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )
        ritmo_txt = texto_ritmo_publicitario(ritmo_pub)

        lineas.extend([
            "",
            "ðŸ“£ <b>Ritmo publicitario observado</b>",
            f"â€¢ Ãšltima hora: <b>{ritmo_pub['ultima_hora']}</b>",
            f"â€¢ Permitidas Ãºltima hora: <b>{ritmo_pub['permitidas_hora']}</b>",
            f"â€¢ Rechazadas Ãºltima hora: <b>{ritmo_pub['rechazadas_hora']}</b>",
            f"â€¢ Ãšltimas 24 h: <b>{ritmo_pub['ultimas_24h']}</b>",
            f"â€¢ Promedio equivalente / hora (24 h): "
            f"<b>{ritmo_pub['promedio_por_hora_24h']}</b>",
            f"â€¢ Frecuencia promedio: <b>{ritmo_txt['frecuencia']}</b>",
            f"â€¢ Ãšltimo intervalo: <b>{ritmo_txt['ultimo_intervalo']}</b>",
            f"â€¢ Ãšltima publicidad: "
            f"<b>{formatear_fecha_peru(ritmo_pub['ultima_publicidad'])}</b>",
        ])

        if ritmo_pub["grupos_hora"]:
            lineas.extend(["", "ðŸ“ <b>Publicidad por grupo Â· Ãºltima hora</b>"])
            for fila in ritmo_pub["grupos_hora"][:7]:
                lineas.append(
                    f"â€¢ {fila['grupo']}: <b>{fila['total']}</b>"
                )

        lineas.extend([
            "",
            "â„¹ï¸ La frecuencia se calcula con publicidad detectada por "
            "MÃ¡ximo Control, incluida la que haya sido rechazada por reglas. "
            "El historial empieza desde que el sistema registra estos eventos.",
        ])

        await query.edit_message_text(
            "\n".join(lineas),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "ðŸ”„ ACTUALIZAR",
                        callback_data=f"orma_actividad:{captura_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "â¬…ï¸ RETROCEDER",
                        callback_data=f"orma_ficha:{captura_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "ðŸ  MENÃš PRINCIPAL",
                        callback_data="orma_menu_principal",
                    ),
                    InlineKeyboardButton(
                        "ðŸ—‘ CERRAR",
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
                "ðŸšª <b>ENTRADAS / SALIDAS</b>",
                "",
                "Esta identidad es un canal/chat y no tiene historial "
                "de membresÃ­a de usuario.",
            ]
        else:
            resumen = resumen_movimientos_db(captura["objetivo_id"])

            lineas = [
                "ðŸšª <b>ENTRADAS / SALIDAS</b>",
                "",
                f"âž• Entradas registradas: <b>{resumen['entradas']}</b>",
                f"âž– Salidas registradas: <b>{resumen['salidas']}</b>",
                "",
                f"ðŸŸ¢ Primera entrada observada: "
                f"<b>{formatear_fecha_peru(resumen['primera_entrada'])}</b>",
                f"ðŸ”„ Ãšltima entrada: "
                f"<b>{formatear_fecha_peru(resumen['ultima_entrada'])}</b>",
                f"ðŸ”´ Ãšltima salida: "
                f"<b>{formatear_fecha_peru(resumen['ultima_salida'])}</b>",
                "",
                "ðŸ“ <b>Por grupo</b>",
            ]

            if resumen["por_grupo"]:
                for fila in resumen["por_grupo"][:7]:
                    lineas.append(
                        f"â€¢ {fila['grupo']}: "
                        f"âž• {int(fila['entradas'] or 0)} Â· "
                        f"âž– {int(fila['salidas'] or 0)}"
                    )
            else:
                lineas.append("â€¢ Sin movimientos registrados todavÃ­a.")

            # Dato de seguimiento Ãºtil sin inventar historial anterior.
            if resumen["ultima_entrada"] and not resumen["ultima_salida"]:
                estado_mov = "ðŸŸ¢ Ãšltimo movimiento registrado: ENTRADA"
            elif resumen["ultima_salida"] and not resumen["ultima_entrada"]:
                estado_mov = "ðŸ”´ Ãšltimo movimiento registrado: SALIDA"
            elif resumen["ultima_entrada"] and resumen["ultima_salida"]:
                try:
                    fe = datetime.fromisoformat(resumen["ultima_entrada"])
                    fs = datetime.fromisoformat(resumen["ultima_salida"])
                    if fe.tzinfo is None:
                        fe = fe.replace(tzinfo=timezone.utc)
                    if fs.tzinfo is None:
                        fs = fs.replace(tzinfo=timezone.utc)
                    estado_mov = (
                        "ðŸŸ¢ Ãšltimo movimiento registrado: ENTRADA"
                        if fe > fs
                        else "ðŸ”´ Ãšltimo movimiento registrado: SALIDA"
                    )
                except (TypeError, ValueError):
                    estado_mov = "âšª Ãšltimo movimiento: No disponible"
            else:
                estado_mov = "âšª Sin movimientos observados todavÃ­a"

            lineas.extend([
                "",
                f"<b>{estado_mov}</b>",
                "",
                "â„¹ï¸ Solo se contabilizan movimientos observados desde "
                "la activaciÃ³n de este registro; no se inventa historial anterior.",
            ])

        await query.edit_message_text(
            "\n".join(lineas),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "ðŸ”„ ACTUALIZAR",
                        callback_data=f"orma_movimientos:{captura_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "â¬…ï¸ RETROCEDER",
                        callback_data=f"orma_ficha:{captura_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "ðŸ  MENÃš PRINCIPAL",
                        callback_data="orma_menu_principal",
                    ),
                    InlineKeyboardButton(
                        "ðŸ—‘ CERRAR",
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
            "âš™ï¸ <b>MODO DE CONTROL</b>\\n\\n"
            "HEREDADO: usarÃ¡ la regla global cuando la activemos.\\n"
            "PERSONALIZADO: aplica lÃ­mites propios.\\n"
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
                [InlineKeyboardButton("â¬…ï¸ RETROCEDER", callback_data=f"orma_publicidad:{captura_id}")],
                [
                    InlineKeyboardButton("ðŸ  MENÃš PRINCIPAL", callback_data="orma_menu_principal"),
                    InlineKeyboardButton("ðŸ—‘ CERRAR", callback_data="orma_cerrar"),
                ],
            ]),
        )
        return

    if data.startswith("orma_pub_setmodo:"):
        _, captura_txt, modo = data.split(":", 2)
        captura_id = int(captura_txt)
        captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            await query.answer("Ficha no disponible.", show_alert=True)
            return

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
            "â± <b>SEPARACIÃ“N ENTRE PUBLICIDADES</b>\\n\\n"
            "Selecciona el tiempo mÃ­nimo entre una publicidad permitida "
            "y la siguiente.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "SIN SEPARACIÃ“N",
                        callback_data=f"orma_pub_setsep:{captura_id}:none",
                    ),
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
                    InlineKeyboardButton("âœï¸ PERSONALIZAR MINUTOS", callback_data=f"orma_pub_input:{captura_id}:separacion_minutos"),
                ],
                [InlineKeyboardButton("â¬…ï¸ RETROCEDER", callback_data=f"orma_publicidad:{captura_id}")],
            ]),
        )
        return

    if data.startswith("orma_pub_setsep:"):
        _, captura_txt, valor = data.split(":", 2)
        captura_id = int(captura_txt)
        captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        segundos = None if valor == "none" else int(valor)

        actualizar_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            modo="PERSONALIZADO",
            separacion_segundos=segundos,
        )
        await query.answer("SeparaciÃ³n actualizada")
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
            "ðŸ”¢ <b>LÃMITES DE PUBLICIDAD</b>\\n\\n"
            f"Hora: <b>{texto_valor_limite(cfg['limite_hora'])}</b>\\n"
            f"DÃ­a: <b>{texto_valor_limite(cfg['limite_dia'])}</b>\\n"
            f"Semana: <b>{texto_valor_limite(cfg['limite_semana'])}</b>\\n"
            f"Mes: <b>{texto_valor_limite(cfg['limite_mes'])}</b>\\n"
            f"AÃ±o: <b>{texto_valor_limite(cfg['limite_anio'])}</b>\\n\\n"
            "Pulsa un periodo y escribe el mÃ¡ximo. "
            "El nÃºmero escrito se borrarÃ¡ automÃ¡ticamente.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("HORA", callback_data=f"orma_pub_input:{captura_id}:limite_hora"),
                    InlineKeyboardButton("DÃA", callback_data=f"orma_pub_input:{captura_id}:limite_dia"),
                ],
                [
                    InlineKeyboardButton("SEMANA", callback_data=f"orma_pub_input:{captura_id}:limite_semana"),
                    InlineKeyboardButton("MES", callback_data=f"orma_pub_input:{captura_id}:limite_mes"),
                    InlineKeyboardButton("AÃ‘O", callback_data=f"orma_pub_input:{captura_id}:limite_anio"),
                ],
                [
                    InlineKeyboardButton("â™¾ QUITAR TODOS LOS LÃMITES", callback_data=f"orma_pub_sinlimites:{captura_id}"),
                ],
                [InlineKeyboardButton("â¬…ï¸ RETROCEDER", callback_data=f"orma_publicidad:{captura_id}")],
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
        await query.answer("LÃ­mites eliminados")
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
            return "âœ…" if bool(cfg[campo]) else "âŒ"

        await query.edit_message_text(
            "ðŸŽ› <b>TIPOS CONTROLADOS</b>\\n\\n"
            "âœ… = entra al control de cupos/separaciÃ³n\\n"
            "âŒ = queda libre para esta identidad\\n\\n"
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
                [InlineKeyboardButton("â¬…ï¸ RETROCEDER", callback_data=f"orma_publicidad:{captura_id}")],
            ]),
        )
        return

    if data.startswith("orma_pub_toggle:"):
        _, captura_txt, campo = data.split(":", 2)
        captura_id = int(captura_txt)
        captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            await query.answer("Ficha no disponible.", show_alert=True)
            return
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

        # Reabrir submenÃº tipos.
        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )

        def marca(c):
            return "âœ…" if bool(cfg[c]) else "âŒ"

        await query.edit_message_text(
            "ðŸŽ› <b>TIPOS CONTROLADOS</b>\\n\\n"
            "âœ… = entra al control de cupos/separaciÃ³n\\n"
            "âŒ = queda libre para esta identidad\\n\\n"
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
                [InlineKeyboardButton("â¬…ï¸ RETROCEDER", callback_data=f"orma_publicidad:{captura_id}")],
            ]),
        )
        return

    if data.startswith("orma_pub_reset:"):
        captura_id = int(data.split(":", 1)[1])
        captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            await query.answer("Ficha no disponible.", show_alert=True)
            return
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
            "separacion_minutos": "minutos de separaciÃ³n",
            "limite_hora": "mÃ¡ximo por hora",
            "limite_dia": "mÃ¡ximo por dÃ­a",
            "limite_semana": "mÃ¡ximo por semana",
            "limite_mes": "mÃ¡ximo por mes",
            "limite_anio": "mÃ¡ximo por aÃ±o",
        }.get(campo, "valor")

        await query.edit_message_text(
            "âœï¸ <b>VALOR PERSONALIZADO</b>\\n\\n"
            f"Escribe ahora el <b>{etiqueta}</b>.\\n\\n"
            "EnvÃ­a un nÃºmero entero igual o mayor que 0. "
            "Tu mensaje se eliminarÃ¡ automÃ¡ticamente.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "âŒ CANCELAR",
                        callback_data=f"orma_pub_cancelinput:{captura_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "â¬…ï¸ RETROCEDER",
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

        # La membresÃ­a 7/7 sigue siendo la primera puerta.
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

    # Los comandos operativos no forman parte de las mÃ©tricas de actividad.
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
            "ðŸ›¡ï¸ <b>MÃXIMO CONTROL GROUP</b>\n\n"
            "Centro privado de administraciÃ³n.\n\n"
            "ðŸ“Œ Responde cualquier mensaje en cualquiera de los grupos "
            "controlados con <code>/orma</code> para abrir su expediente.\n\n"
            "ðŸ§¹ Los comandos y datos operativos se eliminan "
            "automÃ¡ticamente para mantener el panel limpio."
        )
        teclado = InlineKeyboardMarkup([[
            InlineKeyboardButton("ðŸ—‘ CERRAR", callback_data="orma_cerrar")
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
        "âœ… MaximoControlGroup operativo\n"
        "ðŸ” MembresÃ­a: 7/7 activa\n"
        "ðŸŒ ModeraciÃ³n: 7 grupos oficiales + @Orma_Pruebas\n"
        "ðŸŒ Regla 7/7: usuarios, administradores y bots externos\n"        "âœ… Bots oficiales: excluidos de raÃ­z\n"        "ðŸš« Castigos/baneos: desactivados\n"
        "ðŸ›¡ï¸ Control publicitario general: pendiente"
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

    # ÃšNICA EXCEPCIÃ“N: bots oficiales definidos de raÃ­z.
    # Todo lo demÃ¡s (usuarios, administradores y bots externos) cumple 7/7.
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
        # tambiÃ©n aparece durante 2 minutos segÃºn la regla 7/7 definida.
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
    """Procesa m_<chat_id>_<message_id>_<user_id>, guarda el origen y borra el aviso."""
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

    chat_username = None
    chat_nombre = None

    if MAXIMO_APP_REF is not None:
        try:
            chat_origen = await MAXIMO_APP_REF.bot.get_chat(chat_id)
            chat_username = getattr(chat_origen, "username", None)
            chat_nombre = getattr(chat_origen, "title", None)
        except TelegramError:
            logging.info("No se pudo resolver el grupo origen chat_id=%s", chat_id)

    guardar_origen_union_db(
        usuario_id,
        chat_id,
        chat_username,
        chat_nombre,
    )

    if MAXIMO_APP_REF is None:
        return

    try:
        await MAXIMO_APP_REF.bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
    except TelegramError:
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

    # Cualquier texto/nÃºmero escrito para operar el panel desaparece.
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
                        "âŒ <b>VALOR NO VÃLIDO</b>\\n\\n"
                        "Escribe Ãºnicamente un nÃºmero entero igual o mayor que 0.\\n"
                        "El mensaje serÃ¡ eliminado automÃ¡ticamente."
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "âŒ CANCELAR",
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

    # Si el usuario llegÃ³ desde el botÃ³n del aviso del grupo,
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
    await update.effective_message.reply_text('DIAGNOSTICO VERSION f37437b')


async def union_verificar_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    usuario = update.effective_user

    if not query or not usuario:
        return

    await query.answer("Verificando los 7 gruposâ€¦")

    registrar_usuario_membresia(
        usuario,
        union_bot_iniciado=True,
    )

    estado = await obtener_estado_membresia_7de7(usuario.id)
    registrar_verificacion_membresia_db(usuario.id, estado)

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


async def union_mi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.effective_message
    usuario = update.effective_user
    if not mensaje or not usuario:
        return

    try:
        await mensaje.delete()
    except TelegramError:
        pass

    respuesta = await context.bot.send_message(
        chat_id=usuario.id,
        text=f"ðŸ†” Tu ID de Telegram es: <code>{usuario.id}</code>",
        parse_mode="HTML",
    )
    asyncio.create_task(eliminar_mensaje_despues(respuesta, 60))


async def mostrar_monitoreo_union(bot, admin_id):
    panel_id = obtener_panel_union_admin_db(admin_id)
    texto = texto_monitoreo_union()
    teclado = teclado_monitoreo_union()

    if panel_id:
        try:
            await bot.edit_message_text(
                chat_id=admin_id,
                message_id=panel_id,
                text=texto,
                parse_mode="HTML",
                reply_markup=teclado,
            )
            return True
        except TelegramError as error:
            if "message is not modified" in str(error).lower():
                return True

    enviado = await bot.send_message(
        chat_id=admin_id,
        text=texto,
        parse_mode="HTML",
        reply_markup=teclado,
    )
    guardar_panel_union_admin_db(admin_id, enviado.message_id)
    return True


async def union_monitoreo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.effective_message
    usuario = update.effective_user
    chat = update.effective_chat
    if not mensaje or not usuario or not chat or chat.type != ChatType.PRIVATE:
        return

    try:
        await mensaje.delete()
    except TelegramError:
        pass

    if ADMIN_USER_ID <= 0 or usuario.id != ADMIN_USER_ID:
        return

    await mostrar_monitoreo_union(context.bot, usuario.id)


async def union_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    usuario = update.effective_user
    if not query or not usuario:
        return


    if ADMIN_USER_ID <= 0 or usuario.id != ADMIN_USER_ID:
        await query.answer("Acceso no autorizado.", show_alert=True)
        return

    await query.answer()

    if query.data == "union_admin_cerrar":
        try:
            await query.message.delete()
        except TelegramError:
            pass
        eliminar_panel_union_admin_db(usuario.id)
        return

    await mostrar_monitoreo_union(context.bot, usuario.id)


# =========================================================
# ARRANQUE DE AMBOS BOTS
# =========================================================

async def iniciar_aplicacion(application: Application):
    await application.initialize()
    await application.start()

    if application.updater is None:
        raise RuntimeError("La aplicaciÃ³n no tiene Updater disponible.")

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
    union_app.add_handler(CommandHandler("mi_id", union_mi_id))
    union_app.add_handler(CommandHandler("monitoreo", union_monitoreo))
    union_app.add_handler(
        CallbackQueryHandler(
            union_verificar_callback,
            pattern=r"^union_verificar$",
        )
    )
    union_app.add_handler(
        CallbackQueryHandler(
            union_admin_callback,
            pattern=r"^union_admin_",
        )
    )

    await iniciar_aplicacion(maximo_app)
    await iniciar_aplicacion(union_app)

    logging.info("@MaximoControlGroup_bot iniciado.")
    logging.info("@UnionMembresia_bot iniciado.")
    logging.info("MembresÃ­a obligatoria configurada: 7/7.")
    logging.info("Regla 7/7 universal activa en los 7 grupos oficiales.")
    logging.info("Registro de actividad y movimientos del Bloque 3 activo.")
    logging.info("Control Publicitario Individual del Bloque 4 activo.")
    logging.info("Bots oficiales exentos de raÃ­z: %s", sorted(BOTS_OFICIALES_EXENTOS))
    logging.info("@%s permanece como laboratorio de pruebas.", GRUPO_PRUEBAS_USERNAME)

    try:
        await asyncio.Event().wait()
    finally:
        await detener_aplicacion(union_app)
        await detener_aplicacion(maximo_app)


if __name__ == "__main__":
    asyncio.run(main())

