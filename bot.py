import os
import asyncio
import logging
import sqlite3
import html
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from agente_respaldo_remoto import iniciar_agente_respaldo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
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


async def safe_edit_message_text(
    bot,
    *,
    chat_id,
    message_id,
    text,
    parse_mode=None,
    reply_markup=None,
    **kwargs,
):
    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            **kwargs,
        )
    except TelegramError as error:
        detalle = str(error).lower()
        if (
            "message is not modified" in detalle
            or "message to edit not found" in detalle
            or "message can't be edited" in detalle
            or "bad request" in detalle
        ):
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                **kwargs,
            )
        raise


async def safe_query_edit_message(
    query,
    text,
    parse_mode=None,
    reply_markup=None,
    **kwargs,
):
    try:
        return await query.edit_message_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            **kwargs,
        )
    except TelegramError as error:
        detalle = str(error).lower()
        if (
            "message is not modified" in detalle
            or "message to edit not found" in detalle
            or "message can't be edited" in detalle
            or "bad request" in detalle
        ):
            return await query.get_bot().send_message(
                chat_id=query.message.chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                **kwargs,
            )
        raise

BOT_TOKEN = os.environ["BOT_TOKEN"]
UNION_BOT_TOKEN = os.environ["UNION_BOT_TOKEN"]
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0") or 0)
GROUP_ANONYMOUS_BOT_ID = 1087968824
ORMA_ADMIN_USER_ID = int(os.environ.get("ORMA_ADMIN_USER_ID", "7615865943") or 0)

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
    "publicidadcontrolstreaming_bot",
    "groupanonymousbot",
}

MAXIMO_APP_REF = None
UNION_APP_REF = None

# Un solo aviso temporal por usuario y grupo.
# Clave: (chat_id, user_id) -> message_id
AVISOS_MEMBRESIA_ACTIVOS = {}

# Mantiene una referencia fuerte a la tarea de borrado de cada aviso.
# Sin esta referencia, una tarea creada con asyncio.create_task puede quedar
# sin dueño antes de cumplir los 60 segundos.
TAREAS_AVISOS_MEMBRESIA = {}

# Panel privado reutilizable para las capturas /orma.
PANELES_ORMA = {}
CAPTURAS_ORMA = {}

# Entradas temporales del panel de Control Publicitario.
# Clave: propietario_id -> {"captura_id": int, "campo": str}
ENTRADAS_CONTROL_PUBLICIDAD = {}

# Estados efímeros exclusivos de /orma CONTROL MÁXIMO.
# No alteran las reglas raíz de los 7 grupos.
SELECCIONES_MODERACION_ORMA = {}
ENTRADAS_ORMA_TOTAL = {}

# Un solo aviso publicitario temporal por identidad y grupo.
AVISOS_PUBLICIDAD_ACTIVOS = {}

APP_VERSION = "1.0.3"
APP_VERSION_TITULO = "CICLO 24H Y RELOJ BLINDADO"
AVISO_PUBLICIDAD_SEGUNDOS = 30

MAXIMO_BOT_USERNAME = "MaximoControlGroup_bot"
MEMBRESIA_PUBLICIDAD_BOT_USERNAME = "MembresiaConsultasDenuncias_bot"
MEMBRESIA_PUBLICIDAD_URL = (
    f"https://t.me/{MEMBRESIA_PUBLICIDAD_BOT_USERNAME}?start=publicidad"
)

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



def sello_version_panel():
    return (
        f"🏷 <b>v{APP_VERSION}</b> · "
        f"{APP_VERSION_TITULO}"
    )


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
                ancla_limite_dia TEXT,
                fecha_actualizacion TEXT NOT NULL,
                PRIMARY KEY (identidad_tipo, identidad_id)
            )
            """
        )

        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS control_publicidad_grupos (
                identidad_tipo TEXT NOT NULL,
                identidad_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                chat_username TEXT,
                chat_nombre TEXT,
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
                ancla_limite_dia TEXT,
                fecha_actualizacion TEXT NOT NULL,
                PRIMARY KEY (identidad_tipo, identidad_id, chat_id)
            )
            """
        )

        # v1.0.3: ancla independiente para ciclos diarios móviles de 24 horas.
        for tabla in ("control_publicidad_identidades", "control_publicidad_grupos"):
            if not columna_existe(conexion, tabla, "ancla_limite_dia"):
                conexion.execute(
                    f"ALTER TABLE {tabla} ADD COLUMN ancla_limite_dia TEXT"
                )
            # Compatibilidad con reglas existentes: el primer ancla se toma de la
            # última actualización conocida únicamente una vez.
            conexion.execute(
                f"""
                UPDATE {tabla}
                SET ancla_limite_dia = fecha_actualizacion
                WHERE limite_dia IS NOT NULL
                  AND ancla_limite_dia IS NULL
                """
            )

        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS auditoria_orma_acciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                propietario_id INTEGER NOT NULL,
                captura_id INTEGER,
                objetivo_tipo TEXT NOT NULL,
                objetivo_id INTEGER NOT NULL,
                accion TEXT NOT NULL,
                chat_id INTEGER,
                chat_username TEXT,
                chat_nombre TEXT,
                detalle TEXT,
                resultado TEXT NOT NULL,
                error TEXT,
                fecha_evento TEXT NOT NULL
            )
            """
        )

        conexion.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orma_auditoria_objetivo_fecha
            ON auditoria_orma_acciones (
                objetivo_tipo,
                objetivo_id,
                fecha_evento
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
            CREATE TABLE IF NOT EXISTS clientes_editados (
                identidad_tipo TEXT NOT NULL,
                identidad_id INTEGER NOT NULL,
                username TEXT,
                nombre TEXT,
                es_bot INTEGER NOT NULL DEFAULT 0,
                ultima_captura_id INTEGER,
                ultima_accion TEXT,
                fecha_primera_edicion TEXT NOT NULL,
                fecha_ultima_edicion TEXT NOT NULL,
                PRIMARY KEY (identidad_tipo, identidad_id)
            )
            """
        )

        conexion.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_clientes_editados_fecha
            ON clientes_editados (fecha_ultima_edicion DESC)
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
        "📊 <b>MONITOREO DE MEMBRESÍA</b>",
        "",
        f"👥 Usuarios registrados: <b>{int(t['total'] or 0)}</b>",
        f"⏳ Iniciaron sin verificar: <b>{int(t['sin_verificar'] or 0)}</b>",
        f"🔴 Estado 0/7: <b>{int(t['cero'] or 0)}</b>",
        f"🟡 Estado 1–6/7: <b>{int(t['proceso'] or 0)}</b>",
        f"✅ Estado 7/7: <b>{int(t['completos'] or 0)}</b>",
        f"↩️ Perdieron grupos después: <b>{int(t['perdieron'] or 0)}</b>",
        "",
        "📍 <b>ORIGEN DE LOS ACCESOS</b>",
    ]

    if resumen["origenes"]:
        for fila in resumen["origenes"]:
            lineas.append(
                f"• {html.escape(str(fila['origen']))}: <b>{int(fila['total'])}</b>"
            )
    else:
        lineas.append("• Todavía sin registros")

    lineas.extend(["", "🕐 <b>ACCESOS RECIENTES</b>"])

    if resumen["recientes"]:
        for fila in resumen["recientes"]:
            identidad = fila["username"] or fila["nombre"] or str(fila["user_id"])
            lineas.append(
                f"• {html.escape(str(identidad))} · "
                f"<b>{int(fila['grupos_actuales'] or 0)}/7</b> · "
                f"{formatear_fecha_peru(fila['fecha_ultimo_acceso'])}"
            )
    else:
        lineas.append("• Todavía sin registros")

    lineas.extend(["", "Actualizado: " + formatear_fecha_peru(datetime.now(timezone.utc).isoformat())])
    return "\n".join(lineas)


def teclado_monitoreo_union():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 ACTUALIZAR", callback_data="union_admin_actualizar")],
        [InlineKeyboardButton("🗑 CERRAR", callback_data="union_admin_cerrar")],
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
            await safe_edit_message_text(UNION_APP_REF.bot,
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
    if user is None or not getattr(user, "is_bot", False):
        return False

    if int(getattr(user, "id", 0) or 0) == GROUP_ANONYMOUS_BOT_ID:
        return True

    return username_usuario(user) in BOTS_OFICIALES_EXENTOS


def es_administrador_maximo(user):
    """Autoriza únicamente identidades administrativas para paneles internos."""
    if user is None:
        return False

    user_id = int(getattr(user, "id", 0) or 0)

    # Telegram representa al administrador anónimo mediante GroupAnonymousBot.
    if user_id == GROUP_ANONYMOUS_BOT_ID:
        return True

    permitidos = {
        int(valor)
        for valor in (ADMIN_USER_ID, ORMA_ADMIN_USER_ID)
        if int(valor or 0) > 0
    }
    return user_id in permitidos


def comando_dirigido_a_maximo(mensaje):
    """Indica si un comando de grupo pertenece a Máximo Control Group."""
    texto = str(getattr(mensaje, "text", "") or "").strip()
    if not texto.startswith("/"):
        return False

    comando = texto.split(maxsplit=1)[0].lower()

    if "@" in comando:
        _, destino = comando.split("@", 1)
        return destino == MAXIMO_BOT_USERNAME.lower()

    # Comandos propios conocidos sin @.
    return comando in {"/orma", "/start", "/estado"}


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
    clave = (chat_id, user_id)
    tarea_actual = asyncio.current_task()

    try:
        await asyncio.sleep(segundos)

        # Solo borra el aviso que siga siendo el vigente para ese usuario/grupo.
        if AVISOS_MEMBRESIA_ACTIVOS.get(clave) != message_id:
            return

        try:
            await bot.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )
        except TelegramError:
            logging.exception(
                "No se pudo eliminar aviso de membresía chat=%s user=%s message=%s",
                chat_id,
                user_id,
                message_id,
            )
        finally:
            if AVISOS_MEMBRESIA_ACTIVOS.get(clave) == message_id:
                AVISOS_MEMBRESIA_ACTIVOS.pop(clave, None)

    except asyncio.CancelledError:
        raise
    finally:
        if TAREAS_AVISOS_MEMBRESIA.get(clave) is tarea_actual:
            TAREAS_AVISOS_MEMBRESIA.pop(clave, None)


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
    tarea_anterior = TAREAS_AVISOS_MEMBRESIA.pop(clave, None)
    if tarea_anterior and not tarea_anterior.done():
        tarea_anterior.cancel()

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

    tarea_borrado = asyncio.create_task(
        eliminar_aviso_membresia_programado(
            context.bot,
            chat_id,
            user_id,
            aviso.message_id,
            AVISO_MEMBRESIA_SEGUNDOS,
        )
    )
    TAREAS_AVISOS_MEMBRESIA[clave] = tarea_borrado



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
    ahora_actualizacion = datetime.now(timezone.utc).isoformat()
    if "limite_dia" in datos:
        datos["ancla_limite_dia"] = (
            ahora_actualizacion if datos["limite_dia"] is not None else None
        )
    datos["fecha_actualizacion"] = ahora_actualizacion

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
        actualizado = cursor.rowcount > 0

    if actualizado:
        registrar_cliente_editado_db(
            identidad_tipo,
            identidad_id,
            accion="PUBLICIDAD_GLOBAL",
        )
    return actualizado


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
    registrar_cliente_editado_db(
        identidad_tipo,
        identidad_id,
        accion="PUBLICIDAD_GLOBAL_RESETEADA",
    )


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



def ciclo_diario_24h_config(cfg, ahora=None):
    """Devuelve (inicio, siguiente) del ciclo móvil de 24h de la regla efectiva."""
    ahora = ahora or datetime.now(timezone.utc)
    ancla_txt = None
    try:
        ancla_txt = cfg["ancla_limite_dia"]
    except (KeyError, IndexError, TypeError):
        pass

    if not ancla_txt:
        try:
            ancla_txt = cfg["fecha_actualizacion"]
        except (KeyError, IndexError, TypeError):
            return None, None

    try:
        ancla = datetime.fromisoformat(str(ancla_txt))
        if ancla.tzinfo is None:
            ancla = ancla.replace(tzinfo=timezone.utc)
        ancla = ancla.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None, None

    if ahora < ancla:
        return ancla, ancla + timedelta(hours=24)

    ciclo = timedelta(hours=24)
    transcurrido = (ahora - ancla).total_seconds()
    ciclos_completos = int(transcurrido // ciclo.total_seconds())
    inicio = ancla + (ciclo * ciclos_completos)
    siguiente = inicio + ciclo
    return inicio, siguiente

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
    """Resumen histórico global de la identidad, solo para estadísticas."""
    limites = limites_periodos_publicidad()
    return {
        clave: contar_publicidad_permitida_db(
            identidad_tipo,
            identidad_id,
            inicio,
        )
        for clave, inicio in limites.items()
    }


def resumen_uso_publicidad_grupo_db(
    identidad_tipo,
    identidad_id,
    chat_id,
):
    """
    Consumo efectivo del limitador para un grupo concreto.

    Regla oficial v1.0.0:
        identidad + grupo + periodo

    Un límite de X publicaciones permite X publicaciones en CADA grupo
    controlado de forma independiente.
    """
    limites = limites_periodos_publicidad()
    return {
        clave: contar_publicidad_permitida_grupo_db(
            identidad_tipo,
            identidad_id,
            chat_id,
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
    Analiza intentos/publicaciones que el motor clasificó como publicidad.
    Incluye permitidas y rechazadas para reflejar el comportamiento real
    de la identidad, no solamente lo que finalmente quedó visible.
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

    # Promedio equivalente de publicaciones por hora en las últimas 24 h.
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
        frecuencia = "Aún sin muestra suficiente"
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


def evaluar_control_publicidad(
    identidad_tipo,
    identidad_id,
    tipo_contenido,
    chat=None,
):
    if chat is not None:
        cfg, alcance = control_efectivo_para_chat(
            identidad_tipo,
            identidad_id,
            chat,
        )
    else:
        cfg = obtener_control_identidad_db(identidad_tipo, identidad_id)
        alcance = "GLOBAL"

    modo = str(cfg["modo"] or "HEREDADO").upper()

    if modo == "EXCLUIDO":
        return True, f"{alcance} · EXCLUIDO DEL CONTROL", cfg, None

    if modo == "ILIMITADO":
        return True, f"{alcance} · PUBLICIDAD ILIMITADA", cfg, None

    if modo == "BLOQUEADO":
        return False, f"{alcance} · PUBLICIDAD BLOQUEADA", cfg, None

    if not tipo_habilitado_por_config(tipo_contenido, cfg):
        return True, f"{alcance} · TIPO {tipo_contenido} EXCLUIDO", cfg, None

    if modo == "HEREDADO":
        return True, "HEREDADO · SIN REGLA GLOBAL ACTIVA TODAVÍA", cfg, None

    ahora = datetime.now(timezone.utc)

    separacion = cfg["separacion_segundos"]
    if separacion is not None and int(separacion) > 0:
        # La regla de frecuencia se consume por grupo, aunque la
        # configuración aplicada provenga del perfil GLOBAL.
        if chat is not None:
            ultima = ultima_publicidad_permitida_grupo_db(
                identidad_tipo,
                identidad_id,
                chat.id,
            )
        else:
            ultima = ultima_publicidad_permitida_db(
                identidad_tipo,
                identidad_id,
            )

        if ultima:
            try:
                fecha_ultima = datetime.fromisoformat(ultima)
                if fecha_ultima.tzinfo is None:
                    fecha_ultima = fecha_ultima.replace(tzinfo=timezone.utc)
                disponible = fecha_ultima + timedelta(seconds=int(separacion))
                if ahora < disponible:
                    return (
                        False,
                        f"{alcance} · SEPARACIÓN MÍNIMA NO CUMPLIDA",
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

        # Regla oficial: el cupo se consume por identidad + grupo + periodo.
        # Incluso una regla GLOBAL significa "mismo límite en cada grupo",
        # nunca "un único contador compartido entre todos los grupos".
        desde_periodo = limites[periodo]
        if periodo == "dia":
            inicio_diario, _ = ciclo_diario_24h_config(cfg, ahora)
            if inicio_diario is not None:
                desde_periodo = inicio_diario.isoformat()

        if chat is not None:
            usados = contar_publicidad_permitida_grupo_db(
                identidad_tipo,
                identidad_id,
                chat.id,
                desde_periodo,
            )
        else:
            usados = contar_publicidad_permitida_db(
                identidad_tipo,
                identidad_id,
                desde_periodo,
            )

        if usados >= int(limite):
            return False, f"{alcance} · {etiqueta}", cfg, None

    return True, f"{alcance} · DENTRO DE LOS LÍMITES", cfg, None

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


def proxima_disponibilidad_separacion(
    identidad_tipo,
    identidad_id,
    cfg,
    chat_id=None,
):
    separacion = cfg["separacion_segundos"]
    if separacion is None or int(separacion) <= 0:
        return None

    if chat_id is not None:
        ultima = ultima_publicidad_permitida_grupo_db(
            identidad_tipo,
            identidad_id,
            int(chat_id),
        )
    else:
        ultima = ultima_publicidad_permitida_db(
            identidad_tipo,
            identidad_id,
        )
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
                "🎯 CONTROL INDIVIDUAL POR GRUPO",
                callback_data=f"orma_pg_lista:{captura_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "♻️ RESTAURAR HEREDADO GLOBAL",
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
    uso = resumen_uso_publicidad_grupo_db(
        captura["objetivo_tipo"],
        captura["objetivo_id"],
        captura["chat_id"],
    )
    ritmo = resumen_frecuencia_publicidad_db(
        captura["objetivo_tipo"],
        captura["objetivo_id"],
    )
    activos, libres = resumen_tipos_controlados(cfg)
    disponible = proxima_disponibilidad_separacion(
        captura["objetivo_tipo"],
        captura["objetivo_id"],
        cfg,
        captura["chat_id"],
    )
    por_grupos = resumen_por_grupos_orma(
        captura["objetivo_tipo"],
        captura["objetivo_id"],
    )

    modo = str(cfg["modo"] or "HEREDADO").upper()
    if modo == "HEREDADO":
        efecto = "Usará la regla general cuando el módulo global esté activo"
    elif modo == "PERSONALIZADO":
        efecto = "Aplica separación, límites y tipos propios"
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

    lineas = [
        "📣 <b>PUBLICIDAD POR GRUPO · /ORMA</b>",
        "",
        f"👤 <b>{html.escape(str(captura['objetivo_nombre'] or 'Sin nombre'))}</b>",
        f"🆔 <code>{captura['objetivo_id']}</code>",
        "",
    ]

    for grupo in por_grupos:
        frecuencia = (
            "Sin muestra"
            if grupo["pub_promedio_intervalo"] is None
            else f"1 cada {formatear_intervalo_segundos(grupo['pub_promedio_intervalo'])}"
        )
        lineas.extend([
            f"<b>{grupo['indice']}. {nombre_grupo_orma(grupo)}</b>",
            (
                f"• H {grupo['pub_hora']} · 24h {grupo['pub_24h']} · "
                f"D {grupo['pub_dia']} · S {grupo['pub_semana']} · "
                f"M {grupo['pub_mes']} · T <b>{grupo['pub_total']}</b>"
            ),
            (
                f"• ✅ {grupo['pub_permitidas']} · "
                f"⛔ {grupo['pub_bloqueadas']} · "
                f"Ritmo: <b>{frecuencia}</b>"
            ),
            f"• Última: <b>{formatear_fecha_peru(grupo['ultima_publicidad'])}</b>",
            "",
        ])

    lineas.extend([
        "⚙️ <b>CONTROL INDIVIDUAL</b>",
        f"• Modo: <b>{modo}</b>",
        f"• Efecto: <b>{efecto}</b>",
        f"• Separación: <b>{texto_separacion(cfg['separacion_segundos'])}</b>",
        f"• Próxima por separación: <b>{proxima}</b>",
        "",
        "🔢 <b>LÍMITES / USO EN ESTE GRUPO</b>",
        f"• Hora: <b>{texto_valor_limite(cfg['limite_hora'])}</b> · usados {uso['hora']}",
        f"• Día: <b>{texto_valor_limite(cfg['limite_dia'])}</b> · usados {uso['dia']}",
        f"• Semana: <b>{texto_valor_limite(cfg['limite_semana'])}</b> · usados {uso['semana']}",
        f"• Mes: <b>{texto_valor_limite(cfg['limite_mes'])}</b> · usados {uso['mes']}",
        f"• Año: <b>{texto_valor_limite(cfg['limite_anio'])}</b> · usados {uso['anio']}",
        "• Regla: <b>cada grupo lleva su propio contador independiente</b>",
        "",
        "🎛 <b>TIPOS</b>",
        f"• Controlados: <b>{', '.join(activos) if activos else 'NINGUNO'}</b>",
        f"• Libres: <b>{', '.join(libres) if libres else 'NINGUNO'}</b>",
        "• Texto normal puro: <b>SIEMPRE LIBRE</b>",
        "",
        "Selecciona qué deseas modificar.",
    ])

    return "\n".join(lineas)

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
    """
    Aviso comercial temporal cuando una publicación excede el control permitido.

    No expone IDs, reglas internas, límites, motivos técnicos ni datos de moderación.
    El mensaje permanece como máximo AVISO_PUBLICIDAD_SEGUNDOS (30 s).
    """
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

    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📣 MEMBRESÍA PUBLICITARIA",
                url=MEMBRESIA_PUBLICIDAD_URL,
            )
        ]
    ])

    aviso = await context.bot.send_message(
        chat_id=chat.id,
        text=(
            "📣 <b>¿DESEAS PUBLICAR CON MAYOR FRECUENCIA?</b>\n\n"
            "Has alcanzado el límite de publicidad disponible para este momento.\n\n"
            "Si deseas ampliar tu frecuencia o tiempo de publicación, "
            "puedes revisar nuestros planes de <b>Membresía Publicitaria</b>.\n\n"
            "✨ Consulta las opciones disponibles desde el botón inferior.\n"
            "⏳ <i>Este aviso se eliminará automáticamente en 30 segundos.</i>"
        ),
        parse_mode="HTML",
        reply_markup=teclado,
        disable_web_page_preview=True,
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
                SUM(CASE WHEN tipo_movimiento = 'SALIDA' THEN 1 ELSE 0 END) AS salidas,
                MIN(CASE WHEN tipo_movimiento = 'ENTRADA' THEN fecha_evento END) AS primera_entrada,
                MAX(CASE WHEN tipo_movimiento = 'ENTRADA' THEN fecha_evento END) AS ultima_entrada,
                MAX(CASE WHEN tipo_movimiento = 'SALIDA' THEN fecha_evento END) AS ultima_salida
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



def resumen_por_grupos_orma(objetivo_tipo, objetivo_id):
    """
    Panorama de los 7 grupos oficiales para /orma.

    Está diseñado para ser rápido: usa consultas agrupadas sobre la base local
    y NO hace llamadas adicionales a Telegram. La membresía en tiempo real se
    resuelve una sola vez en construir_texto_ficha_orma().
    """
    grupos = obtener_grupos_obligatorios_db()
    limites = limites_periodos_actividad()
    ahora = datetime.now(timezone.utc)
    desde_24h = (ahora - timedelta(hours=24)).isoformat()

    with conectar_db() as conexion:
        actividad = conexion.execute(
            """
            SELECT
                LOWER(COALESCE(chat_username, '')) AS clave,
                SUM(CASE WHEN fecha_evento >= ? THEN 1 ELSE 0 END) AS hora,
                SUM(CASE WHEN fecha_evento >= ? THEN 1 ELSE 0 END) AS dia,
                SUM(CASE WHEN fecha_evento >= ? THEN 1 ELSE 0 END) AS semana,
                SUM(CASE WHEN fecha_evento >= ? THEN 1 ELSE 0 END) AS mes,
                COUNT(*) AS total,
                MAX(fecha_evento) AS ultima
            FROM actividad_grupo
            WHERE identidad_tipo = ?
              AND identidad_id = ?
            GROUP BY LOWER(COALESCE(chat_username, ''))
            """,
            (
                limites["hora"],
                limites["dia"],
                limites["semana"],
                limites["mes"],
                objetivo_tipo,
                objetivo_id,
            ),
        ).fetchall()

        publicidad = conexion.execute(
            """
            SELECT
                LOWER(COALESCE(chat_username, '')) AS clave,
                SUM(CASE WHEN fecha_evento >= ? THEN 1 ELSE 0 END) AS hora,
                SUM(CASE WHEN fecha_evento >= ? THEN 1 ELSE 0 END) AS h24,
                SUM(CASE WHEN fecha_evento >= ? THEN 1 ELSE 0 END) AS dia,
                SUM(CASE WHEN fecha_evento >= ? THEN 1 ELSE 0 END) AS semana,
                SUM(CASE WHEN fecha_evento >= ? THEN 1 ELSE 0 END) AS mes,
                COUNT(*) AS total,
                SUM(CASE WHEN decision = 'PERMITIDA' THEN 1 ELSE 0 END) AS permitidas,
                SUM(CASE WHEN decision <> 'PERMITIDA' THEN 1 ELSE 0 END) AS bloqueadas,
                MAX(fecha_evento) AS ultima
            FROM eventos_publicidad_control
            WHERE identidad_tipo = ?
              AND identidad_id = ?
            GROUP BY LOWER(COALESCE(chat_username, ''))
            """,
            (
                limites["hora"],
                desde_24h,
                limites["dia"],
                limites["semana"],
                limites["mes"],
                objetivo_tipo,
                objetivo_id,
            ),
        ).fetchall()

        movimientos = []
        if objetivo_tipo in {"USUARIO", "BOT"}:
            movimientos = conexion.execute(
                """
                SELECT
                    LOWER(COALESCE(chat_username, '')) AS clave,
                    SUM(CASE WHEN tipo_movimiento = 'ENTRADA' THEN 1 ELSE 0 END) AS entradas,
                    SUM(CASE WHEN tipo_movimiento = 'SALIDA' THEN 1 ELSE 0 END) AS salidas,
                    MIN(CASE WHEN tipo_movimiento = 'ENTRADA' THEN fecha_evento END) AS primera_entrada,
                    MAX(CASE WHEN tipo_movimiento = 'ENTRADA' THEN fecha_evento END) AS ultima_entrada,
                    MAX(CASE WHEN tipo_movimiento = 'SALIDA' THEN fecha_evento END) AS ultima_salida
                FROM movimientos_grupo
                WHERE user_id = ?
                GROUP BY LOWER(COALESCE(chat_username, ''))
                """,
                (objetivo_id,),
            ).fetchall()

        # Una sola lectura de eventos recientes para calcular ritmo por grupo.
        eventos_ritmo = conexion.execute(
            """
            SELECT
                LOWER(COALESCE(chat_username, '')) AS clave,
                fecha_evento
            FROM eventos_publicidad_control
            WHERE identidad_tipo = ?
              AND identidad_id = ?
            ORDER BY fecha_evento DESC
            LIMIT 300
            """,
            (objetivo_tipo, objetivo_id),
        ).fetchall()

    act = {str(f["clave"] or "").lower(): f for f in actividad}
    pub = {str(f["clave"] or "").lower(): f for f in publicidad}
    mov = {str(f["clave"] or "").lower(): f for f in movimientos}

    fechas_por_grupo = {}
    for fila in eventos_ritmo:
        clave = str(fila["clave"] or "").lower()
        if not clave:
            continue
        lista = fechas_por_grupo.setdefault(clave, [])
        if len(lista) >= 20:
            continue
        try:
            fecha = datetime.fromisoformat(str(fila["fecha_evento"]))
            if fecha.tzinfo is None:
                fecha = fecha.replace(tzinfo=timezone.utc)
            lista.append(fecha.astimezone(timezone.utc))
        except (TypeError, ValueError):
            continue

    resultado = []
    for indice, grupo in enumerate(grupos, start=1):
        clave = str(grupo["username"] or "").lower()
        a = act.get(clave)
        p = pub.get(clave)
        m = mov.get(clave)

        recientes = list(reversed(fechas_por_grupo.get(clave, [])))
        intervalos = [
            (actual - anterior).total_seconds()
            for anterior, actual in zip(recientes, recientes[1:])
            if (actual - anterior).total_seconds() >= 0
        ]
        promedio_intervalo = (
            sum(intervalos) / len(intervalos)
            if intervalos
            else None
        )

        resultado.append({
            "indice": indice,
            "username": grupo["username"],
            "nombre": grupo["nombre"] or grupo["username"],
            "actividad_hora": int(a["hora"] or 0) if a else 0,
            "actividad_dia": int(a["dia"] or 0) if a else 0,
            "actividad_semana": int(a["semana"] or 0) if a else 0,
            "actividad_mes": int(a["mes"] or 0) if a else 0,
            "actividad_total": int(a["total"] or 0) if a else 0,
            "ultima_actividad": a["ultima"] if a else None,
            "pub_hora": int(p["hora"] or 0) if p else 0,
            "pub_24h": int(p["h24"] or 0) if p else 0,
            "pub_dia": int(p["dia"] or 0) if p else 0,
            "pub_semana": int(p["semana"] or 0) if p else 0,
            "pub_mes": int(p["mes"] or 0) if p else 0,
            "pub_total": int(p["total"] or 0) if p else 0,
            "pub_permitidas": int(p["permitidas"] or 0) if p else 0,
            "pub_bloqueadas": int(p["bloqueadas"] or 0) if p else 0,
            "ultima_publicidad": p["ultima"] if p else None,
            "pub_promedio_intervalo": promedio_intervalo,
            "entradas": int(m["entradas"] or 0) if m else 0,
            "salidas": int(m["salidas"] or 0) if m else 0,
            "primera_entrada": m["primera_entrada"] if m else None,
            "ultima_entrada": m["ultima_entrada"] if m else None,
            "ultima_salida": m["ultima_salida"] if m else None,
        })

    return resultado


def estado_membresia_por_username(estado):
    resultado = {}
    if not estado:
        return resultado

    for grupo in estado.get("completados", []):
        resultado[str(grupo["username"] or "").lower()] = "✅"

    errores = {
        str(grupo["username"] or "").lower()
        for grupo, _ in estado.get("errores", [])
    }

    for grupo in estado.get("faltantes", []):
        clave = str(grupo["username"] or "").lower()
        resultado[clave] = "⚠️" if clave in errores else "❌"

    return resultado


def nombre_grupo_orma(grupo):
    nombre = str(grupo.get("nombre") or grupo.get("username") or "Grupo")
    return html.escape(nombre)



def grupo_orma_por_indice(indice):
    try:
        indice = int(indice)
    except (TypeError, ValueError):
        return None
    for orden, nombre, username, helpdesk in GRUPOS_OFICIALES:
        if int(orden) == indice:
            return {
                "indice": int(orden),
                "nombre": nombre,
                "username": username,
                "helpdesk": helpdesk,
                "chat_ref": f"@{username}",
            }
    return None


def indice_grupo_orma_por_username(username):
    clave = str(username or "").lstrip("@").lower()
    for orden, _, grupo_username, _ in GRUPOS_OFICIALES:
        if grupo_username.lower() == clave:
            return int(orden)
    return None


def cabecera_identidad_orma(captura, *, rol=None, habilitado=None):
    username = (
        f"@{captura['objetivo_username']}"
        if captura["objetivo_username"]
        else "Sin @username"
    )
    nombre = captura["objetivo_nombre"] or "Sin nombre visible"
    tipo = str(captura["objetivo_tipo"] or "DESCONOCIDO").upper()
    es_bot = "SÍ" if bool(captura["objetivo_es_bot"]) else "NO"

    if rol is None:
        rol = "Consultar ficha"
    rol_txt = str(rol)
    administrador = (
        "SÍ"
        if rol_txt.lower() in {"administrador", "propietario"}
        else "NO"
    )

    if habilitado is None:
        habilitado_txt = "Verificando / no aplica"
    else:
        habilitado_txt = "SÍ" if bool(habilitado) else "NO"

    return "\n".join([
        "👤 <b>IDENTIDAD CONTROLADA</b>",
        f"• ID: <code>{captura['objetivo_id']}</code>",
        f"• Usuario: <b>{html.escape(str(nombre))}</b>",
        f"• UserName: <b>{html.escape(str(username))}</b>",
        f"• Tipo: <b>{html.escape(tipo)}</b>",
        f"• Bot: <b>{es_bot}</b>",
        f"• Rol origen: <b>{html.escape(rol_txt)}</b>",
        f"• Administrador: <b>{administrador}</b>",
        f"• Habilitado 7/7: <b>{habilitado_txt}</b>",
    ])


async def estado_7grupos_orma_concurrente(objetivo_id):
    """
    Consulta los 7 grupos EN PARALELO para que /orma siga siendo rápido.
    No reemplaza ni modifica la regla raíz 7/7.
    """
    if MAXIMO_APP_REF is None:
        return []

    async def consultar(grupo):
        try:
            miembro = await MAXIMO_APP_REF.bot.get_chat_member(
                chat_id=grupo["chat_ref"],
                user_id=int(objetivo_id),
            )
            rol = etiqueta_rol_chat_member(miembro)
            return {
                **grupo,
                "miembro": bool(estado_es_miembro(miembro)),
                "rol": rol,
                "error": None,
            }
        except TelegramError as error:
            return {
                **grupo,
                "miembro": False,
                "rol": "No disponible",
                "error": str(error),
            }

    grupos = [
        grupo_orma_por_indice(indice)
        for indice in range(1, TOTAL_GRUPOS_OBLIGATORIOS + 1)
    ]
    return await asyncio.gather(*(consultar(g) for g in grupos if g))



def registrar_cliente_editado_db(
    identidad_tipo,
    identidad_id,
    *,
    accion="EDICION",
):
    """Registra persistentemente usuarios/bots que ya recibieron una edición."""
    tipo = str(identidad_tipo or "").upper()
    if tipo not in {"USUARIO", "BOT"}:
        return False

    ahora = datetime.now(timezone.utc).isoformat()

    with conectar_db() as conexion:
        captura = conexion.execute(
            """
            SELECT id, objetivo_username, objetivo_nombre, objetivo_es_bot
            FROM capturas_orma
            WHERE objetivo_tipo = ?
              AND objetivo_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (tipo, int(identidad_id)),
        ).fetchone()

        if not captura:
            return False

        conexion.execute(
            """
            INSERT INTO clientes_editados (
                identidad_tipo, identidad_id,
                username, nombre, es_bot,
                ultima_captura_id, ultima_accion,
                fecha_primera_edicion, fecha_ultima_edicion
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identidad_tipo, identidad_id) DO UPDATE SET
                username = excluded.username,
                nombre = excluded.nombre,
                es_bot = excluded.es_bot,
                ultima_captura_id = excluded.ultima_captura_id,
                ultima_accion = excluded.ultima_accion,
                fecha_ultima_edicion = excluded.fecha_ultima_edicion
            """,
            (
                tipo,
                int(identidad_id),
                captura["objetivo_username"],
                captura["objetivo_nombre"],
                int(captura["objetivo_es_bot"] or 0),
                int(captura["id"]),
                str(accion or "EDICION"),
                ahora,
                ahora,
            ),
        )
        conexion.commit()

    return True


def contar_clientes_editados_db():
    with conectar_db() as conexion:
        fila = conexion.execute(
            "SELECT COUNT(*) AS total FROM clientes_editados"
        ).fetchone()
    return int(fila["total"] if fila else 0)


def obtener_clientes_editados_db(*, limite=10, offset=0):
    with conectar_db() as conexion:
        return conexion.execute(
            """
            SELECT *
            FROM clientes_editados
            ORDER BY fecha_ultima_edicion DESC, identidad_id DESC
            LIMIT ? OFFSET ?
            """,
            (int(limite), int(offset)),
        ).fetchall()


def obtener_cliente_editado_db(identidad_tipo, identidad_id):
    with conectar_db() as conexion:
        return conexion.execute(
            """
            SELECT *
            FROM clientes_editados
            WHERE identidad_tipo = ?
              AND identidad_id = ?
            LIMIT 1
            """,
            (str(identidad_tipo).upper(), int(identidad_id)),
        ).fetchone()


def crear_captura_desde_cliente_editado_db(
    propietario_id,
    identidad_tipo,
    identidad_id,
):
    tipo = str(identidad_tipo).upper()

    with conectar_db() as conexion:
        origen = conexion.execute(
            """
            SELECT *
            FROM capturas_orma
            WHERE objetivo_tipo = ?
              AND objetivo_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (tipo, int(identidad_id)),
        ).fetchone()

        if not origen:
            return None

        cursor = conexion.execute(
            """
            INSERT INTO capturas_orma (
                propietario_id, objetivo_tipo, objetivo_id,
                objetivo_username, objetivo_nombre, objetivo_es_bot,
                chat_id, chat_username, chat_nombre,
                mensaje_origen_id, fecha_captura
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(propietario_id),
                tipo,
                int(identidad_id),
                origen["objetivo_username"],
                origen["objetivo_nombre"],
                int(origen["objetivo_es_bot"] or 0),
                origen["chat_id"],
                origen["chat_username"],
                origen["chat_nombre"],
                origen["mensaje_origen_id"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        captura_id = int(cursor.lastrowid)

        conexion.execute(
            """
            UPDATE clientes_editados
            SET ultima_captura_id = ?
            WHERE identidad_tipo = ?
              AND identidad_id = ?
            """,
            (captura_id, tipo, int(identidad_id)),
        )
        conexion.commit()

    return captura_id


def texto_clientes_editados_db(pagina=0, por_pagina=10):
    total = contar_clientes_editados_db()
    pagina = max(0, int(pagina))
    inicio = pagina * int(por_pagina)
    fin = min(total, inicio + int(por_pagina))

    return (
        "👥 <b>CLIENTES EDITADOS</b>\n"
        + sello_version_panel()
        + "\n\n"
        "Usuarios que ya recibieron una edición administrativa quedan "
        "guardados aquí de forma permanente.\n\n"
        "Selecciona uno para volver a abrir su ficha y desbloquearlo, "
        "modificar sus publicaciones, límites, separación, tipos controlados, "
        "configuración por grupo o cualquier otra opción disponible.\n\n"
        f"📦 Guardados: <b>{total}</b>\n"
        f"📄 Mostrando: <b>{inicio + 1 if total else 0}-{fin}</b>"
    )


def teclado_clientes_editados_db(pagina=0, por_pagina=10):
    total = contar_clientes_editados_db()
    pagina = max(0, int(pagina))
    offset = pagina * int(por_pagina)
    clientes = obtener_clientes_editados_db(limite=por_pagina, offset=offset)

    filas = []
    for cliente in clientes:
        username = (
            "@" + str(cliente["username"]).lstrip("@")
            if cliente["username"]
            else ""
        )
        nombre = str(cliente["nombre"] or "").strip()
        visible = username or nombre or str(cliente["identidad_id"])

        try:
            cfg = obtener_control_identidad_db(
                cliente["identidad_tipo"],
                cliente["identidad_id"],
            )
            modo = str(cfg["modo"] or "HEREDADO").upper()
        except Exception:
            modo = "SIN DATOS"

        filas.append([
            InlineKeyboardButton(
                f"👤 {visible} · {modo}",
                callback_data=(
                    "orma_cliente_editado:"
                    f"{cliente['identidad_tipo']}:"
                    f"{cliente['identidad_id']}"
                ),
            )
        ])

    nav = []
    if pagina > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ ANTERIOR",
                callback_data=f"orma_clientes_editados:{pagina - 1}",
            )
        )
    if offset + len(clientes) < total:
        nav.append(
            InlineKeyboardButton(
                "SIGUIENTE ➡️",
                callback_data=f"orma_clientes_editados:{pagina + 1}",
            )
        )
    if nav:
        filas.append(nav)

    filas.append([
        InlineKeyboardButton("🏠 MENÚ PRINCIPAL", callback_data="orma_menu_principal"),
        InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar"),
    ])
    return InlineKeyboardMarkup(filas)


def registrar_auditoria_orma(
    propietario_id,
    captura,
    accion,
    *,
    grupo=None,
    detalle=None,
    resultado="OK",
    error=None,
):
    ahora = datetime.now(timezone.utc).isoformat()
    with conectar_db() as conexion:
        conexion.execute(
            """
            INSERT INTO auditoria_orma_acciones (
                propietario_id,
                captura_id,
                objetivo_tipo,
                objetivo_id,
                accion,
                chat_id,
                chat_username,
                chat_nombre,
                detalle,
                resultado,
                error,
                fecha_evento
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(propietario_id),
                int(captura["id"]),
                captura["objetivo_tipo"],
                int(captura["objetivo_id"]),
                str(accion),
                None if not grupo else grupo.get("chat_id"),
                None if not grupo else grupo.get("username"),
                None if not grupo else grupo.get("nombre"),
                detalle,
                str(resultado),
                error,
                ahora,
            ),
        )
        conexion.commit()

    if str(resultado).upper() == "OK":
        registrar_cliente_editado_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            accion=accion,
        )


def obtener_auditoria_orma_reciente(objetivo_tipo, objetivo_id, limite=15):
    with conectar_db() as conexion:
        return conexion.execute(
            """
            SELECT *
            FROM auditoria_orma_acciones
            WHERE objetivo_tipo = ?
              AND objetivo_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (objetivo_tipo, int(objetivo_id), int(limite)),
        ).fetchall()


def obtener_control_grupo_db(
    identidad_tipo,
    identidad_id,
    chat_id,
    *,
    chat_username=None,
    chat_nombre=None,
    crear=True,
):
    with conectar_db() as conexion:
        fila = conexion.execute(
            """
            SELECT *
            FROM control_publicidad_grupos
            WHERE identidad_tipo = ?
              AND identidad_id = ?
              AND chat_id = ?
            LIMIT 1
            """,
            (identidad_tipo, int(identidad_id), int(chat_id)),
        ).fetchone()

        if fila or not crear:
            return fila

        conexion.execute(
            """
            INSERT INTO control_publicidad_grupos (
                identidad_tipo,
                identidad_id,
                chat_id,
                chat_username,
                chat_nombre,
                modo,
                fecha_actualizacion
            )
            VALUES (?, ?, ?, ?, ?, 'HEREDADO', ?)
            """,
            (
                identidad_tipo,
                int(identidad_id),
                int(chat_id),
                chat_username,
                chat_nombre,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conexion.commit()

        return conexion.execute(
            """
            SELECT *
            FROM control_publicidad_grupos
            WHERE identidad_tipo = ?
              AND identidad_id = ?
              AND chat_id = ?
            """,
            (identidad_tipo, int(identidad_id), int(chat_id)),
        ).fetchone()


def actualizar_control_grupo_db(
    identidad_tipo,
    identidad_id,
    chat_id,
    *,
    chat_username=None,
    chat_nombre=None,
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

    datos = {k: v for k, v in campos.items() if k in permitidos}
    if not datos:
        return False

    obtener_control_grupo_db(
        identidad_tipo,
        identidad_id,
        chat_id,
        chat_username=chat_username,
        chat_nombre=chat_nombre,
        crear=True,
    )

    if chat_username is not None:
        datos["chat_username"] = chat_username
    if chat_nombre is not None:
        datos["chat_nombre"] = chat_nombre
    ahora_actualizacion = datetime.now(timezone.utc).isoformat()
    if "limite_dia" in datos:
        datos["ancla_limite_dia"] = (
            ahora_actualizacion if datos["limite_dia"] is not None else None
        )
    datos["fecha_actualizacion"] = ahora_actualizacion

    partes = [f"{k} = ?" for k in datos]
    valores = list(datos.values())
    valores.extend([identidad_tipo, int(identidad_id), int(chat_id)])

    with conectar_db() as conexion:
        cursor = conexion.execute(
            f"""
            UPDATE control_publicidad_grupos
            SET {", ".join(partes)}
            WHERE identidad_tipo = ?
              AND identidad_id = ?
              AND chat_id = ?
            """,
            valores,
        )
        conexion.commit()
        actualizado = cursor.rowcount > 0

    if actualizado:
        registrar_cliente_editado_db(
            identidad_tipo,
            identidad_id,
            accion="PUBLICIDAD_POR_GRUPO",
        )
    return actualizado


def borrar_control_grupo_db(identidad_tipo, identidad_id, chat_id):
    with conectar_db() as conexion:
        conexion.execute(
            """
            DELETE FROM control_publicidad_grupos
            WHERE identidad_tipo = ?
              AND identidad_id = ?
              AND chat_id = ?
            """,
            (identidad_tipo, int(identidad_id), int(chat_id)),
        )
        conexion.commit()

    registrar_cliente_editado_db(
        identidad_tipo,
        identidad_id,
        accion="PUBLICIDAD_GRUPO_RESETEADA",
    )


def copiar_global_a_grupo_db(
    identidad_tipo,
    identidad_id,
    chat_id,
    *,
    chat_username=None,
    chat_nombre=None,
):
    global_cfg = obtener_control_identidad_db(identidad_tipo, identidad_id)
    campos = {
        "modo": "PERSONALIZADO",
        "separacion_segundos": global_cfg["separacion_segundos"],
        "limite_hora": global_cfg["limite_hora"],
        "limite_dia": global_cfg["limite_dia"],
        "limite_semana": global_cfg["limite_semana"],
        "limite_mes": global_cfg["limite_mes"],
        "limite_anio": global_cfg["limite_anio"],
        "controlar_foto": global_cfg["controlar_foto"],
        "controlar_video": global_cfg["controlar_video"],
        "controlar_gif": global_cfg["controlar_gif"],
        "controlar_documento": global_cfg["controlar_documento"],
        "controlar_enlace": global_cfg["controlar_enlace"],
        "controlar_custom_emoji": global_cfg["controlar_custom_emoji"],
    }
    actualizar_control_grupo_db(
        identidad_tipo,
        identidad_id,
        chat_id,
        chat_username=chat_username,
        chat_nombre=chat_nombre,
        **campos,
    )
    return obtener_control_grupo_db(
        identidad_tipo,
        identidad_id,
        chat_id,
        crear=False,
    )


def control_efectivo_para_chat(identidad_tipo, identidad_id, chat):
    cfg_grupo = obtener_control_grupo_db(
        identidad_tipo,
        identidad_id,
        chat.id,
        chat_username=getattr(chat, "username", None),
        chat_nombre=getattr(chat, "title", None),
        crear=False,
    )
    if cfg_grupo and str(cfg_grupo["modo"] or "HEREDADO").upper() != "HEREDADO":
        return cfg_grupo, "GRUPO"
    return obtener_control_identidad_db(identidad_tipo, identidad_id), "GLOBAL"


def contar_publicidad_permitida_grupo_db(
    identidad_tipo,
    identidad_id,
    chat_id,
    desde,
):
    with conectar_db() as conexion:
        fila = conexion.execute(
            """
            SELECT COUNT(*) AS total
            FROM eventos_publicidad_control
            WHERE identidad_tipo = ?
              AND identidad_id = ?
              AND chat_id = ?
              AND decision = 'PERMITIDA'
              AND fecha_evento >= ?
            """,
            (identidad_tipo, int(identidad_id), int(chat_id), desde),
        ).fetchone()
    return int(fila["total"] if fila else 0)


def ultima_publicidad_permitida_grupo_db(
    identidad_tipo,
    identidad_id,
    chat_id,
):
    with conectar_db() as conexion:
        fila = conexion.execute(
            """
            SELECT fecha_evento
            FROM eventos_publicidad_control
            WHERE identidad_tipo = ?
              AND identidad_id = ?
              AND chat_id = ?
              AND decision = 'PERMITIDA'
            ORDER BY id DESC
            LIMIT 1
            """,
            (identidad_tipo, int(identidad_id), int(chat_id)),
        ).fetchone()
    return fila["fecha_evento"] if fila else None


def permisos_mute_total():
    return ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
        can_manage_topics=False,
    )


async def permisos_normales_grupo(bot, chat_ref):
    try:
        chat = await bot.get_chat(chat_ref)
        if getattr(chat, "permissions", None) is not None:
            return chat.permissions
    except TelegramError:
        pass

    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_change_info=False,
        can_invite_users=True,
        can_pin_messages=False,
        can_manage_topics=False,
    )


async def ejecutar_moderacion_grupo_orma(
    bot,
    propietario_id,
    captura,
    accion,
    grupo,
    *,
    duracion_segundos=None,
):
    objetivo_id = int(captura["objetivo_id"])
    chat_ref = grupo["chat_ref"]

    try:
        chat = await bot.get_chat(chat_ref)
        grupo_real = {
            **grupo,
            "chat_id": chat.id,
            "nombre": chat.title or grupo["nombre"],
            "username": chat.username or grupo["username"],
        }

        if accion == "MUTE":
            until_date = None
            if duracion_segundos is not None:
                until_date = datetime.now(timezone.utc) + timedelta(
                    seconds=int(duracion_segundos)
                )
            await bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=objetivo_id,
                permissions=permisos_mute_total(),
                until_date=until_date,
                use_independent_chat_permissions=True,
            )
            detalle = (
                "PERMANENTE"
                if duracion_segundos is None
                else texto_separacion(int(duracion_segundos))
            )

        elif accion == "UNMUTE":
            permisos = await permisos_normales_grupo(bot, chat.id)
            await bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=objetivo_id,
                permissions=permisos,
                use_independent_chat_permissions=True,
            )
            detalle = "PERMISOS RESTAURADOS"

        elif accion == "BAN":
            await bot.ban_chat_member(chat_id=chat.id, user_id=objetivo_id)
            detalle = "BANEADO"

        elif accion == "UNBAN":
            await bot.unban_chat_member(
                chat_id=chat.id,
                user_id=objetivo_id,
                only_if_banned=True,
            )
            detalle = "DESBANEADO"

        elif accion == "EXPULSAR":
            await bot.ban_chat_member(chat_id=chat.id, user_id=objetivo_id)
            await bot.unban_chat_member(chat_id=chat.id, user_id=objetivo_id)
            detalle = "EXPULSADO · PUEDE REINGRESAR"

        else:
            raise RuntimeError("Acción no reconocida.")

        registrar_auditoria_orma(
            propietario_id,
            captura,
            accion,
            grupo=grupo_real,
            detalle=detalle,
            resultado="OK",
        )
        return True, grupo_real["nombre"], detalle

    except Exception as error:
        registrar_auditoria_orma(
            propietario_id,
            captura,
            accion,
            grupo=grupo,
            detalle=None,
            resultado="ERROR",
            error=str(error),
        )
        return False, grupo["nombre"], str(error)


async def ejecutar_moderacion_seleccion_orma(
    bot,
    propietario_id,
    captura,
    accion,
    indices,
    *,
    duracion_segundos=None,
):
    resultados = []
    # Secuencial para no bombardear la API y poder auditar cada grupo.
    for indice in sorted({int(x) for x in indices}):
        grupo = grupo_orma_por_indice(indice)
        if not grupo:
            continue
        resultados.append(
            await ejecutar_moderacion_grupo_orma(
                bot,
                propietario_id,
                captura,
                accion,
                grupo,
                duracion_segundos=duracion_segundos,
            )
        )
        await asyncio.sleep(0.25)
    return resultados


def texto_resultados_moderacion(resultados):
    lineas = []
    correctos = 0
    fallidos = 0
    for ok, nombre, detalle in resultados:
        if ok:
            correctos += 1
            lineas.append(
                f"✅ <b>{html.escape(str(nombre))}</b> · {html.escape(str(detalle))}"
            )
        else:
            fallidos += 1
            lineas.append(
                f"❌ <b>{html.escape(str(nombre))}</b> · {html.escape(str(detalle))}"
            )
    lineas.extend([
        "",
        f"Resultado: ✅ <b>{correctos}</b> · ❌ <b>{fallidos}</b>",
        f"Hora Perú: <b>{formatear_fecha_peru(datetime.now(timezone.utc).isoformat())}</b>",
    ])
    return "\n".join(lineas)


def obtener_resumen_identidad_orma(objetivo_tipo, objetivo_id):
    """
    Resumen compacto para la ficha principal /orma.
    Usa únicamente información que Máximo ya ha observado/registrado.
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
        return f"{modo} · {separacion}"
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
        [InlineKeyboardButton("🔄 ACTUALIZAR FICHA", callback_data=f"orma_ficha:{captura_id}")],
        [
            InlineKeyboardButton("🔐 MEMBRESÍA 7/7", callback_data=f"orma_membresia:{captura_id}"),
            InlineKeyboardButton("📊 ACTIVIDAD 7/7", callback_data=f"orma_actividad:{captura_id}"),
        ],
        [
            InlineKeyboardButton("📣 PUBLICIDAD 7/7", callback_data=f"orma_publicidad:{captura_id}"),
            InlineKeyboardButton("🚪 ENTRADAS / SALIDAS", callback_data=f"orma_movimientos:{captura_id}"),
        ],
        [InlineKeyboardButton("🛡️ CONTROL TOTAL · MODERACIÓN", callback_data=f"orma_mod:{captura_id}")],
        [InlineKeyboardButton("📜 AUDITORÍA /ORMA", callback_data=f"orma_audit:{captura_id}")],
        [
            InlineKeyboardButton("🏠 MENÚ PRINCIPAL", callback_data="orma_menu_principal"),
            InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar"),
        ],
    ])

def _control_efectivo_ficha_por_username(identidad_tipo, identidad_id, username):
    """Configuración efectiva del grupo sin crear ni alterar registros."""
    global_cfg = obtener_control_identidad_db(identidad_tipo, identidad_id)
    clave = str(username or "").lstrip("@").lower()
    with conectar_db() as conexion:
        propia = conexion.execute(
            """
            SELECT *
            FROM control_publicidad_grupos
            WHERE identidad_tipo = ?
              AND identidad_id = ?
              AND LOWER(COALESCE(chat_username, '')) = ?
            ORDER BY fecha_actualizacion DESC
            LIMIT 1
            """,
            (identidad_tipo, int(identidad_id), clave),
        ).fetchone()

    if propia and str(propia["modo"] or "HEREDADO").upper() != "HEREDADO":
        return propia, "PROPIA"
    return global_cfg, "GLOBAL"


def _uso_publicidad_ficha_por_username(identidad_tipo, identidad_id, username, cfg=None):
    """Consumo real por grupo, usando el username oficial como llave estable."""
    limites = limites_periodos_publicidad()
    clave = str(username or "").lstrip("@").lower()
    resultado = {}
    with conectar_db() as conexion:
        for periodo, inicio in limites.items():
            if periodo == "dia" and cfg is not None:
                inicio_diario, _ = ciclo_diario_24h_config(cfg)
                if inicio_diario is not None:
                    inicio = inicio_diario.isoformat()
            fila = conexion.execute(
                """
                SELECT COUNT(*) AS total
                FROM eventos_publicidad_control
                WHERE identidad_tipo = ?
                  AND identidad_id = ?
                  AND LOWER(COALESCE(chat_username, '')) = ?
                  AND decision = 'PERMITIDA'
                  AND fecha_evento >= ?
                """,
                (identidad_tipo, int(identidad_id), clave, inicio),
            ).fetchone()
            resultado[periodo] = int(fila["total"] if fila else 0)
    return resultado


def _tipos_controlados_ficha(cfg):
    campos = [
        ("controlar_foto", "Foto"),
        ("controlar_video", "Video"),
        ("controlar_gif", "GIF"),
        ("controlar_documento", "Documento"),
        ("controlar_enlace", "Enlace"),
        ("controlar_custom_emoji", "Premium emoji"),
    ]
    controlados = [nombre for campo, nombre in campos if bool(cfg[campo])]
    libres = [nombre for campo, nombre in campos if not bool(cfg[campo])]
    return controlados, libres


def _estado_operativo_publicidad_ficha(cfg, uso):
    modo = str(cfg["modo"] or "HEREDADO").upper()
    if modo == "BLOQUEADO":
        return "🔴 BLOQUEADA", ["🛡 Motivo: <b>Restricción administrativa</b>"]
    if modo == "EXCLUIDO":
        return "🟢 SIN CONTROL", ["♾️ Publicidad: <b>FUERA DEL CONTROL INDIVIDUAL</b>"]
    if modo == "ILIMITADO":
        return "🟢 SIN RESTRICCIONES", ["♾️ Cupo: <b>ILIMITADO</b>"]

    etiquetas = {
        "hora": "Hora",
        "dia": "Día",
        "semana": "Semana",
        "mes": "Mes",
        "anio": "Año",
    }
    activos = []
    agotados = []
    for periodo, campo in [
        ("hora", "limite_hora"),
        ("dia", "limite_dia"),
        ("semana", "limite_semana"),
        ("mes", "limite_mes"),
        ("anio", "limite_anio"),
    ]:
        limite = cfg[campo]
        if limite is None:
            continue
        limite = int(limite)
        usados = int(uso[periodo])
        quedan = max(0, limite - usados)
        activos.append((periodo, limite, usados, quedan))
        if usados >= limite:
            agotados.append((periodo, limite, usados, quedan))

    lineas = []
    if agotados:
        estado = "🔴 BLOQUEADA POR LÍMITE"
    elif activos or cfg["separacion_segundos"]:
        estado = "🟡 LIMITADA"
    else:
        estado = "🟢 SIN RESTRICCIONES"

    # Solo mostramos límites que realmente existen; nada de filas muertas SIN LÍMITE.
    for periodo, limite, usados, quedan in activos:
        lineas.append(
            f"📊 {etiquetas[periodo]}: <b>{quedan} de {limite} disponibles</b> "
            f"· usados {usados}"
        )

    if cfg["separacion_segundos"]:
        lineas.append(
            f"⏱ Separación: <b>{texto_separacion(cfg['separacion_segundos'])}</b>"
        )
    elif not activos:
        lineas.append("♾️ Cupo: <b>ILIMITADO</b>")

    return estado, lineas



def _formatear_tiempo_restante_ficha(segundos):
    segundos = max(0, int(segundos or 0))
    dias, resto = divmod(segundos, 86400)
    horas, resto = divmod(resto, 3600)
    minutos, _ = divmod(resto, 60)

    partes = []
    if dias:
        partes.append(f"{dias} d")
    if horas:
        partes.append(f"{horas} h")
    if minutos or not partes:
        partes.append(f"{minutos} min")
    return " ".join(partes[:2])


def _proxima_renovacion_ficha(periodo):
    """
    Respeta exactamente los períodos vigentes del bot:
    hora = ventana móvil; día/semana/mes/año = calendario America/Lima.
    """
    ahora_local = datetime.now(ZONA_PERU)

    if periodo == "dia":
        return (ahora_local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)

    if periodo == "semana":
        dias_hasta_lunes = 7 - ahora_local.weekday()
        return (ahora_local + timedelta(days=dias_hasta_lunes)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)

    if periodo == "mes":
        if ahora_local.month == 12:
            siguiente = ahora_local.replace(
                year=ahora_local.year + 1,
                month=1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        else:
            siguiente = ahora_local.replace(
                month=ahora_local.month + 1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        return siguiente.astimezone(timezone.utc)

    if periodo == "anio":
        return ahora_local.replace(
            year=ahora_local.year + 1,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ).astimezone(timezone.utc)

    return None


def _proxima_liberacion_hora_ficha(
    identidad_tipo,
    identidad_id,
    username,
):
    clave = str(username or "").lstrip("@").lower()
    inicio = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    with conectar_db() as conexion:
        fila = conexion.execute(
            """
            SELECT fecha_evento
            FROM eventos_publicidad_control
            WHERE identidad_tipo = ?
              AND identidad_id = ?
              AND LOWER(COALESCE(chat_username, '')) = ?
              AND decision = 'PERMITIDA'
              AND fecha_evento >= ?
            ORDER BY fecha_evento ASC
            LIMIT 1
            """,
            (
                identidad_tipo,
                int(identidad_id),
                clave,
                inicio,
            ),
        ).fetchone()

    if not fila:
        return None

    try:
        fecha = datetime.fromisoformat(str(fila["fecha_evento"]))
        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)
        return fecha.astimezone(timezone.utc) + timedelta(hours=1)
    except (TypeError, ValueError):
        return None


def _linea_reloj_restriccion_ficha(
    identidad_tipo,
    identidad_id,
    username,
    cfg,
    uso,
):
    """
    Muestra solamente la renovación útil/limitante.
    No altera contadores ni restricciones.
    """
    periodos = [
        ("hora", "limite_hora"),
        ("dia", "limite_dia"),
        ("semana", "limite_semana"),
        ("mes", "limite_mes"),
        ("anio", "limite_anio"),
    ]

    activos = []
    for periodo, campo in periodos:
        limite = cfg[campo]
        if limite is None:
            continue
        activos.append(
            (
                periodo,
                int(limite),
                int(uso[periodo]),
            )
        )

    if not activos:
        return None

    # Priorizar un período agotado; si ninguno está agotado,
    # mostrar la renovación del límite más corto configurado.
    elegido = next(
        (item for item in activos if item[2] >= item[1]),
        activos[0],
    )
    periodo, limite, usados = elegido

    if periodo == "hora":
        proxima = _proxima_liberacion_hora_ficha(
            identidad_tipo,
            identidad_id,
            username,
        )
    elif periodo == "dia":
        _, proxima = ciclo_diario_24h_config(cfg)
    else:
        proxima = _proxima_renovacion_ficha(periodo)

    if proxima is None:
        return None

    ahora = datetime.now(timezone.utc)
    restante = max(0, int((proxima - ahora).total_seconds()))
    faltante = _formatear_tiempo_restante_ficha(restante)
    fecha_peru = proxima.astimezone(ZONA_PERU).strftime("%d/%m %I:%M %p").lower()

    if usados >= limite:
        return (
            f"⏳ Se habilita en: <b>{faltante}</b> "
            f"· {fecha_peru}"
        )

    return (
        f"⏳ Renovación del cupo: <b>{faltante}</b> "
        f"· {fecha_peru}"
    )


def _lineas_control_grupo_ficha(captura, grupo, rol):
    if captura["objetivo_tipo"] not in {"USUARIO", "BOT"}:
        return ["📣 Publicidad: <b>NO APLICA</b>"]

    rol_txt = str(rol or "").lower()
    if rol_txt in {"fuera del grupo", "expulsado"}:
        return ["🚫 Publicidad: <b>NO APLICA · NO ES MIEMBRO</b>"]

    cfg, origen = _control_efectivo_ficha_por_username(
        captura["objetivo_tipo"],
        captura["objetivo_id"],
        grupo["username"],
    )
    uso = _uso_publicidad_ficha_por_username(
        captura["objetivo_tipo"],
        captura["objetivo_id"],
        grupo["username"],
        cfg,
    )
    estado, detalles = _estado_operativo_publicidad_ficha(cfg, uso)
    controlados, libres = _tipos_controlados_ficha(cfg)

    lineas = [f"📣 Publicidad: <b>{estado}</b>"]
    lineas.extend(detalles)

    try:
        reloj = _linea_reloj_restriccion_ficha(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            grupo["username"],
            cfg,
            uso,
        )
        if reloj:
            lineas.append(reloj)
    except Exception:
        logging.exception(
            "Reloj /orma omitido por error objetivo=%s grupo=%s",
            captura["objetivo_id"],
            grupo["username"],
        )

    if origen == "PROPIA":
        lineas.append("🎯 Regla: <b>PROPIA DE ESTE GRUPO</b>")
    elif str(cfg["modo"] or "HEREDADO").upper() == "HEREDADO":
        lineas.append("🌐 Regla: <b>GLOBAL / HEREDADA</b>")

    if len(controlados) == 6:
        lineas.append("🎛 Tipos: <b>TODOS LOS PUBLICITARIOS CONTROLADOS</b>")
    elif not controlados:
        lineas.append("🎛 Tipos: <b>SIN TIPOS CONTROLADOS</b>")
    else:
        lineas.append(f"🎛 Controlados: <b>{', '.join(controlados)}</b>")
        if libres:
            lineas.append(f"🟢 Libres: <b>{', '.join(libres)}</b>")

    return lineas


async def construir_texto_ficha_orma(captura):
    objetivo_id = captura["objetivo_id"]
    rol_origen = await obtener_rol_en_grupo(captura["chat_id"], objetivo_id)

    estados = []
    if captura["objetivo_tipo"] in {"USUARIO", "BOT"}:
        estados = await estado_7grupos_orma_concurrente(objetivo_id)

    habilitado = (
        bool(estados)
        and len(estados) == TOTAL_GRUPOS_OBLIGATORIOS
        and all(item["miembro"] for item in estados)
    ) if captura["objetivo_tipo"] in {"USUARIO", "BOT"} else None

    por_grupos = resumen_por_grupos_orma(
        captura["objetivo_tipo"],
        objetivo_id,
    )
    estados_por_username = {
        str(item["username"]).lower(): item
        for item in estados
    }

    lineas = [
        "🦍 <b>MÁXIMO CONTROL TOTAL · FICHA AVANZADA</b>",
        sello_version_panel(),
        "",
        cabecera_identidad_orma(
            captura,
            rol=rol_origen,
            habilitado=habilitado,
        ),
        "",
        "📍 <b>ESTADO Y CONTROL POR GRUPO</b>",
    ]

    sin_restricciones = 0
    limitados = 0
    bloqueados = 0
    fuera = 0

    for grupo in por_grupos:
        clave = str(grupo["username"] or "").lower()
        estado = estados_por_username.get(clave)

        if captura["objetivo_tipo"] not in {"USUARIO", "BOT"}:
            marca = "⚪"
            rol = "No aplica"
        elif estado is None:
            marca = "❔"
            rol = "No disponible"
        elif estado["error"]:
            marca = "⚠️"
            rol = estado["rol"]
        elif estado["miembro"]:
            marca = "✅"
            rol = estado["rol"]
        else:
            marca = "❌"
            rol = estado["rol"]

        lineas.extend([
            "",
            f"<b>{grupo['indice']}. {marca} {nombre_grupo_orma(grupo)}</b>",
            f"👤 Estado: <b>{html.escape(str(rol))}</b>",
        ])

        operativas = _lineas_control_grupo_ficha(captura, grupo, rol)
        lineas.extend(operativas)

        primera = operativas[0] if operativas else ""
        if "NO ES MIEMBRO" in primera or "NO APLICA" in primera:
            fuera += 1
        elif "🔴" in primera:
            bloqueados += 1
        elif "🟡" in primera:
            limitados += 1
        else:
            sin_restricciones += 1

    lineas.extend([
        "",
        "📊 <b>RESUMEN DE CONTROL</b>",
        f"🟢 Sin restricciones: <b>{sin_restricciones} grupos</b>",
        f"🟡 Con restricciones: <b>{limitados} grupos</b>",
        f"🔴 Bloqueados: <b>{bloqueados} grupos</b>",
    ])
    if fuera:
        lineas.append(f"⚪ Fuera / no aplica: <b>{fuera} grupos</b>")

    lineas.extend([
        "",
        "ℹ️ <i>Solo se muestran restricciones y cupos útiles. "
        "Los contadores técnicos siguen registrándose internamente.</i>",
    ])

    texto = "\n".join(lineas)
    if len(texto) > 4050:
        texto = texto[:3970] + (
            "\n\n<i>Ficha abreviada por límite de Telegram. "
            "Los paneles inferiores conservan el detalle completo.</i>"
        )
    return texto

async def mostrar_ficha_orma_privada(bot, propietario_id, captura_id):
    captura = obtener_captura_orma(captura_id)
    if not captura or int(captura["propietario_id"]) != int(propietario_id):
        return False

    texto = await construir_texto_ficha_orma(captura)
    teclado = teclado_ficha_orma(captura_id)
    panel_id = PANELES_ORMA.get(propietario_id) or obtener_panel_orma_db(propietario_id)

    if panel_id:
        try:
            editado = await bot.edit_message_text(
                chat_id=propietario_id,
                message_id=panel_id,
                text=texto,
                parse_mode="HTML",
                reply_markup=teclado,
            )
            PANELES_ORMA[propietario_id] = editado.message_id
            guardar_panel_orma_db(propietario_id, editado.message_id)
            return True
        except TelegramError as error:
            if "message is not modified" in str(error).lower():
                PANELES_ORMA[propietario_id] = panel_id
                return True
            logging.info(
                "Panel /orma previo no reutilizable propietario=%s message_id=%s: %s",
                propietario_id,
                panel_id,
                error,
            )

    try:
        enviado = await bot.send_message(
            chat_id=propietario_id,
            text=texto,
            parse_mode="HTML",
            reply_markup=teclado,
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

    # Seguridad: el panel interno /orma solo puede abrirlo una identidad
    # administrativa autorizada. El modo anónimo continúa soportado.
    if not es_administrador_maximo(ejecutor):
        return

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

    propietario_id = ejecutor.id
    if ejecutor.id == GROUP_ANONYMOUS_BOT_ID:
        propietario_id = ORMA_ADMIN_USER_ID or ADMIN_USER_ID
        if propietario_id <= 0:
            logging.error(
                "No se puede resolver /orma anónimo: no existe un ID administrativo configurado."
            )
            return

    captura_id = guardar_captura_orma(
        propietario_id, tipo, objetivo_id, objetivo_username,
        objetivo_nombre, objetivo_es_bot, chat, origen.message_id,
    )
    CAPTURAS_ORMA[propietario_id] = captura_id
    await mostrar_ficha_orma_privada(context.bot, propietario_id, captura_id)



async def resolver_chat_grupo_orma(bot, indice):
    grupo = grupo_orma_por_indice(indice)
    if not grupo:
        return None
    try:
        chat = await bot.get_chat(grupo["chat_ref"])
        return {
            **grupo,
            "chat_id": chat.id,
            "nombre": chat.title or grupo["nombre"],
            "username": chat.username or grupo["username"],
        }
    except TelegramError:
        return {
            **grupo,
            "chat_id": None,
        }


async def teclado_lista_publicidad_grupos(captura, bot):
    filas = []
    for indice in range(1, 8):
        grupo = await resolver_chat_grupo_orma(bot, indice)
        if not grupo or grupo["chat_id"] is None:
            etiqueta = f"{indice}. ⚠️ Grupo no disponible"
        else:
            cfg = obtener_control_grupo_db(
                captura["objetivo_tipo"],
                captura["objetivo_id"],
                grupo["chat_id"],
                crear=False,
            )
            modo = (
                str(cfg["modo"] or "HEREDADO").upper()
                if cfg
                else "HEREDADO"
            )
            etiqueta = f"{indice}. {modo} · {grupo['nombre']}"
        filas.append([
            InlineKeyboardButton(
                etiqueta[:60],
                callback_data=f"orma_pg:{captura['id']}:{indice}",
            )
        ])

    filas.extend([
        [InlineKeyboardButton(
            "🌐 CONTROL GLOBAL 7/7",
            callback_data=f"orma_publicidad:{captura['id']}",
        )],
        [InlineKeyboardButton(
            "⬅️ RETROCEDER",
            callback_data=f"orma_ficha:{captura['id']}",
        )],
        [
            InlineKeyboardButton("🏠 MENÚ PRINCIPAL", callback_data="orma_menu_principal"),
            InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar"),
        ],
    ])
    return InlineKeyboardMarkup(filas)


async def texto_control_publicidad_grupo(captura, grupo, cfg):
    global_cfg = obtener_control_identidad_db(
        captura["objetivo_tipo"],
        captura["objetivo_id"],
    )
    heredado = str(cfg["modo"] or "HEREDADO").upper() == "HEREDADO"
    efectivo = global_cfg if heredado else cfg

    limites = limites_periodos_publicidad()
    uso = {}
    for periodo, inicio in limites.items():
        uso[periodo] = contar_publicidad_permitida_grupo_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            grupo["chat_id"],
            inicio,
        )

    activos, libres = resumen_tipos_controlados(efectivo)
    rol = await obtener_rol_en_grupo(grupo["chat_id"], captura["objetivo_id"])
    habilitado = rol.lower() not in {
        "fuera del grupo",
        "expulsado",
        "desconocido",
        "no disponible",
    }

    return "\n".join([
        "🎯 <b>CONTROL PUBLICITARIO · GRUPO INDIVIDUAL</b>",
        "",
        cabecera_identidad_orma(
            captura,
            rol=rol,
            habilitado=habilitado,
        ),
        "",
        f"📍 Grupo: <b>{html.escape(str(grupo['nombre']))}</b>",
        f"• UserName grupo: <b>@{html.escape(str(grupo['username']).lstrip('@'))}</b>",
        "",
        f"⚙️ Modo propio: <b>{html.escape(str(cfg['modo']))}</b>",
        (
            "• Configuración efectiva: <b>GLOBAL HEREDADA</b>"
            if heredado
            else "• Configuración efectiva: <b>PROPIA DE ESTE GRUPO</b>"
        ),
        f"• Separación: <b>{texto_separacion(efectivo['separacion_segundos'])}</b>",
        "",
        "🔢 <b>LÍMITES / USO EN ESTE GRUPO</b>",
        f"• Hora: {texto_valor_limite(efectivo['limite_hora'])} · usados <b>{uso['hora']}</b>",
        f"• Día: {texto_valor_limite(efectivo['limite_dia'])} · usados <b>{uso['dia']}</b>",
        f"• Semana: {texto_valor_limite(efectivo['limite_semana'])} · usados <b>{uso['semana']}</b>",
        f"• Mes: {texto_valor_limite(efectivo['limite_mes'])} · usados <b>{uso['mes']}</b>",
        f"• Año: {texto_valor_limite(efectivo['limite_anio'])} · usados <b>{uso['anio']}</b>",
        "",
        "🎛 <b>TIPOS</b>",
        f"• Controlados: <b>{', '.join(activos) if activos else 'NINGUNO'}</b>",
        f"• Libres: <b>{', '.join(libres) if libres else 'NINGUNO'}</b>",
        "• Texto normal puro: <b>SIEMPRE LIBRE</b>",
        "",
        f"🕐 Actualizado: <b>{formatear_fecha_peru(cfg['fecha_actualizacion'])}</b>",
    ])


def teclado_publicidad_grupo(captura_id, indice, cfg):
    modo = str(cfg["modo"] or "HEREDADO").upper()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"⚙️ MODO: {modo}",
            callback_data=f"orma_pgm:{captura_id}:{indice}",
        )],
        [InlineKeyboardButton(
            f"⏱ SEPARACIÓN: {texto_separacion(cfg['separacion_segundos'])}",
            callback_data=f"orma_pgs:{captura_id}:{indice}",
        )],
        [
            InlineKeyboardButton(
                "🔢 LÍMITES",
                callback_data=f"orma_pgl:{captura_id}:{indice}",
            ),
            InlineKeyboardButton(
                "🎛 TIPOS",
                callback_data=f"orma_pgt:{captura_id}:{indice}",
            ),
        ],
        [InlineKeyboardButton(
            "♻️ HEREDAR GLOBAL",
            callback_data=f"orma_pgr:{captura_id}:{indice}",
        )],
        [InlineKeyboardButton(
            "⬅️ LISTA DE GRUPOS",
            callback_data=f"orma_pg_lista:{captura_id}",
        )],
        [
            InlineKeyboardButton("🏠 MENÚ PRINCIPAL", callback_data="orma_menu_principal"),
            InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar"),
        ],
    ])


def seleccion_moderacion_orma(propietario_id, captura_id, accion):
    estado = SELECCIONES_MODERACION_ORMA.get(int(propietario_id))
    if (
        not estado
        or int(estado.get("captura_id", -1)) != int(captura_id)
        or estado.get("accion") != accion
    ):
        estado = {
            "captura_id": int(captura_id),
            "accion": accion,
            "grupos": set(),
            "duracion_segundos": None,
        }
        SELECCIONES_MODERACION_ORMA[int(propietario_id)] = estado
    return estado


def teclado_seleccion_moderacion(captura_id, accion, seleccionados):
    filas = [
        [
            InlineKeyboardButton(
                "✅ TODOS 7/7",
                callback_data=f"orma_modall:{captura_id}:{accion}",
            ),
            InlineKeyboardButton(
                "🚫 NINGUNO",
                callback_data=f"orma_modnone:{captura_id}:{accion}",
            ),
        ]
    ]
    for indice in range(1, 8):
        grupo = grupo_orma_por_indice(indice)
        marca = "✅" if indice in seleccionados else "⬜"
        filas.append([
            InlineKeyboardButton(
                f"{marca} {indice}. {grupo['nombre']}"[:60],
                callback_data=f"orma_modtog:{captura_id}:{accion}:{indice}",
            )
        ])
    filas.extend([
        [InlineKeyboardButton(
            "➡️ CONTINUAR",
            callback_data=f"orma_modnext:{captura_id}:{accion}",
        )],
        [InlineKeyboardButton(
            "⬅️ MODERACIÓN",
            callback_data=f"orma_mod:{captura_id}",
        )],
    ])
    return InlineKeyboardMarkup(filas)


def texto_accion_moderacion(accion):
    return {
        "MUTE": "🔇 MUTEAR",
        "UNMUTE": "🔊 DESMUTEAR",
        "EXPULSAR": "👢 EXPULSAR",
        "BAN": "🚫 BANEAR",
        "UNBAN": "♻️ DESBANEAR",
    }.get(accion, accion)


async def orma_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    usuario = update.effective_user
    if not query or not usuario:
        return

    data = query.data or ""

    if data == "orma_cerrar":
        ENTRADAS_CONTROL_PUBLICIDAD.pop(usuario.id, None)
        ENTRADAS_ORMA_TOTAL.pop(usuario.id, None)
        SELECCIONES_MODERACION_ORMA.pop(usuario.id, None)
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
        ENTRADAS_ORMA_TOTAL.pop(usuario.id, None)
        SELECCIONES_MODERACION_ORMA.pop(usuario.id, None)
        await query.answer()
        try:
            await safe_query_edit_message(query,
                "🦍 <b>MÁXIMO CONTROL TOTAL</b>\n\n"
                "Centro privado de administración.\n\n"
                "📌 Responde cualquier mensaje en un grupo controlado "
                "con <code>/orma</code> para abrir su expediente.\n\n"
                "Los comandos y datos escritos se eliminan automáticamente "
                "después de ser procesados.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "👥 CLIENTES EDITADOS",
                            callback_data="orma_clientes_editados:0",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🗑 CERRAR",
                            callback_data="orma_cerrar",
                        )
                    ],
                ]),
            )
        except TelegramError:
            pass
        return

    if data.startswith("orma_clientes_editados:"):
        try:
            pagina = int(data.rsplit(":", 1)[1])
        except (TypeError, ValueError):
            pagina = 0

        await query.answer()
        await safe_query_edit_message(
            query,
            texto_clientes_editados_db(pagina),
            parse_mode="HTML",
            reply_markup=teclado_clientes_editados_db(pagina),
        )
        return

    if data.startswith("orma_cliente_editado:"):
        try:
            _, tipo, identidad_txt = data.split(":", 2)
            identidad_id = int(identidad_txt)
        except (TypeError, ValueError):
            await query.answer("Cliente no disponible.", show_alert=True)
            return

        cliente = obtener_cliente_editado_db(tipo, identidad_id)
        if not cliente:
            await query.answer("Cliente no disponible.", show_alert=True)
            return

        captura_id = crear_captura_desde_cliente_editado_db(
            usuario.id,
            tipo,
            identidad_id,
        )
        if not captura_id:
            await query.answer(
                "No existe una captura válida para este cliente.",
                show_alert=True,
            )
            return

        CAPTURAS_ORMA[usuario.id] = captura_id
        await query.answer()
        await mostrar_ficha_orma_privada(context.bot, usuario.id, captura_id)
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
                "🔐 <b>MEMBRESÍA</b>\n\n"
                "Esta identidad es un canal/chat y no puede evaluarse con la regla de usuario 7/7."
            )
        else:
            estado = await obtener_estado_membresia_7de7(captura["objetivo_id"])
            marcas = estado_membresia_por_username(estado)
            lineas = [
                "🔐 <b>MEMBRESÍA 7/7 · DETALLE</b>",
                "",
                f"Progreso: <b>{len(estado['completados'])}/{estado['total']}</b>",
                "",
            ]
            for indice, grupo in enumerate(obtener_grupos_obligatorios_db(), start=1):
                clave = str(grupo["username"] or "").lower()
                marca = marcas.get(clave, "❔")
                lineas.append(
                    f"{indice}. {marca} <b>{html.escape(str(grupo['nombre']))}</b>"
                )

            if estado["errores"]:
                lineas.extend([
                    "",
                    "⚠️ Hay grupos cuya consulta a Telegram devolvió error; "
                    "se marcan con ⚠️ y no se asumen como membresía confirmada.",
                ])
            texto_membresia = "\n".join(lineas)

        await safe_query_edit_message(query,
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
        por_grupos = resumen_por_grupos_orma(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )

        lineas = [
            "📊 <b>ACTIVIDAD POR GRUPO</b>",
            "",
            (
                f"Global: H <b>{resumen['hora']}</b> · "
                f"D <b>{resumen['dia']}</b> · "
                f"S <b>{resumen['semana']}</b> · "
                f"M <b>{resumen['mes']}</b>"
            ),
            "",
        ]

        for grupo in por_grupos:
            lineas.extend([
                f"<b>{grupo['indice']}. {nombre_grupo_orma(grupo)}</b>",
                (
                    f"• H {grupo['actividad_hora']} · "
                    f"D {grupo['actividad_dia']} · "
                    f"S {grupo['actividad_semana']} · "
                    f"M {grupo['actividad_mes']} · "
                    f"T <b>{grupo['actividad_total']}</b>"
                ),
                f"• Última: <b>{formatear_fecha_peru(grupo['ultima_actividad'])}</b>",
                "",
            ])

        lineas.append("📦 <b>TIPOS DE CONTENIDO · MES</b>")
        if resumen["tipos_mes"]:
            for fila in resumen["tipos_mes"][:10]:
                lineas.append(
                    f"• {html.escape(str(fila['tipo_contenido']))}: <b>{fila['total']}</b>"
                )
        else:
            lineas.append("• Sin actividad registrada todavía.")

        await safe_query_edit_message(query,
            "\n".join(lineas),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 ACTUALIZAR", callback_data=f"orma_actividad:{captura_id}")],
                [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_ficha:{captura_id}")],
                [
                    InlineKeyboardButton("🏠 MENÚ PRINCIPAL", callback_data="orma_menu_principal"),
                    InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar"),
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
                    lineas.extend([
                        f"• <b>{html.escape(str(fila['grupo']))}</b>: "
                        f"➕ {int(fila['entradas'] or 0)} · "
                        f"➖ {int(fila['salidas'] or 0)}",
                        f"  ↳ última entrada: {formatear_fecha_peru(fila['ultima_entrada'])}",
                        f"  ↳ última salida: {formatear_fecha_peru(fila['ultima_salida'])}",
                    ])
            else:
                lineas.append("• Sin movimientos registrados todavía.")

            # Dato de seguimiento útil sin inventar historial anterior.
            if resumen["ultima_entrada"] and not resumen["ultima_salida"]:
                estado_mov = "🟢 Último movimiento registrado: ENTRADA"
            elif resumen["ultima_salida"] and not resumen["ultima_entrada"]:
                estado_mov = "🔴 Último movimiento registrado: SALIDA"
            elif resumen["ultima_entrada"] and resumen["ultima_salida"]:
                try:
                    fe = datetime.fromisoformat(resumen["ultima_entrada"])
                    fs = datetime.fromisoformat(resumen["ultima_salida"])
                    if fe.tzinfo is None:
                        fe = fe.replace(tzinfo=timezone.utc)
                    if fs.tzinfo is None:
                        fs = fs.replace(tzinfo=timezone.utc)
                    estado_mov = (
                        "🟢 Último movimiento registrado: ENTRADA"
                        if fe > fs
                        else "🔴 Último movimiento registrado: SALIDA"
                    )
                except (TypeError, ValueError):
                    estado_mov = "⚪ Último movimiento: No disponible"
            else:
                estado_mov = "⚪ Sin movimientos observados todavía"

            lineas.extend([
                "",
                f"<b>{estado_mov}</b>",
                "",
                "ℹ️ Solo se contabilizan movimientos observados desde "
                "la activación de este registro; no se inventa historial anterior.",
            ])

        await safe_query_edit_message(query,
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

    if data.startswith("orma_audit:"):
        captura_id = int(data.split(":", 1)[1])
        captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        await query.answer()

        rol = await obtener_rol_en_grupo(
            captura["chat_id"],
            captura["objetivo_id"],
        )
        filas = obtener_auditoria_orma_reciente(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            limite=15,
        )
        lineas = [
            "📜 <b>AUDITORÍA /ORMA</b>",
            "",
            cabecera_identidad_orma(captura, rol=rol),
            "",
            "Últimas acciones:",
        ]
        if not filas:
            lineas.append("• Sin acciones administrativas registradas.")
        else:
            for fila in filas:
                marca = "✅" if fila["resultado"] == "OK" else "❌"
                grupo = fila["chat_nombre"] or fila["chat_username"] or "GLOBAL"
                lineas.append(
                    f"{marca} {formatear_fecha_peru(fila['fecha_evento'])} · "
                    f"<b>{html.escape(str(fila['accion']))}</b> · "
                    f"{html.escape(str(grupo))}"
                )

        await safe_query_edit_message(
            query,
            "\n".join(lineas),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 ACTUALIZAR", callback_data=f"orma_audit:{captura_id}")],
                [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_ficha:{captura_id}")],
                [
                    InlineKeyboardButton("🏠 MENÚ PRINCIPAL", callback_data="orma_menu_principal"),
                    InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar"),
                ],
            ]),
        )
        return

    if data.startswith("orma_pg_lista:"):
        captura_id = int(data.split(":", 1)[1])
        captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        await query.answer()
        rol = await obtener_rol_en_grupo(
            captura["chat_id"],
            captura["objetivo_id"],
        )
        await safe_query_edit_message(
            query,
            "🎯 <b>CONTROL PUBLICITARIO POR GRUPO</b>\n\n"
            + cabecera_identidad_orma(captura, rol=rol)
            + "\n\nSelecciona un grupo. Cada grupo puede tener reglas totalmente "
              "independientes. HEREDADO significa que obedece al control global 7/7.",
            parse_mode="HTML",
            reply_markup=await teclado_lista_publicidad_grupos(
                captura,
                context.bot,
            ),
        )
        return

    if data.startswith("orma_pg:"):
        _, captura_txt, indice_txt = data.split(":", 2)
        captura_id = int(captura_txt)
        indice = int(indice_txt)
        captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        grupo = await resolver_chat_grupo_orma(context.bot, indice)
        if not grupo or grupo["chat_id"] is None:
            await query.answer("No se pudo resolver ese grupo.", show_alert=True)
            return
        cfg = obtener_control_grupo_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            grupo["chat_id"],
            chat_username=grupo["username"],
            chat_nombre=grupo["nombre"],
            crear=True,
        )
        await query.answer()
        await safe_query_edit_message(
            query,
            await texto_control_publicidad_grupo(captura, grupo, cfg),
            parse_mode="HTML",
            reply_markup=teclado_publicidad_grupo(captura_id, indice, cfg),
        )
        return

    if data.startswith("orma_pgm:"):
        _, captura_txt, indice_txt = data.split(":", 2)
        captura_id = int(captura_txt)
        indice = int(indice_txt)
        await query.answer()
        await safe_query_edit_message(
            query,
            "⚙️ <b>MODO PUBLICITARIO DEL GRUPO</b>\n\n"
            "HEREDADO: usa la regla global del usuario.\n"
            "PERSONALIZADO: este grupo tiene tiempos/cantidades propios.\n"
            "ILIMITADO: registra pero no limita en este grupo.\n"
            "BLOQUEADO: elimina toda publicidad controlable en este grupo.\n"
            "EXCLUIDO: no aplica control publicitario en este grupo.\n\n"
            "El texto normal puro permanece libre.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("HEREDADO", callback_data=f"orma_pgmset:{captura_id}:{indice}:HEREDADO"),
                    InlineKeyboardButton("PERSONALIZADO", callback_data=f"orma_pgmset:{captura_id}:{indice}:PERSONALIZADO"),
                ],
                [
                    InlineKeyboardButton("ILIMITADO", callback_data=f"orma_pgmset:{captura_id}:{indice}:ILIMITADO"),
                    InlineKeyboardButton("BLOQUEADO", callback_data=f"orma_pgmset:{captura_id}:{indice}:BLOQUEADO"),
                ],
                [InlineKeyboardButton("EXCLUIDO", callback_data=f"orma_pgmset:{captura_id}:{indice}:EXCLUIDO")],
                [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_pg:{captura_id}:{indice}")],
            ]),
        )
        return

    if data.startswith("orma_pgmset:"):
        _, captura_txt, indice_txt, modo = data.split(":", 3)
        captura_id = int(captura_txt)
        indice = int(indice_txt)
        captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        grupo = await resolver_chat_grupo_orma(context.bot, indice)
        if not grupo or grupo["chat_id"] is None:
            await query.answer("Grupo no disponible.", show_alert=True)
            return

        if modo == "HEREDADO":
            borrar_control_grupo_db(
                captura["objetivo_tipo"],
                captura["objetivo_id"],
                grupo["chat_id"],
            )
        elif modo == "PERSONALIZADO":
            copiar_global_a_grupo_db(
                captura["objetivo_tipo"],
                captura["objetivo_id"],
                grupo["chat_id"],
                chat_username=grupo["username"],
                chat_nombre=grupo["nombre"],
            )
        else:
            actualizar_control_grupo_db(
                captura["objetivo_tipo"],
                captura["objetivo_id"],
                grupo["chat_id"],
                chat_username=grupo["username"],
                chat_nombre=grupo["nombre"],
                modo=modo,
            )

        registrar_auditoria_orma(
            usuario.id,
            captura,
            "PUBLICIDAD_MODO_GRUPO",
            grupo=grupo,
            detalle=modo,
            resultado="OK",
        )
        await query.answer(f"Modo: {modo}")
        cfg = obtener_control_grupo_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            grupo["chat_id"],
            chat_username=grupo["username"],
            chat_nombre=grupo["nombre"],
            crear=True,
        )
        await safe_query_edit_message(
            query,
            await texto_control_publicidad_grupo(captura, grupo, cfg),
            parse_mode="HTML",
            reply_markup=teclado_publicidad_grupo(captura_id, indice, cfg),
        )
        return

    if data.startswith("orma_pgs:"):
        _, captura_txt, indice_txt = data.split(":", 2)
        captura_id = int(captura_txt)
        indice = int(indice_txt)
        await query.answer()
        await safe_query_edit_message(
            query,
            "⏱ <b>SEPARACIÓN · ESTE GRUPO</b>\n\n"
            "Define el tiempo mínimo entre publicidades de esta identidad "
            "solamente en el grupo seleccionado.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("SIN SEPARACIÓN", callback_data=f"orma_pgsset:{captura_id}:{indice}:0")],
                [
                    InlineKeyboardButton("1 min", callback_data=f"orma_pgsset:{captura_id}:{indice}:60"),
                    InlineKeyboardButton("3 min", callback_data=f"orma_pgsset:{captura_id}:{indice}:180"),
                    InlineKeyboardButton("5 min", callback_data=f"orma_pgsset:{captura_id}:{indice}:300"),
                ],
                [
                    InlineKeyboardButton("10 min", callback_data=f"orma_pgsset:{captura_id}:{indice}:600"),
                    InlineKeyboardButton("30 min", callback_data=f"orma_pgsset:{captura_id}:{indice}:1800"),
                    InlineKeyboardButton("1 h", callback_data=f"orma_pgsset:{captura_id}:{indice}:3600"),
                ],
                [InlineKeyboardButton("✍️ MANUAL", callback_data=f"orma_pgin:{captura_id}:{indice}:separacion_minutos")],
                [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_pg:{captura_id}:{indice}")],
            ]),
        )
        return

    if data.startswith("orma_pgsset:"):
        _, captura_txt, indice_txt, segundos_txt = data.split(":", 3)
        captura_id = int(captura_txt)
        indice = int(indice_txt)
        segundos = int(segundos_txt)
        captura = obtener_captura_orma(captura_id)
        grupo = await resolver_chat_grupo_orma(context.bot, indice)
        if not captura_pertenece_propietario(captura, usuario.id) or not grupo:
            await query.answer("Ficha no disponible.", show_alert=True)
            return

        cfg_actual = obtener_control_grupo_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            grupo["chat_id"],
            crear=True,
        )
        if str(cfg_actual["modo"]).upper() == "HEREDADO":
            copiar_global_a_grupo_db(
                captura["objetivo_tipo"],
                captura["objetivo_id"],
                grupo["chat_id"],
                chat_username=grupo["username"],
                chat_nombre=grupo["nombre"],
            )

        actualizar_control_grupo_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            grupo["chat_id"],
            modo="PERSONALIZADO",
            separacion_segundos=None if segundos == 0 else segundos,
        )
        registrar_auditoria_orma(
            usuario.id, captura, "PUBLICIDAD_SEPARACION_GRUPO",
            grupo=grupo,
            detalle="SIN SEPARACIÓN" if segundos == 0 else texto_separacion(segundos),
        )
        await query.answer("Separación actualizada")
        cfg = obtener_control_grupo_db(
            captura["objetivo_tipo"], captura["objetivo_id"], grupo["chat_id"]
        )
        await safe_query_edit_message(
            query,
            await texto_control_publicidad_grupo(captura, grupo, cfg),
            parse_mode="HTML",
            reply_markup=teclado_publicidad_grupo(captura_id, indice, cfg),
        )
        return

    if data.startswith("orma_pgl:"):
        _, captura_txt, indice_txt = data.split(":", 2)
        captura_id = int(captura_txt)
        indice = int(indice_txt)
        captura = obtener_captura_orma(captura_id)
        grupo = await resolver_chat_grupo_orma(context.bot, indice)
        if not captura_pertenece_propietario(captura, usuario.id) or not grupo:
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        cfg = obtener_control_grupo_db(
            captura["objetivo_tipo"], captura["objetivo_id"], grupo["chat_id"], crear=True
        )
        efectivo = (
            obtener_control_identidad_db(captura["objetivo_tipo"], captura["objetivo_id"])
            if str(cfg["modo"]).upper() == "HEREDADO"
            else cfg
        )
        await query.answer()
        await safe_query_edit_message(
            query,
            "🔢 <b>LÍMITES · ESTE GRUPO</b>\n\n"
            f"Hora: <b>{texto_valor_limite(efectivo['limite_hora'])}</b>\n"
            f"Día: <b>{texto_valor_limite(efectivo['limite_dia'])}</b>\n"
            f"Semana: <b>{texto_valor_limite(efectivo['limite_semana'])}</b>\n"
            f"Mes: <b>{texto_valor_limite(efectivo['limite_mes'])}</b>\n"
            f"Año: <b>{texto_valor_limite(efectivo['limite_anio'])}</b>\n\n"
            "Selecciona el periodo y escribe el valor manual. 0 significa "
            "cero publicaciones permitidas en ese periodo.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("HORA", callback_data=f"orma_pgin:{captura_id}:{indice}:limite_hora"),
                    InlineKeyboardButton("DÍA", callback_data=f"orma_pgin:{captura_id}:{indice}:limite_dia"),
                ],
                [
                    InlineKeyboardButton("SEMANA", callback_data=f"orma_pgin:{captura_id}:{indice}:limite_semana"),
                    InlineKeyboardButton("MES", callback_data=f"orma_pgin:{captura_id}:{indice}:limite_mes"),
                ],
                [InlineKeyboardButton("AÑO", callback_data=f"orma_pgin:{captura_id}:{indice}:limite_anio")],
                [InlineKeyboardButton("♾ SIN LÍMITES", callback_data=f"orma_pgnolim:{captura_id}:{indice}")],
                [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_pg:{captura_id}:{indice}")],
            ]),
        )
        return

    if data.startswith("orma_pgnolim:"):
        _, captura_txt, indice_txt = data.split(":", 2)
        captura_id = int(captura_txt)
        indice = int(indice_txt)
        captura = obtener_captura_orma(captura_id)
        grupo = await resolver_chat_grupo_orma(context.bot, indice)
        if not captura_pertenece_propietario(captura, usuario.id) or not grupo:
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        actualizar_control_grupo_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            grupo["chat_id"],
            modo="PERSONALIZADO",
            limite_hora=None,
            limite_dia=None,
            limite_semana=None,
            limite_mes=None,
            limite_anio=None,
        )
        await query.answer("Límites eliminados en este grupo")
        cfg = obtener_control_grupo_db(
            captura["objetivo_tipo"], captura["objetivo_id"], grupo["chat_id"]
        )
        await safe_query_edit_message(
            query,
            await texto_control_publicidad_grupo(captura, grupo, cfg),
            parse_mode="HTML",
            reply_markup=teclado_publicidad_grupo(captura_id, indice, cfg),
        )
        return

    if data.startswith("orma_pgt:"):
        _, captura_txt, indice_txt = data.split(":", 2)
        captura_id = int(captura_txt)
        indice = int(indice_txt)
        captura = obtener_captura_orma(captura_id)
        grupo = await resolver_chat_grupo_orma(context.bot, indice)
        if not captura_pertenece_propietario(captura, usuario.id) or not grupo:
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        cfg = obtener_control_grupo_db(
            captura["objetivo_tipo"], captura["objetivo_id"], grupo["chat_id"], crear=True
        )
        efectivo = (
            obtener_control_identidad_db(captura["objetivo_tipo"], captura["objetivo_id"])
            if str(cfg["modo"]).upper() == "HEREDADO"
            else cfg
        )
        def marca(campo):
            return "✅" if bool(efectivo[campo]) else "❌"
        await query.answer()
        await safe_query_edit_message(
            query,
            "🎛 <b>TIPOS · ESTE GRUPO</b>\n\n"
            "✅ entra al control · ❌ queda libre.\n"
            "Texto normal puro siempre libre.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"{marca('controlar_foto')} FOTO", callback_data=f"orma_pgtog:{captura_id}:{indice}:controlar_foto"),
                    InlineKeyboardButton(f"{marca('controlar_video')} VIDEO", callback_data=f"orma_pgtog:{captura_id}:{indice}:controlar_video"),
                ],
                [
                    InlineKeyboardButton(f"{marca('controlar_gif')} GIF", callback_data=f"orma_pgtog:{captura_id}:{indice}:controlar_gif"),
                    InlineKeyboardButton(f"{marca('controlar_documento')} DOCUMENTO", callback_data=f"orma_pgtog:{captura_id}:{indice}:controlar_documento"),
                ],
                [InlineKeyboardButton(f"{marca('controlar_enlace')} ENLACE", callback_data=f"orma_pgtog:{captura_id}:{indice}:controlar_enlace")],
                [InlineKeyboardButton(f"{marca('controlar_custom_emoji')} PREMIUM EMOJI", callback_data=f"orma_pgtog:{captura_id}:{indice}:controlar_custom_emoji")],
                [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_pg:{captura_id}:{indice}")],
            ]),
        )
        return

    if data.startswith("orma_pgtog:"):
        _, captura_txt, indice_txt, campo = data.split(":", 3)
        captura_id = int(captura_txt)
        indice = int(indice_txt)
        captura = obtener_captura_orma(captura_id)
        grupo = await resolver_chat_grupo_orma(context.bot, indice)
        if not captura_pertenece_propietario(captura, usuario.id) or not grupo:
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        cfg = obtener_control_grupo_db(
            captura["objetivo_tipo"], captura["objetivo_id"], grupo["chat_id"], crear=True
        )
        if str(cfg["modo"]).upper() == "HEREDADO":
            cfg = copiar_global_a_grupo_db(
                captura["objetivo_tipo"],
                captura["objetivo_id"],
                grupo["chat_id"],
                chat_username=grupo["username"],
                chat_nombre=grupo["nombre"],
            )
        nuevo = 0 if bool(cfg[campo]) else 1
        actualizar_control_grupo_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            grupo["chat_id"],
            modo="PERSONALIZADO",
            **{campo: nuevo},
        )
        await query.answer("Tipo actualizado")
        # Regresa al panel de tipos.
        cfg = obtener_control_grupo_db(
            captura["objetivo_tipo"], captura["objetivo_id"], grupo["chat_id"]
        )
        def marca2(c):
            return "✅" if bool(cfg[c]) else "❌"
        await safe_query_edit_message(
            query,
            "🎛 <b>TIPOS · ESTE GRUPO</b>\n\n"
            "✅ entra al control · ❌ queda libre.\n"
            "Texto normal puro siempre libre.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"{marca2('controlar_foto')} FOTO", callback_data=f"orma_pgtog:{captura_id}:{indice}:controlar_foto"),
                    InlineKeyboardButton(f"{marca2('controlar_video')} VIDEO", callback_data=f"orma_pgtog:{captura_id}:{indice}:controlar_video"),
                ],
                [
                    InlineKeyboardButton(f"{marca2('controlar_gif')} GIF", callback_data=f"orma_pgtog:{captura_id}:{indice}:controlar_gif"),
                    InlineKeyboardButton(f"{marca2('controlar_documento')} DOCUMENTO", callback_data=f"orma_pgtog:{captura_id}:{indice}:controlar_documento"),
                ],
                [InlineKeyboardButton(f"{marca2('controlar_enlace')} ENLACE", callback_data=f"orma_pgtog:{captura_id}:{indice}:controlar_enlace")],
                [InlineKeyboardButton(f"{marca2('controlar_custom_emoji')} PREMIUM EMOJI", callback_data=f"orma_pgtog:{captura_id}:{indice}:controlar_custom_emoji")],
                [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_pg:{captura_id}:{indice}")],
            ]),
        )
        return

    if data.startswith("orma_pgr:"):
        _, captura_txt, indice_txt = data.split(":", 2)
        captura_id = int(captura_txt)
        indice = int(indice_txt)
        captura = obtener_captura_orma(captura_id)
        grupo = await resolver_chat_grupo_orma(context.bot, indice)
        if not captura_pertenece_propietario(captura, usuario.id) or not grupo:
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        borrar_control_grupo_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            grupo["chat_id"],
        )
        registrar_auditoria_orma(
            usuario.id, captura, "PUBLICIDAD_RESTAURAR_HEREDADO",
            grupo=grupo, detalle="HEREDADO GLOBAL",
        )
        await query.answer("Este grupo vuelve a HEREDADO")
        cfg = obtener_control_grupo_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            grupo["chat_id"],
            chat_username=grupo["username"],
            chat_nombre=grupo["nombre"],
            crear=True,
        )
        await safe_query_edit_message(
            query,
            await texto_control_publicidad_grupo(captura, grupo, cfg),
            parse_mode="HTML",
            reply_markup=teclado_publicidad_grupo(captura_id, indice, cfg),
        )
        return

    if data.startswith("orma_pgin:"):
        _, captura_txt, indice_txt, campo = data.split(":", 3)
        captura_id = int(captura_txt)
        indice = int(indice_txt)
        captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        ENTRADAS_ORMA_TOTAL[usuario.id] = {
            "tipo": "PUBLICIDAD_GRUPO",
            "captura_id": captura_id,
            "indice": indice,
            "campo": campo,
        }
        await query.answer()
        etiqueta = (
            "minutos de separación"
            if campo == "separacion_minutos"
            else campo.replace("limite_", "límite ").replace("_", " ")
        )
        await safe_query_edit_message(
            query,
            "✍️ <b>VALOR MANUAL · ESTE GRUPO</b>\n\n"
            f"Escribe ahora <b>{html.escape(etiqueta)}</b>.\n"
            "Debe ser un número entero igual o mayor que 0.\n\n"
            "Tu mensaje será eliminado automáticamente.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ CANCELAR", callback_data=f"orma_pg:{captura_id}:{indice}")]
            ]),
        )
        return

    if data.startswith("orma_mod:"):
        captura_id = int(data.split(":", 1)[1])
        captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        await query.answer()

        if captura["objetivo_tipo"] not in {"USUARIO", "BOT"}:
            await safe_query_edit_message(
                query,
                "🛡️ <b>CONTROL TOTAL · MODERACIÓN</b>\n\n"
                + cabecera_identidad_orma(captura)
                + "\n\n⚠️ Las acciones de miembro (mute/ban/expulsión) "
                  "solo aplican a identidades USUARIO/BOT.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_ficha:{captura_id}")]
                ]),
            )
            return

        rol = await obtener_rol_en_grupo(
            captura["chat_id"],
            captura["objetivo_id"],
        )
        await safe_query_edit_message(
            query,
            "🛡️ <b>CONTROL TOTAL · MODERACIÓN 7/7</b>\n\n"
            + cabecera_identidad_orma(captura, rol=rol)
            + "\n\nSelecciona una acción. Después podrás elegir "
              "un grupo, varios grupos o los 7.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔇 MUTEAR", callback_data=f"orma_modacc:{captura_id}:MUTE"),
                    InlineKeyboardButton("🔊 DESMUTEAR", callback_data=f"orma_modacc:{captura_id}:UNMUTE"),
                ],
                [
                    InlineKeyboardButton("👢 EXPULSAR", callback_data=f"orma_modacc:{captura_id}:EXPULSAR"),
                    InlineKeyboardButton("🚫 BANEAR", callback_data=f"orma_modacc:{captura_id}:BAN"),
                ],
                [InlineKeyboardButton("♻️ DESBANEAR", callback_data=f"orma_modacc:{captura_id}:UNBAN")],
                [InlineKeyboardButton("⬅️ RETROCEDER", callback_data=f"orma_ficha:{captura_id}")],
                [
                    InlineKeyboardButton("🏠 MENÚ PRINCIPAL", callback_data="orma_menu_principal"),
                    InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar"),
                ],
            ]),
        )
        return

    if data.startswith("orma_modacc:"):
        _, captura_txt, accion = data.split(":", 2)
        captura_id = int(captura_txt)
        captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        estado = seleccion_moderacion_orma(usuario.id, captura_id, accion)
        # Por comodidad seleccionamos de inicio el grupo desde donde nació /orma.
        indice_origen = indice_grupo_orma_por_username(captura["chat_username"])
        if indice_origen and not estado["grupos"]:
            estado["grupos"].add(indice_origen)
        await query.answer()
        await safe_query_edit_message(
            query,
            f"{texto_accion_moderacion(accion)} · <b>SELECCIONAR GRUPOS</b>\n\n"
            + cabecera_identidad_orma(captura)
            + "\n\nSelecciona uno, varios o TODOS 7/7.",
            parse_mode="HTML",
            reply_markup=teclado_seleccion_moderacion(
                captura_id,
                accion,
                estado["grupos"],
            ),
        )
        return

    if data.startswith("orma_modtog:"):
        _, captura_txt, accion, indice_txt = data.split(":", 3)
        captura_id = int(captura_txt)
        indice = int(indice_txt)
        estado = seleccion_moderacion_orma(usuario.id, captura_id, accion)
        if indice in estado["grupos"]:
            estado["grupos"].remove(indice)
        else:
            estado["grupos"].add(indice)
        await query.answer()
        captura = obtener_captura_orma(captura_id)
        await safe_query_edit_message(
            query,
            f"{texto_accion_moderacion(accion)} · <b>SELECCIONAR GRUPOS</b>\n\n"
            + cabecera_identidad_orma(captura)
            + f"\n\nSeleccionados: <b>{len(estado['grupos'])}</b>",
            parse_mode="HTML",
            reply_markup=teclado_seleccion_moderacion(
                captura_id, accion, estado["grupos"]
            ),
        )
        return

    if data.startswith("orma_modall:"):
        _, captura_txt, accion = data.split(":", 2)
        captura_id = int(captura_txt)
        estado = seleccion_moderacion_orma(usuario.id, captura_id, accion)
        estado["grupos"] = set(range(1, 8))
        await query.answer("Seleccionados 7/7")
        captura = obtener_captura_orma(captura_id)
        await safe_query_edit_message(
            query,
            f"{texto_accion_moderacion(accion)} · <b>SELECCIONAR GRUPOS</b>\n\n"
            + cabecera_identidad_orma(captura)
            + "\n\nSeleccionados: <b>7/7</b>",
            parse_mode="HTML",
            reply_markup=teclado_seleccion_moderacion(
                captura_id, accion, estado["grupos"]
            ),
        )
        return

    if data.startswith("orma_modnone:"):
        _, captura_txt, accion = data.split(":", 2)
        captura_id = int(captura_txt)
        estado = seleccion_moderacion_orma(usuario.id, captura_id, accion)
        estado["grupos"] = set()
        await query.answer("Selección vacía")
        captura = obtener_captura_orma(captura_id)
        await safe_query_edit_message(
            query,
            f"{texto_accion_moderacion(accion)} · <b>SELECCIONAR GRUPOS</b>\n\n"
            + cabecera_identidad_orma(captura)
            + "\n\nSeleccionados: <b>0</b>",
            parse_mode="HTML",
            reply_markup=teclado_seleccion_moderacion(
                captura_id, accion, estado["grupos"]
            ),
        )
        return

    if data.startswith("orma_modnext:"):
        _, captura_txt, accion = data.split(":", 2)
        captura_id = int(captura_txt)
        estado = seleccion_moderacion_orma(usuario.id, captura_id, accion)
        captura = obtener_captura_orma(captura_id)
        if not estado["grupos"]:
            await query.answer("Selecciona al menos un grupo.", show_alert=True)
            return
        await query.answer()

        if accion == "MUTE":
            await safe_query_edit_message(
                query,
                "🔇 <b>DURACIÓN DEL MUTE</b>\n\n"
                + cabecera_identidad_orma(captura)
                + f"\n\nGrupos seleccionados: <b>{len(estado['grupos'])}</b>\n"
                  "Elige un tiempo o introdúcelo manualmente.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("10 min", callback_data=f"orma_moddur:{captura_id}:600"),
                        InlineKeyboardButton("30 min", callback_data=f"orma_moddur:{captura_id}:1800"),
                    ],
                    [
                        InlineKeyboardButton("1 h", callback_data=f"orma_moddur:{captura_id}:3600"),
                        InlineKeyboardButton("6 h", callback_data=f"orma_moddur:{captura_id}:21600"),
                    ],
                    [
                        InlineKeyboardButton("24 h", callback_data=f"orma_moddur:{captura_id}:86400"),
                        InlineKeyboardButton("7 días", callback_data=f"orma_moddur:{captura_id}:604800"),
                    ],
                    [InlineKeyboardButton("♾ PERMANENTE", callback_data=f"orma_moddur:{captura_id}:perm")],
                    [InlineKeyboardButton("✍️ MINUTOS MANUALES", callback_data=f"orma_moddur:{captura_id}:manual")],
                    [InlineKeyboardButton("⬅️ GRUPOS", callback_data=f"orma_modacc:{captura_id}:MUTE")],
                ]),
            )
            return

        await safe_query_edit_message(
            query,
            f"⚠️ <b>CONFIRMAR {texto_accion_moderacion(accion)}</b>\n\n"
            + cabecera_identidad_orma(captura)
            + f"\n\nGrupos: <b>{len(estado['grupos'])}</b>\n"
              "La operación se ejecutará grupo por grupo y mostrará "
              "cuáles tuvieron éxito o fallo.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ CONFIRMAR", callback_data=f"orma_modgo:{captura_id}:{accion}")],
                [InlineKeyboardButton("❌ CANCELAR", callback_data=f"orma_mod:{captura_id}")],
            ]),
        )
        return

    if data.startswith("orma_moddur:"):
        _, captura_txt, valor = data.split(":", 2)
        captura_id = int(captura_txt)
        estado = seleccion_moderacion_orma(usuario.id, captura_id, "MUTE")
        captura = obtener_captura_orma(captura_id)
        if valor == "manual":
            ENTRADAS_ORMA_TOTAL[usuario.id] = {
                "tipo": "MUTE_MANUAL",
                "captura_id": captura_id,
            }
            await query.answer()
            await safe_query_edit_message(
                query,
                "✍️ <b>MUTE · TIEMPO MANUAL</b>\n\n"
                + cabecera_identidad_orma(captura)
                + "\n\nEscribe la cantidad de <b>minutos</b>.\n"
                  "Tu mensaje será eliminado automáticamente.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ CANCELAR", callback_data=f"orma_mod:{captura_id}")]
                ]),
            )
            return

        estado["duracion_segundos"] = None if valor == "perm" else int(valor)
        detalle = (
            "PERMANENTE"
            if valor == "perm"
            else texto_separacion(int(valor))
        )
        await query.answer()
        await safe_query_edit_message(
            query,
            "⚠️ <b>CONFIRMAR MUTE</b>\n\n"
            + cabecera_identidad_orma(captura)
            + f"\n\nGrupos: <b>{len(estado['grupos'])}</b>\n"
              f"Duración: <b>{detalle}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ CONFIRMAR", callback_data=f"orma_modgo:{captura_id}:MUTE")],
                [InlineKeyboardButton("❌ CANCELAR", callback_data=f"orma_mod:{captura_id}")],
            ]),
        )
        return

    if data.startswith("orma_modgo:"):
        _, captura_txt, accion = data.split(":", 2)
        captura_id = int(captura_txt)
        captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            await query.answer("Ficha no disponible.", show_alert=True)
            return
        estado = seleccion_moderacion_orma(usuario.id, captura_id, accion)
        if not estado["grupos"]:
            await query.answer("No hay grupos seleccionados.", show_alert=True)
            return

        await query.answer("Ejecutando...")
        await safe_query_edit_message(
            query,
            f"⏳ <b>EJECUTANDO {texto_accion_moderacion(accion)}</b>\n\n"
            f"Objetivo: <code>{captura['objetivo_id']}</code>\n"
            f"Grupos: <b>{len(estado['grupos'])}</b>\n\n"
            "Procesando secuencialmente...",
            parse_mode="HTML",
        )

        resultados = await ejecutar_moderacion_seleccion_orma(
            context.bot,
            usuario.id,
            captura,
            accion,
            estado["grupos"],
            duracion_segundos=estado.get("duracion_segundos"),
        )

        await safe_query_edit_message(
            query,
            f"🛡️ <b>RESULTADO · {texto_accion_moderacion(accion)}</b>\n\n"
            + cabecera_identidad_orma(captura)
            + "\n\n"
            + texto_resultados_moderacion(resultados),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 REINTENTAR / MODERAR", callback_data=f"orma_mod:{captura_id}")],
                [InlineKeyboardButton("📜 AUDITORÍA", callback_data=f"orma_audit:{captura_id}")],
                [InlineKeyboardButton("⬅️ FICHA", callback_data=f"orma_ficha:{captura_id}")],
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
        await safe_query_edit_message(query,
            await texto_control_publicidad(captura),
            parse_mode="HTML",
            reply_markup=teclado_control_publicidad(captura_id, cfg),
        )
        return

    if data.startswith("orma_pub_modo:"):
        captura_id = int(data.split(":", 1)[1])
        await query.answer()
        await safe_query_edit_message(query,
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
        await safe_query_edit_message(query,
            await texto_control_publicidad(captura),
            parse_mode="HTML",
            reply_markup=teclado_control_publicidad(captura_id, cfg),
        )
        return

    if data.startswith("orma_pub_sep:"):
        captura_id = int(data.split(":", 1)[1])
        await query.answer()
        await safe_query_edit_message(query,
            "⏱ <b>SEPARACIÓN ENTRE PUBLICIDADES</b>\\n\\n"
            "Selecciona el tiempo mínimo entre una publicidad permitida "
            "y la siguiente.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "SIN SEPARACIÓN",
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
        await query.answer("Separación actualizada")
        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )
        await safe_query_edit_message(query,
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
        await safe_query_edit_message(query,
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
        await safe_query_edit_message(query,
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

        await safe_query_edit_message(query,
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

        # Reabrir submenú tipos.
        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )

        def marca(c):
            return "✅" if bool(cfg[c]) else "❌"

        await safe_query_edit_message(query,
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
        await safe_query_edit_message(query,
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

        await safe_query_edit_message(query,
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
        await safe_query_edit_message(query,
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
        chat=chat,
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
    chat = update.effective_chat

    if not mensaje or not usuario or not chat:
        return

    # En grupos, los comandos operativos no generan respuestas públicas.
    if chat.type != ChatType.PRIVATE:
        if comando_dirigido_a_maximo(mensaje):
            try:
                await mensaje.delete()
            except TelegramError:
                pass
        return

    try:
        await mensaje.delete()
    except TelegramError:
        pass

    if not es_administrador_maximo(usuario):
        return

    registrar_usuario_membresia(usuario)

    texto = (
        "🦍 <b>MÁXIMO CONTROL TOTAL</b>\n\n"
        "Centro privado de administración.\n\n"
        "📌 Responde cualquier mensaje en cualquiera de los grupos "
        "controlados con <code>/orma</code> para abrir su expediente.\n\n"
        "🧹 Los comandos y datos operativos se eliminan "
        "automáticamente para mantener el panel limpio."
    )
    teclado = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar")
    ]])

    panel_id = PANELES_ORMA.get(usuario.id) or obtener_panel_orma_db(usuario.id)
    if panel_id:
        try:
            await safe_edit_message_text(
                context.bot,
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
    usuario = update.effective_user
    chat = update.effective_chat

    if not mensaje or not usuario or not chat:
        return

    # Nunca exponer estado operativo en grupos.
    if chat.type != ChatType.PRIVATE:
        if comando_dirigido_a_maximo(mensaje):
            try:
                await mensaje.delete()
            except TelegramError:
                pass
        return

    try:
        await mensaje.delete()
    except TelegramError:
        pass

    if not es_administrador_maximo(usuario):
        return

    hora_peru = datetime.now(ZONA_PERU).strftime("%d/%m/%Y %H:%M:%S")
    respuesta = await context.bot.send_message(
        chat_id=usuario.id,
        text=(
            "✅ <b>MaximoControlGroup operativo</b>\n"
            "🔐 Panel administrativo privado\n"
            f"🇵🇪 Hora Perú: <b>{hora_peru}</b>"
        ),
        parse_mode="HTML",
    )
    asyncio.create_task(eliminar_mensaje_despues(respuesta, 30))


async def limpiar_comandos_maximo_en_grupos(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Elimina silenciosamente comandos dirigidos a @MaximoControlGroup_bot
    dentro de grupos. No responde ni revela datos operativos.
    """
    mensaje = update.effective_message
    chat = update.effective_chat

    if (
        not mensaje
        or not chat
        or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}
        or not es_grupo_controlado(chat)
        or not comando_dirigido_a_maximo(mensaje)
    ):
        return

    try:
        await mensaje.delete()
    except TelegramError:
        pass


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
        # también aparece durante 1 minuto según la regla 7/7 definida.
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

    tarea = TAREAS_AVISOS_MEMBRESIA.pop(clave, None)
    if tarea and not tarea.done():
        tarea.cancel()


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

    entrada_total = ENTRADAS_ORMA_TOTAL.get(usuario.id)
    if entrada_total:
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
            return

        captura = obtener_captura_orma(entrada_total["captura_id"])
        if not captura or captura["propietario_id"] != usuario.id:
            ENTRADAS_ORMA_TOTAL.pop(usuario.id, None)
            return

        if entrada_total["tipo"] == "PUBLICIDAD_GRUPO":
            indice = int(entrada_total["indice"])
            campo = entrada_total["campo"]
            grupo = await resolver_chat_grupo_orma(context.bot, indice)
            if not grupo or grupo["chat_id"] is None:
                ENTRADAS_ORMA_TOTAL.pop(usuario.id, None)
                return

            cfg = obtener_control_grupo_db(
                captura["objetivo_tipo"],
                captura["objetivo_id"],
                grupo["chat_id"],
                crear=True,
            )
            if str(cfg["modo"]).upper() == "HEREDADO":
                copiar_global_a_grupo_db(
                    captura["objetivo_tipo"],
                    captura["objetivo_id"],
                    grupo["chat_id"],
                    chat_username=grupo["username"],
                    chat_nombre=grupo["nombre"],
                )

            cambios = {}
            if campo == "separacion_minutos":
                cambios["separacion_segundos"] = None if valor == 0 else valor * 60
            else:
                cambios[campo] = valor

            actualizar_control_grupo_db(
                captura["objetivo_tipo"],
                captura["objetivo_id"],
                grupo["chat_id"],
                modo="PERSONALIZADO",
                **cambios,
            )
            registrar_auditoria_orma(
                usuario.id,
                captura,
                "PUBLICIDAD_VALOR_MANUAL_GRUPO",
                grupo=grupo,
                detalle=f"{campo}={valor}",
            )
            ENTRADAS_ORMA_TOTAL.pop(usuario.id, None)

            cfg = obtener_control_grupo_db(
                captura["objetivo_tipo"],
                captura["objetivo_id"],
                grupo["chat_id"],
            )
            panel_id = PANELES_ORMA.get(usuario.id) or obtener_panel_orma_db(usuario.id)
            if panel_id:
                await safe_edit_message_text(
                    context.bot,
                    chat_id=usuario.id,
                    message_id=panel_id,
                    text=await texto_control_publicidad_grupo(captura, grupo, cfg),
                    parse_mode="HTML",
                    reply_markup=teclado_publicidad_grupo(
                        captura["id"],
                        indice,
                        cfg,
                    ),
                )
            return

        if entrada_total["tipo"] == "MUTE_MANUAL":
            if valor <= 0:
                return
            estado = seleccion_moderacion_orma(
                usuario.id,
                captura["id"],
                "MUTE",
            )
            estado["duracion_segundos"] = valor * 60
            ENTRADAS_ORMA_TOTAL.pop(usuario.id, None)

            panel_id = PANELES_ORMA.get(usuario.id) or obtener_panel_orma_db(usuario.id)
            if panel_id:
                await safe_edit_message_text(
                    context.bot,
                    chat_id=usuario.id,
                    message_id=panel_id,
                    text=(
                        "⚠️ <b>CONFIRMAR MUTE</b>\n\n"
                        + cabecera_identidad_orma(captura)
                        + f"\n\nGrupos: <b>{len(estado['grupos'])}</b>\n"
                          f"Duración: <b>{valor} minutos</b>"
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            "✅ CONFIRMAR",
                            callback_data=f"orma_modgo:{captura['id']}:MUTE",
                        )],
                        [InlineKeyboardButton(
                            "❌ CANCELAR",
                            callback_data=f"orma_mod:{captura['id']}",
                        )],
                    ]),
                )
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
                await safe_edit_message_text(context.bot,
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
            await safe_edit_message_text(context.bot,
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
    await update.effective_message.reply_text('DIAGNOSTICO VERSION f37437b')


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
    registrar_verificacion_membresia_db(usuario.id, estado)

    try:
        await safe_query_edit_message(query,
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
        text=f"🆔 Tu ID de Telegram es: <code>{usuario.id}</code>",
        parse_mode="HTML",
    )
    asyncio.create_task(eliminar_mensaje_despues(respuesta, 60))


async def mostrar_monitoreo_union(bot, admin_id):
    panel_id = obtener_panel_union_admin_db(admin_id)
    texto = texto_monitoreo_union()
    teclado = teclado_monitoreo_union()

    if panel_id:
        try:
            await safe_edit_message_text(bot,
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
    iniciar_agente_respaldo()

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
            filters.ChatType.GROUPS & filters.COMMAND,
            limpiar_comandos_maximo_en_grupos,
        ),
        group=5,
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


