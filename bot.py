import os
import json
import asyncio
import logging
import sqlite3
import html
import hashlib
import re
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from agente_respaldo_remoto import iniciar_agente_respaldo
from bienvenidas import WelcomeService
from registro21 import Registry21
from pertenencia_consulta import MembershipService, bind_service

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ApplicationHandlerStop,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def paginas_html_panel(texto, limite=3600):
    """Divide HTML sin cortar etiquetas ni entidades; cierra y reabre formato."""
    paginas, partes, abiertas = [], [], []
    longitud = 0
    def unidades(valor):
        return len(valor.encode("utf-16-le")) // 2
    def cerrar():
        return "".join(f"</{nombre}>" for nombre, _ in reversed(abiertas))
    def volcar():
        nonlocal partes, longitud
        paginas.append("".join(partes) + cerrar())
        partes = [etiqueta for _, etiqueta in abiertas]
        longitud = sum(unidades(x) for x in partes)
    for token in re.findall(r"<[^>]*>|&(?:#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z]+);|[^<&]+|[<&]", str(texto)):
        etiqueta = re.fullmatch(r"<(/?)([a-zA-Z0-9-]+)(?:\s[^>]*)?>", token)
        if etiqueta:
            es_cierre, nombre = etiqueta.group(1), etiqueta.group(2)
            if not es_cierre and longitud + unidades(token) + unidades(cerrar()) + unidades(f"</{nombre}>") > limite:
                volcar()
            partes.append(token); longitud += unidades(token)
            if es_cierre:
                if abiertas and abiertas[-1][0] == nombre:
                    abiertas.pop()
            else:
                abiertas.append((nombre, token))
        else:
            elementos = [token] if token.startswith("&") and token.endswith(";") else token
            for elemento in elementos:
                if longitud + unidades(elemento) + unidades(cerrar()) > limite:
                    volcar()
                partes.append(elemento); longitud += unidades(elemento)
    if partes:
        paginas.append("".join(partes) + cerrar())
    return paginas or [""]


def texto_panel_seguro(texto, parse_mode):
    if str(parse_mode or "").upper() != "HTML":
        return texto
    paginas = paginas_html_panel(texto)
    if len(paginas) == 1:
        return paginas[0]
    return paginas[0] + "\n\n<i>Resumen abreviado. Usa los paneles de detalle para consultar el resto.</i>"


def completar_navegacion_panel(markup):
    if markup is None:
        return None
    filas = [list(fila) for fila in markup.inline_keyboard]
    callbacks = [str(b.callback_data or "") for fila in filas for b in fila]
    if not any(x.startswith("orma_") for x in callbacks):
        return markup
    ultima = []
    if "orma_menu_principal" not in callbacks:
        ultima.append(InlineKeyboardButton("🏠 MENÚ PRINCIPAL", callback_data="orma_menu_principal"))
    if "orma_cerrar" not in callbacks:
        ultima.append(InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar"))
    if ultima:
        filas.append(ultima)
    return InlineKeyboardMarkup(filas)


def registrar_panel_reemplazo(chat_id, mensaje):
    if int(chat_id) > 0:
        PANELES_ORMA[int(chat_id)] = mensaje.message_id
        guardar_panel_orma_db(int(chat_id), mensaje.message_id)
    return mensaje


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
    reply_markup = completar_navegacion_panel(reply_markup)
    text = texto_panel_seguro(text, parse_mode)
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
        if "message is not modified" in detalle:
            return None
        if (
            "message to edit not found" in detalle
            or "message can't be edited" in detalle
        ):
            nuevo = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                **kwargs,
            )
            if reply_markup and any(str(b.callback_data or "").startswith("orma_") for fila in reply_markup.inline_keyboard for b in fila):
                registrar_panel_reemplazo(chat_id, nuevo)
            return nuevo
        raise


async def safe_query_edit_message(
    query,
    text,
    parse_mode=None,
    reply_markup=None,
    **kwargs,
):
    reply_markup = completar_navegacion_panel(reply_markup)
    text = texto_panel_seguro(text, parse_mode)
    try:
        return await query.edit_message_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            **kwargs,
        )
    except TelegramError as error:
        detalle = str(error).lower()
        if "message is not modified" in detalle:
            return None
        if (
            "message to edit not found" in detalle
            or "message can't be edited" in detalle
        ):
            nuevo = await query.get_bot().send_message(
                chat_id=query.message.chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                **kwargs,
            )
            registrar_panel_reemplazo(query.message.chat_id, nuevo)
            return nuevo
        raise

BOT_TOKEN = os.environ["BOT_TOKEN"]
UNION_BOT_TOKEN = os.environ["UNION_BOT_TOKEN"]
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0") or 0)
GROUP_ANONYMOUS_BOT_ID = 1087968824
ORMA_ADMIN_USER_ID = int(os.environ.get("ORMA_ADMIN_USER_ID", "7615865943") or 0)

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
DATABASE_PATH = os.path.join(DATA_DIR, "maximo_control.db")

TOTAL_GRUPOS_OBLIGATORIOS = 21
TOTAL_GRUPOS_HISTORICOS = 12
TOTAL_GRUPOS_UNION = 21  # Panel y moderación
UNION_AUTO_INTERVALO_SEGUNDOS = 30  # Una cuenta por ciclo, limita consultas a Telegram
UNION_AUTO_REVISION_SEGUNDOS = 600  # No repetir el barrido de un usuario antes de 10 minutos
UNION_AUTO_TAREA = None
UNION_AUTO_ULTIMA_REVISION = {}
UNION_AUTO_BLOQUEOS = {}
AVISO_MEMBRESIA_SEGUNDOS = 60
UNION_BOT_USERNAME = "UnionMembresia_bot"
GRUPO_PRUEBAS_USERNAME = "Orma_Pruebas"
ZONA_PERU = ZoneInfo("America/Lima")
GRUPO_PUBLICIDAD_NO_IDENTIFICADA_ID = -1004406356313
GRUPO_PUBLICACIONES_USUARIOS_ID = -1004410994552

GRUPOS_OFICIALES = [
    (1, "DISTRITO STREAMING UNIVERSAL 🌎🌍", "DistritoStreamingUniversal", "DistritoStreamingUniversal_Bot"),
    (2, "STREAMING DIGITAL PERUCHO 🇵🇪", "StreamingDigitalPerucho", "StreamingDigitalPerucho_bot"),
    (3, "PERÚ ENTRETENIMIENTO STREAMING 🇵🇪", "PeruEntretenimientoStreaming", "PeruEntretenimientoStreaming_Bot"),
    (4, "MUNDO CACHINERO STREAMING 🌎", "MundoCachineroStreaming", "MUCASTBOT"),
    (5, "🌎 UNIVERSO CIBERNÉTICO PERÚ 🇵🇪", "mundocibertetico", "UniversoCibertneticoPeru_bot"),
    (6, "💻 Metaverso Streaming Perú 🇵🇪", "metaversostreaminggo", "MetaversoPeru_bot"),
    (7, "🎭 MUNDO STREAMING PERÚ 🇵🇪", "mymundostreaming", "MundoStreamingPeru_bot"),
    (8, "👑 Club Premium Latino Digital", "Club_Premium_Latino_Digital", "Club_Premium_Latino_bot"),
    (9, "🎬 Mundo Play | Entretenimiento Digital", "Mundo_Play_E_Digital", "Mundo_Play_E_Digital_bot"),
    (10, "⚡ CONECTA2 STREAMING & SERVICIOS", "Conectados_Streaming_Servicios", "Conecta2_Streaming_bot"),
    (11, "🌎 Imperio Digital | Streaming & Servicios", "Imperio_Digital_Streaming", "Imperio_Digital_Streaming_bot"),
    (12, "💠 The Digital Club | Perú & Latam", "The_Digital_Club_Peru_Latam", "The_Digital_Club_Peru_Latam_bot"),
    (13, 'ALTA FRECUENCIA STREAMING 🎼🎬', 'Alta_Frecuencia_Streaming', 'ALTA_FRECUENCIA_STREAMING_bot'),
    (14, 'PUNTO DE ENCUENTRO LATINO 🌎', 'Punto_de_Encuentro_latino', 'PUNTO_DE_ENCUENTRO_LATINO_bot'),
    (15, 'HORIZONTE DEL PACÍFICO STREAMING 🌊🤘', 'Horizonte_Del_Pacifico_Streaming', 'Horizonte_Pacifico_Streaming_Bot'),
    (16, 'AVENIDA CENTRAL DIGITAL 💃🕺', 'Avenida_Central_Digital', 'AVENIDA_CENTRAL_DIGITAL_BOT'),
    (17, 'CÍRCULO ABIERTO STREAMING 🫶✍️', 'Circulo_Abierto_Streaming', 'CIRCULO_ABIERTO_STREAMING_bot'),
    (18, 'TERRITORIO INDIGO DIGITAL 🤖👺', 'Territorio_Indigo_Digital', 'TERRITORIO_INDIGO_DIGITAL_BOT'),
    (19, 'LA QUINTA AVENIDA STREAMING 🛑🚨', 'La_Quinta_Avenida_Streaming', 'La_Quinta_Avenida_Streaming_bot'),
    (20, 'COSTA DORADA ENTRETENIMIENTO 🍀🌿🌼', 'Costa_Dorada_Entretenimiento', 'Costa_Dorada_Entrete_Bot'),
    (21, 'NUEVA ERA DE CONEXIONES 👽🫂', 'Nueva_Era_De_Conexiones', 'Nueva_Era_Conexiones_Bot'),
]

# Bots oficiales excluidos DE RAÍZ del control 12/12.
# Cualquier otro bot, usuario o administrador sí queda sujeto a la regla.
BOTS_OFICIALES_EXENTOS = {
    "distritostreaminguniversal_bot",
    "streamingdigitalperucho_bot",
    "peruentretenimientostreaming_bot",
    "mucastbot",
    "universocibertneticoperu_bot",
    "metaversoperu_bot",
    "mundostreamingperu_bot",
    "club_premium_latino_bot",
    "mundo_play_e_digital_bot",
    "conecta2_streaming_bot",
    "imperio_digital_streaming_bot",
    "the_digital_club_peru_latam_bot",
    'alta_frecuencia_streaming_bot',
    'punto_de_encuentro_latino_bot',
    'horizonte_pacifico_streaming_bot',
    'avenida_central_digital_bot',
    'circulo_abierto_streaming_bot',
    'territorio_indigo_digital_bot',
    'la_quinta_avenida_streaming_bot',
    'costa_dorada_entrete_bot',
    'nueva_era_conexiones_bot',

    "maximocontrolgroup_bot",
    "unionmembresia_bot",
    "publicidadcontrolstreaming_bot",
    "publicidadcontratada_bot",
    "groupanonymousbot",
}

BIENVENIDAS = None
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
# No alteran las reglas raíz de los 12 grupos.
SELECCIONES_MODERACION_ORMA = {}
ENTRADAS_ORMA_TOTAL = {}

# Entradas efímeras exclusivas del Centro de Control de Raíz.
# Clave: propietario_id -> {"accion": str}
ENTRADAS_RAIZ = {}

# Un solo aviso publicitario temporal por identidad y grupo.
AVISOS_PUBLICIDAD_ACTIVOS = {}

APP_VERSION = "1.0.20-rc.2"
APP_VERSION_TITULO = "CANDIDATA · REGISTRO 21 Y LIMPIEZA DE INGRESOS"
APP_VERSION_FECHA = "27/10/2026"
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


@contextmanager
def conectar_db():
    conexion = sqlite3.connect(DATABASE_PATH)
    conexion.row_factory = sqlite3.Row
    try:
        with conexion:
            yield conexion
    finally:
        conexion.close()


def columna_existe(conexion, tabla, columna):
    columnas = {
        fila["name"]
        for fila in conexion.execute(f"PRAGMA table_info({tabla})").fetchall()
    }
    return columna in columnas



def sello_version_panel():
    return (
        f"🏷 <b>v{APP_VERSION}</b> · "
        f"{APP_VERSION_TITULO} · {APP_VERSION_FECHA}"
    )


def texto_menu_principal_orma():
    return (
        "🦍 <b>MÁXIMO CONTROL TOTAL</b>\n"
        + sello_version_panel()
        + "\n\nCentro privado de administración.\n\n"
        "📌 Responde cualquier mensaje en un grupo controlado "
        "con <code>/orma</code> para abrir su expediente.\n\n"
        "🧹 Los comandos y datos operativos se eliminan automáticamente "
        "para mantener el panel limpio."
    )


def teclado_menu_principal_orma():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "👥 CLIENTES EDITADOS",
            callback_data="orma_clientes_editados:0",
        )],
        [InlineKeyboardButton(
            "⚙️ CONTROL DE RAÍZ 21/21",
            callback_data="orma_raiz",
        )],
        [InlineKeyboardButton(
            "🗑 CERRAR",
            callback_data="orma_cerrar",
        )],
    ])


def teclado_navegacion_raiz(
    volver_callback="orma_raiz",
    volver_texto="⬅️ CONTROL DE RAÍZ",
):
    return [
        [InlineKeyboardButton(volver_texto, callback_data=volver_callback)],
        [
            InlineKeyboardButton("🏠 MENÚ PRINCIPAL", callback_data="orma_menu_principal"),
            InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar"),
        ],
    ]


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
            "grupos_base_7_actuales": "INTEGER NOT NULL DEFAULT 0",
            "grupos_expansion_5_actuales": "INTEGER NOT NULL DEFAULT 0",
            "maximo_expansion_5": "INTEGER NOT NULL DEFAULT 0",
            "alcanzo_5de5": "INTEGER NOT NULL DEFAULT 0",
            "alcanzo_12de12": "INTEGER NOT NULL DEFAULT 0",
            "perdio_12de12": "INTEGER NOT NULL DEFAULT 0",
            "grupos_7de7_historico": "INTEGER",
            "maximo_7de7_historico": "INTEGER",

            "total_verificaciones": "INTEGER NOT NULL DEFAULT 0",
            "fecha_primera_verificacion": "TEXT",
            "fecha_ultima_verificacion": "TEXT",
            "origen_chat_id": "INTEGER",
            "origen_username": "TEXT",
            "origen_nombre": "TEXT",
            "fecha_ultimo_acceso": "TEXT",
        }

        snapshot_7de7_necesario = (
            not columna_existe(
                conexion,
                "usuarios_membresia",
                "grupos_7de7_historico",
            )
        )

        for columna, definicion in columnas_monitoreo.items():
            if not columna_existe(conexion, "usuarios_membresia", columna):
                conexion.execute(
                    f"ALTER TABLE usuarios_membresia ADD COLUMN {columna} {definicion}"
                )

        if snapshot_7de7_necesario:
            conexion.execute(
                """
                UPDATE usuarios_membresia
                SET grupos_7de7_historico = grupos_actuales,
                    maximo_7de7_historico = maximo_grupos,
                    grupos_base_7_actuales = CASE
                        WHEN grupos_actuales > 7 THEN 7
                        ELSE COALESCE(grupos_actuales, 0)
                    END
                """
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

        # v1.0.9: auditoría persistente de la puerta raíz de membresía 12/12.
        # Registra el resultado real de la eliminación para poder distinguir
        # un bloqueo confirmado de un fallo de Telegram sin alterar otras reglas.
        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS eventos_membresia_7de7 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                nombre TEXT,
                chat_id INTEGER NOT NULL,
                chat_username TEXT,
                chat_nombre TEXT,
                message_id INTEGER NOT NULL,
                grupos_actuales INTEGER NOT NULL DEFAULT 0,
                grupos_requeridos INTEGER NOT NULL DEFAULT 7,
                decision TEXT NOT NULL,
                eliminacion_estado TEXT NOT NULL,
                error TEXT,
                fecha_evento TEXT NOT NULL,
                UNIQUE(chat_id, message_id, user_id)
            )
            """
        )

        conexion.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_eventos_membresia_7de7_usuario_fecha
            ON eventos_membresia_7de7 (user_id, fecha_evento)
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
                controlar_texto_simple INTEGER NOT NULL DEFAULT 0,
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

        # v1.0.6: excepción individual para texto publicitario simple.
        # Por defecto queda en 0 para preservar TEXTO NORMAL PURO libre por regla general.
        if not columna_existe(
            conexion,
            "control_publicidad_identidades",
            "controlar_texto_simple",
        ):
            conexion.execute(
                """
                ALTER TABLE control_publicidad_identidades
                ADD COLUMN controlar_texto_simple INTEGER NOT NULL DEFAULT 0
                """
            )

        # Preservar la excepción efectiva de las reglas propias anteriores.
        if not columna_existe(conexion, "control_publicidad_grupos", "controlar_texto_simple"):
            conexion.execute("ALTER TABLE control_publicidad_grupos ADD COLUMN controlar_texto_simple INTEGER NOT NULL DEFAULT 0")
            conexion.execute("""UPDATE control_publicidad_grupos SET controlar_texto_simple =
                COALESCE((SELECT controlar_texto_simple FROM control_publicidad_identidades i
                WHERE i.identidad_tipo=control_publicidad_grupos.identidad_tipo
                AND i.identidad_id=control_publicidad_grupos.identidad_id), 0)""")

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

        if not columna_existe(conexion, "eventos_publicidad_control", "eliminacion_estado"):
            conexion.execute(
                "ALTER TABLE eventos_publicidad_control ADD COLUMN eliminacion_estado TEXT"
            )

        # v1.0.8: vigilancia persistente de texto normal puro largo.
        # Primera huella (>50 caracteres visibles) queda en vigilancia; la segunda
        # coincidencia de la misma identidad se clasifica como publicidad textual.
        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS vigilancia_texto_repetitivo (
                identidad_tipo TEXT NOT NULL,
                identidad_id INTEGER NOT NULL,
                huella_sha256 TEXT NOT NULL,
                texto_normalizado TEXT NOT NULL,
                longitud_visible INTEGER NOT NULL,
                primera_fecha TEXT NOT NULL,
                ultima_fecha TEXT NOT NULL,
                total_coincidencias INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (identidad_tipo, identidad_id, huella_sha256)
            )
            """
        )

        conexion.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vigilancia_texto_identidad
            ON vigilancia_texto_repetitivo (identidad_tipo, identidad_id)
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

        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS configuracion_raiz (
                clave TEXT PRIMARY KEY,
                valor TEXT NOT NULL,
                fecha_actualizacion TEXT NOT NULL
            )
            """
        )
        for clave, valor in (
            ("membresia_7de7_activa", "1"),
            ("grupo_pruebas_activo", "1"),
            ("aviso_membresia_segundos", str(AVISO_MEMBRESIA_SEGUNDOS)),
            ("aviso_publicidad_segundos", str(AVISO_PUBLICIDAD_SEGUNDOS)),
        ):
            conexion.execute(
                "INSERT OR IGNORE INTO configuracion_raiz (clave, valor, fecha_actualizacion) VALUES (?, ?, ?)",
                (clave, valor, ahora),
            )

        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS bots_exentos_raiz (
                username TEXT PRIMARY KEY,
                activo INTEGER NOT NULL DEFAULT 1,
                fecha_actualizacion TEXT NOT NULL
            )
            """
        )
        for username in sorted(BOTS_OFICIALES_EXENTOS):
            conexion.execute(
                "INSERT OR IGNORE INTO bots_exentos_raiz (username, activo, fecha_actualizacion) VALUES (?, 1, ?)",
                (username, ahora),
            )

        conexion.commit()


def obtener_config_raiz(clave, predeterminado="1"):
    try:
        with conectar_db() as conexion:
            fila = conexion.execute(
                "SELECT valor FROM configuracion_raiz WHERE clave = ?", (clave,)
            ).fetchone()
        return str(fila["valor"] if fila else predeterminado)
    except sqlite3.Error:
        return str(predeterminado)


def guardar_config_raiz(clave, valor):
    with conectar_db() as conexion:
        conexion.execute(
            """INSERT INTO configuracion_raiz (clave, valor, fecha_actualizacion)
               VALUES (?, ?, ?)
               ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor, fecha_actualizacion=excluded.fecha_actualizacion""",
            (str(clave), str(valor), datetime.now(timezone.utc).isoformat()),
        )
        conexion.commit()


def obtener_config_raiz_entero(clave, predeterminado):
    try:
        return max(1, int(obtener_config_raiz(clave, predeterminado)))
    except (TypeError, ValueError):
        return int(predeterminado)


def config_raiz_activa(clave, predeterminado=True):
    return obtener_config_raiz(clave, "1" if predeterminado else "0") == "1"


def alternar_config_raiz(clave):
    nuevo = "0" if config_raiz_activa(clave) else "1"
    guardar_config_raiz(clave, nuevo)
    return nuevo == "1"


def obtener_grupos_raiz_db():
    with conectar_db() as conexion:
        return conexion.execute(
            "SELECT * FROM grupos_obligatorios ORDER BY orden ASC"
        ).fetchall()


def obtener_grupo_raiz_db(username):
    clave = str(username or "").lstrip("@").lower()
    with conectar_db() as conexion:
        return conexion.execute(
            """SELECT * FROM grupos_obligatorios
               WHERE LOWER(username) = ? LIMIT 1""",
            (clave,),
        ).fetchone()


def obtener_bots_exentos_raiz_db():
    with conectar_db() as conexion:
        return conexion.execute(
            "SELECT username, activo FROM bots_exentos_raiz ORDER BY username"
        ).fetchall()


def bot_exento_raiz_activo(username):
    clave = str(username or "").lstrip("@").lower()
    # Los bots estructurales de raíz nunca pueden perder su exención desde UI.
    if clave in BOTS_OFICIALES_EXENTOS:
        return True
    try:
        with conectar_db() as conexion:
            fila = conexion.execute(
                "SELECT activo FROM bots_exentos_raiz WHERE username = ?", (clave,)
            ).fetchone()
        return bool(fila["activo"]) if fila is not None else False
    except sqlite3.Error:
        return False


def agregar_bot_exento_raiz(username):
    clave = str(username or "").lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{5,32}", clave):
        raise ValueError("Username de Telegram no válido")
    with conectar_db() as conexion:
        conexion.execute(
            """INSERT INTO bots_exentos_raiz (username, activo, fecha_actualizacion)
               VALUES (?, 1, ?)
               ON CONFLICT(username) DO UPDATE SET activo=1, fecha_actualizacion=excluded.fecha_actualizacion""",
            (clave, datetime.now(timezone.utc).isoformat()),
        )
        conexion.commit()
    return clave


def quitar_bot_exento_raiz(username):
    clave = str(username or "").lstrip("@").lower()
    if clave in BOTS_OFICIALES_EXENTOS:
        return False
    with conectar_db() as conexion:
        cursor = conexion.execute(
            "DELETE FROM bots_exentos_raiz WHERE username = ?", (clave,)
        )
        conexion.commit()
    return cursor.rowcount > 0


def bots_exentos_personalizados_raiz():
    return [
        fila for fila in obtener_bots_exentos_raiz_db()
        if fila["username"] not in BOTS_OFICIALES_EXENTOS and bool(fila["activo"])
    ]


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


def registrar_evento_membresia_7de7_db(
    usuario,
    chat,
    message_id,
    estado,
    eliminacion_estado,
    error=None,
):
    """Audita únicamente bloqueos de la puerta raíz 12/12."""
    ahora = datetime.now(timezone.utc).isoformat()
    nombre = " ".join(
        parte for parte in [usuario.first_name, usuario.last_name] if parte
    ).strip()
    actuales = len(estado.get("completados", []))
    requeridos = int(estado.get("total") or TOTAL_GRUPOS_OBLIGATORIOS)

    with conectar_db() as conexion:
        conexion.execute(
            """
            INSERT INTO eventos_membresia_7de7 (
                user_id, username, nombre,
                chat_id, chat_username, chat_nombre, message_id,
                grupos_actuales, grupos_requeridos, decision,
                eliminacion_estado, error, fecha_evento
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id, user_id) DO UPDATE SET
                grupos_actuales = excluded.grupos_actuales,
                grupos_requeridos = excluded.grupos_requeridos,
                decision = excluded.decision,
                eliminacion_estado = excluded.eliminacion_estado,
                error = excluded.error,
                fecha_evento = excluded.fecha_evento
            """,
            (
                usuario.id,
                usuario.username,
                nombre,
                chat.id,
                getattr(chat, "username", None),
                getattr(chat, "title", None),
                message_id,
                actuales,
                requeridos,
                "BLOQUEADA_7DE7",
                eliminacion_estado,
                str(error) if error else None,
                ahora,
            ),
        )
        conexion.commit()


def registrar_evento_sender_chat_7de7_db(
    sender_chat,
    chat,
    message_id,
    eliminacion_estado,
    error=None,
):
    """Audita publicaciones como canal/chat que no pueden acreditar 12/12."""
    ahora = datetime.now(timezone.utc).isoformat()
    identidad_id = int(getattr(sender_chat, "id", 0) or 0)
    username = getattr(sender_chat, "username", None)
    nombre = getattr(sender_chat, "title", None) or "CANAL/CHAT"

    with conectar_db() as conexion:
        conexion.execute(
            """
            INSERT INTO eventos_membresia_7de7 (
                user_id, username, nombre,
                chat_id, chat_username, chat_nombre, message_id,
                grupos_actuales, grupos_requeridos, decision,
                eliminacion_estado, error, fecha_evento
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id, user_id) DO UPDATE SET
                decision = excluded.decision,
                eliminacion_estado = excluded.eliminacion_estado,
                error = excluded.error,
                fecha_evento = excluded.fecha_evento
            """,
            (
                identidad_id,
                username,
                nombre,
                chat.id,
                getattr(chat, "username", None),
                getattr(chat, "title", None),
                message_id,
                TOTAL_GRUPOS_OBLIGATORIOS,
                "BLOQUEADA_SENDER_CHAT_7DE7",
                eliminacion_estado,
                str(error) if error else None,
                ahora,
            ),
        )
        conexion.commit()


async def mostrar_aviso_sender_chat_7de7_temporal(context, chat, sender_chat):
    """Aviso temporal para publicaciones hechas en nombre de canal/chat."""
    nombre = html.escape(
        str(getattr(sender_chat, "title", None) or "CANAL/CHAT")
    )
    aviso = await context.bot.send_message(
        chat_id=chat.id,
        text=(
            "🔒 <b>MEMBRESÍA 21/21 REQUERIDA</b>\n\n"
            f"La publicación enviada como <b>{nombre}</b> fue retirada.\n\n"
            "Las publicaciones en nombre de un canal/chat no pueden acreditar "
            "la membresía personal 21/21. Envía el mensaje con tu usuario de Telegram "
            "y completa la membresía para participar."
        ),
        parse_mode="HTML",
    )
    asyncio.create_task(
        eliminar_mensaje_despues(
            aviso,
            obtener_config_raiz_entero(
                "aviso_membresia_segundos",
                AVISO_MEMBRESIA_SEGUNDOS,
            ),
        )
    )


async def eliminar_mensaje_membresia_7de7_estricto(
    context,
    mensaje,
    chat,
    usuario_id,
):
    """Doble intento de eliminación exclusivo para la puerta raíz 12/12."""
    try:
        await mensaje.delete()
        return True, "ELIMINADA_PRIMER_INTENTO", None
    except TelegramError as error_1:
        logging.warning(
            "12/12 primer intento de eliminación falló user=%s chat=%s message=%s: %s",
            usuario_id,
            chat.id,
            mensaje.message_id,
            error_1,
        )

    try:
        await context.bot.delete_message(
            chat_id=chat.id,
            message_id=mensaje.message_id,
        )
        return True, "ELIMINADA_SEGUNDO_INTENTO", str(error_1)
    except TelegramError as error_2:
        logging.exception(
            "12/12 BLOQUEO FALLIDO user=%s chat=%s message=%s",
            usuario_id,
            chat.id,
            mensaje.message_id,
        )
        detalle = f"primer_intento={error_1}; segundo_intento={error_2}"
        return False, "FALLO_ELIMINACION", detalle


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
    completados = estado["completados"]
    actuales = sum(1 for grupo in completados if int(grupo["orden"]) <= TOTAL_GRUPOS_HISTORICOS)

    ordenes = {
        int(grupo["orden"])
        for grupo in completados
        if grupo["orden"] is not None
    }
    base_7_actuales = sum(1 for orden in range(1, 8) if orden in ordenes)
    expansion_5_actuales = sum(1 for orden in range(8, 13) if orden in ordenes)

    with conectar_db() as conexion:
        fila = conexion.execute(
            "SELECT * FROM usuarios_membresia WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if not fila:
            return

        maximo_anterior = int(fila["maximo_grupos"] or 0)
        maximo_nuevo = max(maximo_anterior, actuales)

        alcanzo_7_antes = bool(fila["alcanzo_7de7"])
        alcanzo_7_ahora = alcanzo_7_antes or base_7_actuales == 7
        perdio_7 = bool(fila["perdio_grupos"]) or (
            alcanzo_7_ahora and base_7_actuales < 7
        )

        maximo_expansion_anterior = int(fila["maximo_expansion_5"] or 0)
        maximo_expansion_nuevo = max(
            maximo_expansion_anterior,
            expansion_5_actuales,
        )
        alcanzo_5_antes = bool(fila["alcanzo_5de5"])
        alcanzo_5_ahora = alcanzo_5_antes or expansion_5_actuales == 5

        alcanzo_12_antes = bool(fila["alcanzo_12de12"])
        alcanzo_12_ahora = alcanzo_12_antes or (
            base_7_actuales == 7 and expansion_5_actuales == 5
        )
        perdio_12 = bool(fila["perdio_12de12"]) or (
            alcanzo_12_ahora and actuales < TOTAL_GRUPOS_HISTORICOS
        )

        primera = fila["fecha_primera_verificacion"] or ahora

        conexion.execute(
            """
            UPDATE usuarios_membresia
            SET grupos_actuales = ?,
                maximo_grupos = ?,
                grupos_base_7_actuales = ?,
                grupos_expansion_5_actuales = ?,
                maximo_expansion_5 = ?,
                alcanzo_7de7 = ?,
                perdio_grupos = ?,
                alcanzo_5de5 = ?,
                alcanzo_12de12 = ?,
                perdio_12de12 = ?,
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
                base_7_actuales,
                expansion_5_actuales,
                maximo_expansion_nuevo,
                1 if alcanzo_7_ahora else 0,
                1 if perdio_7 else 0,
                1 if alcanzo_5_ahora else 0,
                1 if alcanzo_12_ahora else 0,
                1 if perdio_12 else 0,
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
                SUM(CASE WHEN grupos_actuales BETWEEN 1 AND 11 THEN 1 ELSE 0 END) AS proceso,
                SUM(CASE WHEN grupos_actuales = 12 THEN 1 ELSE 0 END) AS completos,
                SUM(CASE WHEN alcanzo_7de7 = 1 THEN 1 ELSE 0 END) AS alcanzo_base_7,
                SUM(CASE WHEN perdio_grupos = 1 THEN 1 ELSE 0 END) AS perdieron_base_7,
                SUM(CASE WHEN alcanzo_5de5 = 1 THEN 1 ELSE 0 END) AS alcanzo_expansion_5,
                SUM(CASE WHEN alcanzo_12de12 = 1 THEN 1 ELSE 0 END) AS alcanzo_total_12,
                SUM(CASE WHEN perdio_12de12 = 1 THEN 1 ELSE 0 END) AS perdieron_total_12
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
            SELECT
                user_id,
                username,
                nombre,
                grupos_actuales,
                grupos_base_7_actuales,
                grupos_expansion_5_actuales,
                fecha_ultimo_acceso
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
        "?? <b>MONITOREO DE MEMBRES?A</b>",
        "",
        f"?? Usuarios registrados: <b>{int(t['total'] or 0)}</b>",
        f"? Iniciaron sin verificar: <b>{int(t['sin_verificar'] or 0)}</b>",
        f"?? Estado 0/12: <b>{int(t['cero'] or 0)}</b>",
        f"?? Estado 1?11/12: <b>{int(t['proceso'] or 0)}</b>",
        f"? Estado 12/12: <b>{int(t['completos'] or 0)}</b>",
        "",
        "?? <b>COMPATIBILIDAD HIST?RICA</b>",
        f"? Alcanzaron base 7/7: <b>{int(t['alcanzo_base_7'] or 0)}</b>",
        f"? Perdieron base 7/7 despu?s: <b>{int(t['perdieron_base_7'] or 0)}</b>",
        f"? Alcanzaron expansi?n 5/5: <b>{int(t['alcanzo_expansion_5'] or 0)}</b>",
        f"? Alcanzaron total 12/12: <b>{int(t['alcanzo_total_12'] or 0)}</b>",
        f"? Perdieron 12/12 despu?s: <b>{int(t['perdieron_total_12'] or 0)}</b>",
        "",
        "?? <b>ORIGEN DE LOS ACCESOS</b>",
    ]

    if resumen["origenes"]:
        for fila in resumen["origenes"]:
            lineas.append(
                f"? {html.escape(str(fila['origen']))}: "
                f"<b>{int(fila['total'])}</b>"
            )
    else:
        lineas.append("? Todav?a sin registros")

    lineas.extend(["", "?? <b>ACCESOS RECIENTES</b>"])

    if resumen["recientes"]:
        for fila in resumen["recientes"]:
            identidad = fila["username"] or fila["nombre"] or str(fila["user_id"])
            lineas.append(
                f"? {html.escape(str(identidad))} ? "
                f"<b>{int(fila['grupos_actuales'] or 0)}/12</b> "
                f"(7/7: {int(fila['grupos_base_7_actuales'] or 0)}/7 ? "
                f"5/5: {int(fila['grupos_expansion_5_actuales'] or 0)}/5) ? "
                f"{formatear_fecha_peru(fila['fecha_ultimo_acceso'])}"
            )
    else:
        lineas.append("? Todav?a sin registros")

    lineas.extend([
        "",
        "Actualizado: " + formatear_fecha_peru(
            datetime.now(timezone.utc).isoformat()
        ),
    ])
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

    return {
        "total": len(grupos),
        "completados": completados,
        "faltantes": faltantes,
        "errores": errores,
        "completo": (
            len(grupos) == TOTAL_GRUPOS_OBLIGATORIOS
            and not faltantes
            and not errores
        ),
    }


async def obtener_estado_union(user_id):
    """Consulta todos los grupos; un error es desconocido, no una salida."""
    if MAXIMO_APP_REF is None:
        raise RuntimeError("MaximoControlGroup todavía no está inicializado.")
    grupos = obtener_grupos_union()
    completados, faltantes, errores = [], [], []
    for grupo in grupos:
        try:
            miembro = await MAXIMO_APP_REF.bot.get_chat_member(
                chat_id=f"@{grupo['username']}", user_id=user_id
            )
            (completados if estado_es_miembro(miembro) else faltantes).append(grupo)
        except TelegramError as error:
            logging.warning("Membresía Unión sin confirmar user=%s grupo=%s: %s",
                            user_id, grupo["username"], error)
            errores.append((grupo, str(error)))
    return {
        "total": len(grupos), "grupos": grupos, "completados": completados,
        "faltantes": faltantes, "errores": errores,
        "completo": len(completados) == TOTAL_GRUPOS_UNION and not errores,
    }


def obtener_grupos_union():
    """Combina los 21 grupos obligatorios con los nombres cortos del catálogo."""
    grupos = [dict(g) for g in obtener_grupos_obligatorios_db()]
    ruta = os.path.join(os.path.dirname(__file__), "pertenencia_grupos.json")
    with open(ruta, encoding="utf-8") as archivo:
        catalogo = json.load(archivo)
    por_username = {g["username"].casefold(): g for g in grupos}
    for item in catalogo:
        username = item["username"]
        if username.casefold() in por_username:
            por_username[username.casefold()]["nombre_corto"] = item["short"]
            continue
        if not item.get("query_only"):
            raise ValueError(f"Grupo original sin configuración obligatoria: {username}")
        grupos.append({
            "username": username,
            "nombre": item["short"],
            "nombre_corto": item["short"],
            "enlace": f"https://t.me/{username}",
            "orden": len(grupos) + 1,
        })
    if len(grupos) != TOTAL_GRUPOS_UNION:
        raise ValueError(f"Se esperaban 21 grupos de Unión, encontrados {len(grupos)}")
    return grupos


def registrar_verificacion_union_12_db(user_id, estado):
    """Registra únicamente los 12 grupos obligatorios, no los 9 de consulta."""
    oficiales = {g["username"].casefold() for g in obtener_grupos_obligatorios_db() if int(g["orden"]) <= TOTAL_GRUPOS_HISTORICOS}
    completados = [
        g for g in estado["completados"]
        if g["username"].casefold() in oficiales
    ]
    registrar_verificacion_membresia_db(user_id, {"completados": completados})


def texto_union_membresia(estado):
    completos = len(estado["completados"])
    total = estado["total"]
    pendientes = len(estado["faltantes"])
    desconocidos = len(estado["errores"])
    lineas = [
        "🔐 <b>MEMBRESÍA DE USUARIO</b>", "",
        f"🟢 Dentro: <b>{completos}</b> · 🔴 Fuera: <b>{pendientes}</b>",
        f"⚪ Sin verificar: <b>{desconocidos}</b>",
        f"📊 Membresía: <b>{completos}/{total}</b>", "",
        "Selecciona un grupo para abrirlo en Telegram.",
        "Pulsa <b>🔄 VERIFICAR MEMBRESÍA</b> para actualizar.",
    ]
    if estado["completo"]:
        lineas.extend(["", "✅ <b>MEMBRESÍA COMPLETA</b>"])
    if desconocidos:
        lineas.extend(["", "⚠️ Algunas comprobaciones no pudieron confirmarse."])
    return "\n".join(lineas)


def teclado_union_membresia(estado):
    dentro = {g["username"].casefold() for g in estado["completados"]}
    desconocidos = {g["username"].casefold() for g, _ in estado["errores"]}
    botones = []
    for grupo in estado["grupos"]:
        clave = grupo["username"].casefold()
        marca = "⚪" if clave in desconocidos else ("🟢" if clave in dentro else "🔴")
        botones.append(InlineKeyboardButton(
            f"{marca} {grupo.get('nombre_corto') or grupo['nombre']}",
            url=grupo["enlace"],
        ))
    filas = [botones[i:i + 3] for i in range(0, len(botones), 3)]
    filas.append([InlineKeyboardButton(
        "🔄 VERIFICAR MEMBRESÍA", callback_data="union_verificar"
    )])
    return InlineKeyboardMarkup(filas)


def union_usuario_con_panel(user_id):
    usuario = obtener_usuario_membresia_db(user_id)
    return bool(usuario and usuario["union_bot_iniciado"] and usuario["union_panel_message_id"])


async def union_actualizar_automaticamente(user_id):
    """Serializa la actualización de un panel existente; no envía avisos nuevos."""
    if not union_usuario_con_panel(user_id):
        return False
    bloqueo = UNION_AUTO_BLOQUEOS.setdefault(user_id, asyncio.Lock())
    if bloqueo.locked():
        return False
    async with bloqueo:
        try:
            resultado = await mostrar_o_actualizar_panel_union(user_id)
            UNION_AUTO_ULTIMA_REVISION[user_id] = asyncio.get_running_loop().time()
            return resultado
        except Exception:
            logging.exception("Unión 21: actualización automática fallida user=%s", user_id)
            return False


async def union_observar_cambio_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Escucha tanto chat_member como avisos de ingreso/salida de los 21 grupos.

    El panel administrativo procesa también mensajes de servicio; limitarse a
    chat_member puede dejar sin actualización al panel privado.
    """
    cambio = update.chat_member
    mensaje = update.message
    chat = cambio.chat if cambio is not None else (mensaje.chat if mensaje is not None else None)
    if chat is None or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    usuarios = set()
    if cambio is not None:
        if estado_es_miembro(cambio.old_chat_member) != estado_es_miembro(cambio.new_chat_member):
            usuarios.add(cambio.new_chat_member.user.id)
    if mensaje is not None:
        usuarios.update(u.id for u in (mensaje.new_chat_members or []))
        if mensaje.left_chat_member is not None:
            usuarios.add(mensaje.left_chat_member.id)
    if not usuarios:
        return

    try:
        with open(os.path.join(os.path.dirname(__file__), "pertenencia_grupos.json"), encoding="utf-8") as archivo:
            catalogo = json.load(archivo)
        ids = {int(g["chat_id"]) for g in catalogo if g.get("chat_id")}
        nombres = {g["username"].casefold() for g in catalogo}
        if chat.id not in ids and username_chat(chat).casefold() not in nombres:
            return
        for user_id in usuarios:
            if not union_usuario_con_panel(user_id):
                continue
            # Reconsultar tras el evento: Telegram puede tardar en reflejar
            # la nueva membresía en get_chat_member.
            await union_actualizar_automaticamente(user_id)
            asyncio.create_task(union_revalidar_evento(user_id))
    except Exception:
        logging.exception("Unión 21: no se pudo procesar evento de membresía")


async def union_revalidar_evento(user_id):
    """Segunda lectura tras propagación de Telegram, sin mensajes adicionales."""
    await asyncio.sleep(3)
    await union_actualizar_automaticamente(user_id)


async def union_revision_automatica():
    """Respaldo gradual para eventos ausentes: máximo una cuenta por ciclo."""
    while True:
        try:
            ahora = asyncio.get_running_loop().time()
            with conectar_db() as conexion:
                filas = conexion.execute(
                    "SELECT user_id FROM usuarios_membresia "
                    "WHERE union_bot_iniciado = 1 AND union_panel_message_id IS NOT NULL "
                    "ORDER BY user_id"
                ).fetchall()
            candidatos = [int(f["user_id"]) for f in filas]
            vigentes = set(candidatos)
            for user_id in list(UNION_AUTO_ULTIMA_REVISION):
                if user_id not in vigentes:
                    UNION_AUTO_ULTIMA_REVISION.pop(user_id, None)
                    UNION_AUTO_BLOQUEOS.pop(user_id, None)
            elegibles = [uid for uid in candidatos if ahora - UNION_AUTO_ULTIMA_REVISION.get(uid, -1e12) >= UNION_AUTO_REVISION_SEGUNDOS]
            if elegibles:
                elegido = min(elegibles, key=lambda uid: UNION_AUTO_ULTIMA_REVISION.get(uid, -1e12))
                await union_actualizar_automaticamente(elegido)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Unión 21: fallo en revisión automática de respaldo")
        await asyncio.sleep(UNION_AUTO_INTERVALO_SEGUNDOS)


async def mostrar_o_actualizar_panel_union(user_id):
    if UNION_APP_REF is None:
        return False

    estado = await obtener_estado_union(user_id)
    registrar_verificacion_union_12_db(user_id, estado)
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

    return bot_exento_raiz_activo(username_usuario(user))


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
        (username == GRUPO_PRUEBAS_USERNAME.lower() and config_raiz_activa("grupo_pruebas_activo"))
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
        "Para participar debes completar tu membresía en los 12 grupos oficiales.\n\n"
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
            obtener_config_raiz_entero("aviso_membresia_segundos", AVISO_MEMBRESIA_SEGUNDOS),
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
        "controlar_texto_simple",
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


def texto_visible_puro(mensaje):
    texto = getattr(mensaje, "text", None)
    return str(texto or "")


def normalizar_texto_vigilancia(texto):
    # Normalización deliberadamente conservadora y auditable: Unicode visible
    # intacto, espacios/saltos repetidos equivalentes y extremos eliminados.
    return re.sub(r"\s+", " ", str(texto or "")).strip()


def clasificar_texto_repetitivo_db(identidad_tipo, identidad_id, mensaje):
    texto = texto_visible_puro(mensaje)
    if not texto:
        return None

    # Regla de raíz: hasta 50 caracteres visibles permanece 100 % libre.
    if len(texto) <= 50:
        return None

    normalizado = normalizar_texto_vigilancia(texto)
    if len(normalizado) <= 50:
        return None

    huella = hashlib.sha256(normalizado.encode("utf-8")).hexdigest()
    ahora = datetime.now(timezone.utc).isoformat()

    with conectar_db() as conexion:
        fila = conexion.execute(
            """
            SELECT total_coincidencias
            FROM vigilancia_texto_repetitivo
            WHERE identidad_tipo = ? AND identidad_id = ? AND huella_sha256 = ?
            """,
            (identidad_tipo, int(identidad_id), huella),
        ).fetchone()

        if fila is None:
            conexion.execute(
                """
                INSERT INTO vigilancia_texto_repetitivo (
                    identidad_tipo, identidad_id, huella_sha256, texto_normalizado,
                    longitud_visible, primera_fecha, ultima_fecha, total_coincidencias
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    identidad_tipo, int(identidad_id), huella, normalizado,
                    len(texto), ahora, ahora,
                ),
            )
            conexion.commit()
            return "VIGILANCIA_PRIMERA"

        conexion.execute(
            """
            UPDATE vigilancia_texto_repetitivo
            SET ultima_fecha = ?, total_coincidencias = total_coincidencias + 1
            WHERE identidad_tipo = ? AND identidad_id = ? AND huella_sha256 = ?
            """,
            (ahora, identidad_tipo, int(identidad_id), huella),
        )
        conexion.commit()
        return "PUBLICIDAD_TEXTUAL_REPETITIVA"


def tipo_publicitario_mensaje(mensaje, usuario=None, controlar_texto_simple=False):
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

    # Excepción manual v1.0.6: CONTROLAR TEXTO SIMPLE conserva prioridad.
    # La regla automática de repetición se evalúa después, ya con identidad.
    if tipo == "TEXTO" and bool(controlar_texto_simple):
        return "TEXTO SIMPLE"

    return None


def tipo_habilitado_por_config(tipo, cfg):
    mapa = {
        "FOTO": "controlar_foto",
        "VIDEO": "controlar_video",
        "GIF/ANIMACIÓN": "controlar_gif",
        "DOCUMENTO": "controlar_documento",
        "TEXTO + ENLACE": "controlar_enlace",
        "CUSTOM EMOJI": "controlar_custom_emoji",
        "TEXTO SIMPLE": "controlar_texto_simple",
        "PUBLICIDAD TEXTUAL REPETITIVA": None,
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
    eliminacion_estado=None,
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
                fecha_evento,
                eliminacion_estado
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                eliminacion_estado,
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

        if int(limite) == 0:
            return (
                False,
                f"{alcance} · {etiqueta} = 0 · MEMBRESÍA PUBLICITARIA REQUERIDA",
                cfg,
                None,
            )

        if usados >= int(limite):
            return False, f"{alcance} · {etiqueta}", cfg, None

    return True, f"{alcance} · DENTRO DE LOS LÍMITES", cfg, None

def texto_valor_limite(valor):
    if valor is None:
        return "SIN LÍMITE"
    if int(valor) == 0:
        return "0 · BLOQUEO ESTRICTO"
    return str(valor)


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
        ("Texto simple · membresía", "controlar_texto_simple"),
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
    inicio_diario, _ = ciclo_diario_24h_config(cfg)
    if inicio_diario is not None:
        uso["dia"] = contar_publicidad_permitida_grupo_db(
            captura["objetivo_tipo"], captura["objetivo_id"], captura["chat_id"], inicio_diario.isoformat()
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
        (
            "• Texto normal puro: <b>RESTRINGIDO SOLO PARA ESTA IDENTIDAD · "
            "MEMBRESÍA PUBLICITARIA</b>"
            if bool(cfg["controlar_texto_simple"]) and modo not in {"ILIMITADO", "EXCLUIDO"}
            else "• Texto normal puro: <b>LIBRE POR REGLA DE RAÍZ</b>"
        ),
        "",
        "Selecciona qué deseas modificar.",
    ])

    return "\n".join(lineas)

def teclado_tipos_publicidad_identidad(captura_id, cfg):
    def marca(campo):
        return "✅" if bool(cfg[campo]) else "❌"

    texto_estado = (
        "🔒 TEXTO SIMPLE · MEMBRESÍA"
        if bool(cfg["controlar_texto_simple"])
        else "🔓 TEXTO SIMPLE · LIBRE"
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{marca('controlar_foto')} FOTO",
                callback_data=f"orma_pub_toggle:{captura_id}:controlar_foto",
            ),
            InlineKeyboardButton(
                f"{marca('controlar_video')} VIDEO",
                callback_data=f"orma_pub_toggle:{captura_id}:controlar_video",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{marca('controlar_gif')} GIF",
                callback_data=f"orma_pub_toggle:{captura_id}:controlar_gif",
            ),
            InlineKeyboardButton(
                f"{marca('controlar_documento')} DOCUMENTO",
                callback_data=f"orma_pub_toggle:{captura_id}:controlar_documento",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{marca('controlar_enlace')} ENLACE",
                callback_data=f"orma_pub_toggle:{captura_id}:controlar_enlace",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{marca('controlar_custom_emoji')} PREMIUM EMOJI",
                callback_data=f"orma_pub_toggle:{captura_id}:controlar_custom_emoji",
            ),
        ],
        [
            InlineKeyboardButton(
                texto_estado,
                callback_data=f"orma_pub_texto_simple:{captura_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "⬅️ RETROCEDER",
                callback_data=f"orma_publicidad:{captura_id}",
            ),
        ],
    ])


def texto_tipos_publicidad_identidad(cfg):
    estado_texto = (
        "🔒 <b>RESTRINGIDO INDIVIDUALMENTE</b> · solicita Membresía Publicitaria"
        if bool(cfg["controlar_texto_simple"])
        else "🔓 <b>LIBRE</b> · aplica la regla general de raíz"
    )
    return (
        "🎛 <b>TIPOS CONTROLADOS</b>\n\n"
        "✅ = entra al control de cupos/separación\n"
        "❌ = queda libre para esta identidad\n\n"
        f"💬 Texto simple: {estado_texto}\n\n"
        "La restricción de texto simple es una <b>excepción individual</b>. "
        "No modifica la regla general TEXTO NORMAL PURO: LIBRE POR DEFECTO "
        "para los demás usuarios."
    )



async def eliminar_aviso_publicidad_programado(
    bot,
    chat_id,
    identidad_id,
    message_id,
):
    try:
        await asyncio.sleep(obtener_config_raiz_entero("aviso_publicidad_segundos", AVISO_PUBLICIDAD_SEGUNDOS))
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


async def mostrar_aviso_texto_simple_membresia_temporal(
    context,
    chat,
    identidad_id,
):
    """
    Aviso exclusivo de la excepción individual TEXTO SIMPLE.
    No modifica el aviso normal de límites publicitarios.
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
                "📣 OBTENER MEMBRESÍA PUBLICITARIA",
                url=MEMBRESIA_PUBLICIDAD_URL,
            )
        ]
    ])

    segundos = obtener_config_raiz_entero(
        "aviso_publicidad_segundos",
        AVISO_PUBLICIDAD_SEGUNDOS,
    )
    aviso = await context.bot.send_message(
        chat_id=chat.id,
        text=(
            "📣 <b>MEMBRESÍA PUBLICITARIA REQUERIDA</b>\n\n"
            "Tu cuenta tiene una restricción individual para publicar "
            "promociones mediante <b>texto simple</b> en nuestros grupos.\n\n"
            "Para utilizar este formato publicitario, revisa las opciones de "
            "<b>Membresía Publicitaria</b> desde el botón inferior.\n\n"
            f"⏳ <i>Este aviso se eliminará automáticamente en {segundos} segundos.</i>"
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
    Panorama de los 12 grupos oficiales para /orma.

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
        resultado[clave] = "\u274c"

    for clave in errores:
        resultado[clave] = "\u26a0\ufe0f"

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
        habilitado_txt = "NO COMPROBADO EN ESTA PANTALLA"
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
        f"• Habilitado 21/21: <b>{habilitado_txt}</b>",
    ])


async def estado_7grupos_orma_concurrente(objetivo_id):
    """
    Consulta los 12 grupos EN PARALELO para que /orma siga siendo rápido.
    No reemplaza ni modifica la regla raíz 12/12.
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
        "ancla_limite_dia",
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
        "controlar_texto_simple",
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
    if "limite_dia" in datos and "ancla_limite_dia" not in datos:
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
        "ancla_limite_dia": global_cfg["ancla_limite_dia"],
        "limite_semana": global_cfg["limite_semana"],
        "limite_mes": global_cfg["limite_mes"],
        "limite_anio": global_cfg["limite_anio"],
        "controlar_foto": global_cfg["controlar_foto"],
        "controlar_video": global_cfg["controlar_video"],
        "controlar_gif": global_cfg["controlar_gif"],
        "controlar_documento": global_cfg["controlar_documento"],
        "controlar_enlace": global_cfg["controlar_enlace"],
        "controlar_custom_emoji": global_cfg["controlar_custom_emoji"],
        "controlar_texto_simple": global_cfg["controlar_texto_simple"] ,
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
    chat = await bot.get_chat(chat_ref)
    permisos = getattr(chat, "permissions", None)
    if permisos is None:
        raise RuntimeError("No se pudieron obtener los permisos actuales del grupo; no se aplicó desmute.")
    return permisos


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
            InlineKeyboardButton("🔐 MEMBRESÍA 21/21", callback_data=f"orma_membresia:{captura_id}"),
            InlineKeyboardButton("📊 ACTIVIDAD 12/12", callback_data=f"orma_actividad:{captura_id}"),
        ],
        [
            InlineKeyboardButton("📣 PUBLICIDAD 12/12", callback_data=f"orma_publicidad:{captura_id}"),
            InlineKeyboardButton("🚪 ENTRADAS / SALIDAS", callback_data=f"orma_movimientos:{captura_id}"),
        ],
        [InlineKeyboardButton("🛡️ CONTROL TOTAL · MODERACIÓN", callback_data=f"orma_mod:{captura_id}")],
        [InlineKeyboardButton("🔐 PERMISOS INDIVIDUALES · TELEGRAM", callback_data=f"orma_perm:{captura_id}")],
        [InlineKeyboardButton("📜 AUDITORÍA /ORMA", callback_data=f"orma_audit:{captura_id}")],
        [InlineKeyboardButton("⬅️ CLIENTES EDITADOS", callback_data="orma_clientes_editados:0")],
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
        ("controlar_texto_simple", "Texto simple"),
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

    if len(controlados) == 7:
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
    if any(item["error"] for item in estados):
        habilitado = None

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

    return "\n".join(lineas)

async def mostrar_ficha_orma_privada(bot, propietario_id, captura_id, pagina=0):
    captura = obtener_captura_orma(captura_id)
    if not captura_pertenece_propietario(captura, propietario_id):
        return False
    try:
        paginas = paginas_html_panel(await construir_texto_ficha_orma(captura))
        pagina = max(0, min(int(pagina), len(paginas) - 1))
        texto = paginas[pagina]
        filas = []
        if len(paginas) > 1:
            texto += f"\n\n📄 <b>Ficha · página {pagina + 1}/{len(paginas)}</b>"
            controles = []
            if pagina > 0:
                controles.append(InlineKeyboardButton("⬅️ PÁGINA ANTERIOR", callback_data=f"orma_ficha_pagina:{captura_id}:{pagina-1}"))
            if pagina + 1 < len(paginas):
                controles.append(InlineKeyboardButton("PÁGINA SIGUIENTE ➡️", callback_data=f"orma_ficha_pagina:{captura_id}:{pagina+1}"))
            filas.append(controles)
        filas.extend(teclado_ficha_orma(captura_id).inline_keyboard)
        teclado = InlineKeyboardMarkup(filas)
        panel_id = PANELES_ORMA.get(propietario_id) or obtener_panel_orma_db(propietario_id)
        if panel_id:
            resultado = await safe_edit_message_text(bot, chat_id=propietario_id,
                message_id=panel_id, text=texto, parse_mode="HTML", reply_markup=teclado)
            if resultado is not None:
                registrar_panel_reemplazo(propietario_id, resultado)
            return True
        enviado = await bot.send_message(chat_id=propietario_id, text=texto,
            parse_mode="HTML", reply_markup=teclado)
        registrar_panel_reemplazo(propietario_id, enviado)
        return True
    except Exception:
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
    for indice in range(1, TOTAL_GRUPOS_OBLIGATORIOS + 1):
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
            "🌐 CONTROL GLOBAL 21/21",
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
    inicio_diario, _ = ciclo_diario_24h_config(efectivo)
    if inicio_diario is not None:
        limites["dia"] = inicio_diario.isoformat()
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
            habilitado=None,
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
        "• Texto simple: <b>" + ("RESTRINGIDO" if bool(efectivo["controlar_texto_simple"]) and str(efectivo["modo"]).upper() not in {"ILIMITADO", "EXCLUIDO"} else "LIBRE DEL CONTROL PUBLICITARIO") + "</b>",
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
                "✅ TODOS 21/21",
                callback_data=f"orma_modall:{captura_id}:{accion}",
            ),
            InlineKeyboardButton(
                "🚫 NINGUNO",
                callback_data=f"orma_modnone:{captura_id}:{accion}",
            ),
        ]
    ]
    for indice in range(1, TOTAL_GRUPOS_OBLIGATORIOS + 1):
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


def texto_centro_control_raiz():
    membresia = "ACTIVA" if config_raiz_activa("membresia_7de7_activa") else "PAUSADA"
    pruebas = "BAJO CONTROL" if config_raiz_activa("grupo_pruebas_activo") else "FUERA DE CONTROL"
    grupos = obtener_grupos_raiz_db()
    personalizados = len(bots_exentos_personalizados_raiz())
    return (
        "⚙️ <b>CENTRO DE CONTROL DE RAÍZ 21/21</b>\n"
        + sello_version_panel()
        + "\n\n"
        "Estado operativo de las reglas de raíz:\n\n"
        f"🛡 Membresía 21/21: <b>{membresia}</b>\n"
        f"🌐 Grupos oficiales estructurales: <b>{len(grupos)}/21</b>\n"
        f"🤖 Bots exentos fijos: <b>{len(BOTS_OFICIALES_EXENTOS)}</b>\n"
        f"➕ Bots exentos añadidos: <b>{personalizados}</b>\n"
        f"🧪 @{GRUPO_PRUEBAS_USERNAME}: <b>{pruebas}</b>\n\n"
        "Selecciona el módulo que deseas administrar. "
        "Las identidades estructurales de los 12 grupos y los bots oficiales quedan protegidas."
    )


def teclado_centro_control_raiz():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛡 MEMBRESÍA 21/21", callback_data="orma_raiz_membresia")],
        [InlineKeyboardButton("🌐 GRUPOS OFICIALES 21/21", callback_data="orma_raiz_grupos")],
        [InlineKeyboardButton("🤖 BOTS EXENTOS / AUTORIZADOS", callback_data="orma_raiz_bots")],
        [InlineKeyboardButton("⏱ TIEMPOS DE AVISO", callback_data="orma_raiz_tiempos")],
        [InlineKeyboardButton("🧪 GRUPO DE PRUEBAS", callback_data="orma_raiz_pruebas")],
        [InlineKeyboardButton("📜 REGLAS PROTEGIDAS DE RAÍZ", callback_data="orma_raiz_reglas_protegidas")],
        [InlineKeyboardButton("📊 AUDITORÍA DE RAÍZ", callback_data="orma_raiz_auditoria")],
        [
            InlineKeyboardButton("🏠 MENÚ PRINCIPAL", callback_data="orma_menu_principal"),
            InlineKeyboardButton("🗑 CERRAR", callback_data="orma_cerrar"),
        ],
    ])


def texto_membresia_raiz():
    activa = config_raiz_activa("membresia_7de7_activa")
    aviso = obtener_config_raiz_entero("aviso_membresia_segundos", AVISO_MEMBRESIA_SEGUNDOS)
    return (
        "🛡 <b>MEMBRESÍA DE RAÍZ 21/21</b>\n"
        + sello_version_panel()
        + "\n\n"
        f"Estado: <b>{'🟢 ACTIVA' if activa else '🔴 PAUSADA'}</b>\n"
        "Regla estructural: <b>12 grupos oficiales obligatorios</b>\n"
        f"Aviso temporal: <b>{aviso} segundos</b>\n\n"
        "Al pausar esta regla no se borran grupos, clientes ni historiales. "
        "Solo se suspende la puerta de membresía 12/12 hasta volver a activarla."
    )


def teclado_membresia_raiz():
    activa = config_raiz_activa("membresia_7de7_activa")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🔴 PAUSAR MEMBRESÍA 21/21" if activa else "🟢 ACTIVAR MEMBRESÍA 21/21",
            callback_data="orma_raiz_membresia_toggle",
        )],
        [InlineKeyboardButton("🌐 VER LOS 12 GRUPOS", callback_data="orma_raiz_grupos")],
        [InlineKeyboardButton("⏱ CONFIGURAR AVISO", callback_data="orma_raiz_tiempos_membresia")],
        *teclado_navegacion_raiz(),
    ])


def texto_grupos_raiz():
    lineas = [
        "🌐 <b>GRUPOS OFICIALES 21/21</b>",
        sello_version_panel(),
        "",
        "Los 12 grupos forman la estructura protegida de Máximo Control.",
        "Pulsa uno para consultar su ficha de raíz.",
        "",
    ]
    for grupo in obtener_grupos_raiz_db():
        lineas.append(
            f"{grupo['orden']}. {'🟢' if grupo['activo'] else '🔴'} "
            f"<b>{html.escape(grupo['nombre'])}</b>"
        )
    lineas += ["", "🔒 Alta/baja estructural protegida para evitar romper la regla 12/12."]
    return "\n".join(lineas)


def teclado_grupos_raiz():
    filas = []
    for grupo in obtener_grupos_raiz_db():
        marca = "🟢" if grupo["activo"] else "🔴"
        filas.append([InlineKeyboardButton(
            f"{marca} {grupo['orden']}. {grupo['nombre'][:28]}",
            callback_data=f"orma_raiz_grupo:{grupo['username']}",
        )])
    filas.extend(teclado_navegacion_raiz())
    return InlineKeyboardMarkup(filas)


def texto_grupo_raiz(grupo):
    return (
        "🌐 <b>FICHA DE GRUPO · RAÍZ</b>\n"
        + sello_version_panel()
        + "\n\n"
        f"Orden: <b>{grupo['orden']}</b>\n"
        f"Nombre: <b>{html.escape(grupo['nombre'])}</b>\n"
        f"Username: <b>@{html.escape(grupo['username'])}</b>\n"
        f"Helpdesk: <b>@{html.escape(grupo['helpdesk_username'] or '—')}</b>\n"
        f"Activo: <b>{'SÍ' if grupo['activo'] else 'NO'}</b>\n"
        f"Obligatorio: <b>{'SÍ' if grupo['obligatorio'] else 'NO'}</b>\n\n"
        "🔒 <b>IDENTIDAD ESTRUCTURAL PROTEGIDA</b>\n"
        "Esta versión permite consultar la raíz sin exponer borrado, renombrado o sustitución accidental del grupo."
    )


def texto_bots_exentos_raiz():
    personalizados = bots_exentos_personalizados_raiz()
    return (
        "🤖 <b>BOTS EXENTOS / AUTORIZADOS</b>\n"
        + sello_version_panel()
        + "\n\n"
        f"🔒 Bots estructurales protegidos: <b>{len(BOTS_OFICIALES_EXENTOS)}</b>\n"
        f"➕ Bots añadidos por panel: <b>{len(personalizados)}</b>\n\n"
        "Los bots estructurales nunca pueden perder su exención desde Telegram. "
        "Puedes agregar o retirar bots adicionales sin modificar el código raíz."
    )


def teclado_bots_exentos_raiz():
    filas = [[InlineKeyboardButton("➕ AGREGAR BOT EXENTO", callback_data="orma_raiz_bot_agregar")]]
    for username in sorted(BOTS_OFICIALES_EXENTOS):
        filas.append([InlineKeyboardButton(f"🔒 @{username}", callback_data="orma_raiz_bot_protegido")])
    personalizados = bots_exentos_personalizados_raiz()
    if personalizados:
        filas.append([InlineKeyboardButton("— AÑADIDOS DESDE PANEL —", callback_data="orma_raiz_nop")])
        for fila in personalizados:
            filas.append([InlineKeyboardButton(
                f"🟢 @{fila['username']} · QUITAR",
                callback_data=f"orma_raiz_bot_quitar:{fila['username']}",
            )])
    filas.extend(teclado_navegacion_raiz())
    return InlineKeyboardMarkup(filas)


def texto_tiempos_raiz():
    memb = obtener_config_raiz_entero("aviso_membresia_segundos", AVISO_MEMBRESIA_SEGUNDOS)
    pub = obtener_config_raiz_entero("aviso_publicidad_segundos", AVISO_PUBLICIDAD_SEGUNDOS)
    return (
        "⏱ <b>TIEMPOS DE AVISO · RAÍZ</b>\n"
        + sello_version_panel()
        + "\n\n"
        f"🛡 Aviso de membresía: <b>{memb} s</b>\n"
        f"📢 Aviso publicitario: <b>{pub} s</b>\n\n"
        "Estos tiempos controlan únicamente cuánto permanece visible el aviso temporal; "
        "no cambian las reglas, cupos ni restricciones."
    )


def teclado_tiempos_raiz():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛡 AVISO MEMBRESÍA", callback_data="orma_raiz_tiempos_membresia")],
        [InlineKeyboardButton("📢 AVISO PUBLICIDAD", callback_data="orma_raiz_tiempos_publicidad")],
        *teclado_navegacion_raiz(),
    ])


def teclado_selector_tiempo_raiz(tipo):
    opciones = [15, 30, 60, 120]
    filas = [[InlineKeyboardButton(f"{seg} s", callback_data=f"orma_raiz_tiempo:{tipo}:{seg}") for seg in opciones[:2]],
             [InlineKeyboardButton(f"{seg} s", callback_data=f"orma_raiz_tiempo:{tipo}:{seg}") for seg in opciones[2:]]]
    filas.extend(teclado_navegacion_raiz(
        "orma_raiz_tiempos",
        "⬅️ TIEMPOS DE AVISO",
    ))
    return InlineKeyboardMarkup(filas)


def texto_pruebas_raiz():
    activo = config_raiz_activa("grupo_pruebas_activo")
    return (
        "🧪 <b>GRUPO DE PRUEBAS</b>\n"
        + sello_version_panel()
        + "\n\n"
        f"Grupo: <b>@{GRUPO_PRUEBAS_USERNAME}</b>\n"
        f"Estado: <b>{'🟢 BAJO CONTROL' if activo else '🔴 FUERA DE CONTROL'}</b>\n\n"
        "Este interruptor solo afecta al grupo de pruebas. Los 12 grupos oficiales permanecen intactos."
    )


def teclado_pruebas_raiz():
    activo = config_raiz_activa("grupo_pruebas_activo")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🔴 SACAR DE CONTROL" if activo else "🟢 PONER BAJO CONTROL",
            callback_data="orma_raiz_pruebas_toggle",
        )],
        *teclado_navegacion_raiz(),
    ])



def texto_reglas_protegidas_raiz():
    return (
        "📜 <b>REGLAS PROTEGIDAS DE RAÍZ</b>\n"
        + sello_version_panel()
        + "\n\n"
        "📝 <b>TEXTO NORMAL PURO · LIBERTAD + VIGILANCIA</b>\n"
        "• Hasta 50 caracteres visibles: <b>100 % LIBRE</b>; no consume cupo ni entra en vigilancia.\n"
        "• Más de 50 caracteres, primera aparición: <b>LIBRE + EN VIGILANCIA</b>.\n"
        "• La vigilancia queda persistida por identidad y no caduca por minutos, horas ni días.\n\n"
        "🔁 <b>TEXTO REPETITIVO · PUBLICIDAD</b>\n"
        "• Segunda coincidencia del mismo texto normalizado por la misma identidad: "
        "<b>PUBLICIDAD TEXTUAL REPETITIVA</b>.\n"
        "• La detección es global entre grupos; los cupos siguen siendo independientes por grupo.\n"
        "• Espacios y saltos repetidos no permiten evadir la huella.\n\n"
        "🛡 <b>CONTROLAR TEXTO SIMPLE · EXCEPCIÓN MANUAL</b>\n"
        "• Se conserva en CLIENTES EDITADOS para identidades detectadas intentando evadir la membresía.\n\n"
        "🚫 <b>PUBLICIDAD/DÍA = 0 · BLOQUEO ESTRICTO</b>\n"
        "• Cero significa ninguna publicidad permitida cuando el contenido entra al motor.\n"
        "• Se elimina únicamente la publicación; si el primer borrado falla, se intenta un segundo borrado directo.\n"
        "• La auditoría diferencia eliminación confirmada de fallo de eliminación.\n\n"
        "🎯 <b>SIN CASTIGO AL USUARIO</b>\n"
        "• Estas reglas <b>NO</b> aplican ban, mute, expulsión ni restricción general.\n"
        "• La finalidad es bloquear la publicidad no permitida y mostrar el acceso a Membresía Publicitaria.\n\n"
        "🧮 <b>CONTADORES INDEPENDIENTES POR GRUPO</b>\n"
        "• Publicar en un grupo no consume el cupo de otro.\n\n"
        "🕒 <b>CICLO DIARIO MÓVIL DE 24 HORAS</b>\n"
        "• El límite diario conserva su ancla propia.\n\n"
        "🔒 Los 12 grupos oficiales, bots estructurales y controles existentes permanecen protegidos."
    )


def teclado_reglas_protegidas_raiz():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 AUDITORÍA DE RAÍZ", callback_data="orma_raiz_auditoria")],
        *teclado_navegacion_raiz(),
    ])


def texto_auditoria_raiz():
    grupos = obtener_grupos_raiz_db()
    memb = config_raiz_activa("membresia_7de7_activa")
    pruebas = config_raiz_activa("grupo_pruebas_activo")
    personalizados = bots_exentos_personalizados_raiz()
    lineas = [
        "📊 <b>AUDITORÍA OPERATIVA DE RAÍZ</b>",
        "",
        f"🏷 Versión: <b>v{APP_VERSION} · {APP_VERSION_TITULO}</b>",
        f"🛡 Membresía 21/21: <b>{'ACTIVA' if memb else 'PAUSADA'}</b>",
        f"🌐 Grupos estructurales: <b>{len(grupos)}/21</b>",
        f"🤖 Exentos estructurales: <b>{len(BOTS_OFICIALES_EXENTOS)}</b>",
        f"➕ Exentos añadidos: <b>{len(personalizados)}</b>",
        f"🧪 Grupo de pruebas: <b>{'BAJO CONTROL' if pruebas else 'FUERA DE CONTROL'}</b>",
        f"⏱ Aviso membresía: <b>{obtener_config_raiz_entero('aviso_membresia_segundos', AVISO_MEMBRESIA_SEGUNDOS)} s</b>",
        f"📢 Aviso publicidad: <b>{obtener_config_raiz_entero('aviso_publicidad_segundos', AVISO_PUBLICIDAD_SEGUNDOS)} s</b>",
        "📝 Texto ≤50: <b>LIBRE</b> · texto >50 primera vez: <b>VIGILANCIA</b>",
        "🔁 Segunda coincidencia: <b>PUBLICIDAD TEXTUAL · SIN CADUCIDAD</b>",
        "🚫 Día=0: <b>BLOQUEO ESTRICTO DE PUBLICIDAD · SIN BAN/MUTE</b>",
        "🧮 Contadores por grupo: <b>INDEPENDIENTES · PROTEGIDO</b>",
        "🕒 Ciclo diario: <b>24 HORAS MÓVILES · PROTEGIDO</b>",
        "",
        "🔒 CLIENTES EDITADOS y sus controles no son modificados por este centro.",
    ]
    return "\n".join(lineas)


async def eliminar_origen_supervision_callback(update, context):
    """Eliminación puntual desde supervisión; conserva la copia y /orma."""
    query = update.callback_query
    usuario = update.effective_user
    if not query or not usuario:
        return
    permitidos = {int(v) for v in (ADMIN_USER_ID, ORMA_ADMIN_USER_ID) if int(v or 0) > 0}
    if usuario.id not in permitidos:
        await query.answer("Acceso no autorizado.", show_alert=True)
        return
    if not query.message or query.message.chat_id != GRUPO_PUBLICIDAD_NO_IDENTIFICADA_ID:
        await query.answer("Acción disponible solo en el grupo de supervisión.", show_alert=True)
        return
    try:
        captura_id = int((query.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Referencia inválida.", show_alert=True)
        return
    captura = obtener_captura_orma(captura_id)
    if not captura or int(captura["propietario_id"]) not in permitidos:
        await query.answer("Captura no disponible.", show_alert=True)
        return
    if (str(captura["chat_username"] or "").lower() not in usernames_grupos_oficiales()
            or not captura["mensaje_origen_id"]):
        await query.answer("Origen no autorizado.", show_alert=True)
        return
    await query.answer("Procesando eliminación…")
    with conectar_db() as conexion:
        anterior = conexion.execute(
            "SELECT 1 FROM auditoria_orma_acciones WHERE captura_id=? "
            "AND accion='ELIMINAR_MENSAJE_ORIGEN' AND resultado='OK' LIMIT 1",
            (captura_id,),
        ).fetchone()
    if anterior:
        estado = "✅ ELIMINADO DEL GRUPO DE ORIGEN"
    else:
        try:
            eliminado = await context.bot.delete_message(
                chat_id=int(captura["chat_id"]),
                message_id=int(captura["mensaje_origen_id"]),
            )
            if not eliminado:
                raise TelegramError("Telegram no confirmó la eliminación.")
        except TelegramError as error:
            try:
                registrar_auditoria_orma(
                    usuario.id, captura, "ELIMINAR_MENSAJE_ORIGEN",
                    grupo={"chat_id": captura["chat_id"], "username": captura["chat_username"],
                           "nombre": captura["chat_nombre"]},
                    detalle=f"message_id={captura['mensaje_origen_id']}",
                    resultado="ERROR", error=str(error),
                )
            except Exception:
                logging.exception("No se pudo auditar el fallo de borrado de origen")
            await context.bot.send_message(
                chat_id=GRUPO_PUBLICIDAD_NO_IDENTIFICADA_ID,
                text=("❌ No se confirmó la eliminación del mensaje "
                      f"{captura['mensaje_origen_id']} del grupo de origen.\n"
                      f"Motivo: {error}"),
                reply_to_message_id=query.message.message_id,
            )
            return
        # La auditoría no modifica perfiles, límites ni clientes editados.
        try:
            with conectar_db() as conexion:
                conexion.execute(
                    "INSERT INTO auditoria_orma_acciones "
                    "(propietario_id,captura_id,objetivo_tipo,objetivo_id,accion,chat_id,"
                    "chat_username,chat_nombre,detalle,resultado,error,fecha_evento) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (usuario.id, captura_id, captura["objetivo_tipo"], captura["objetivo_id"],
                     "ELIMINAR_MENSAJE_ORIGEN", captura["chat_id"], captura["chat_username"],
                     captura["chat_nombre"], f"message_id={captura['mensaje_origen_id']}",
                     "OK", None, datetime.now(timezone.utc).isoformat()),
                )
        except Exception:
            logging.exception("Mensaje eliminado, pero no se pudo persistir la auditoría")
        estado = "✅ ELIMINADO DEL GRUPO DE ORIGEN"
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛡 CONTROL INDIVIDUAL /ORMA", callback_data=f"orma_ficha:{captura_id}")],
        [InlineKeyboardButton(estado, callback_data=f"orma_borrar_origen:{captura_id}")],
    ])
    try:
        await query.edit_message_reply_markup(reply_markup=teclado)
    except TelegramError:
        logging.exception("No se pudo actualizar el botón de eliminación")
        await context.bot.send_message(
            chat_id=GRUPO_PUBLICIDAD_NO_IDENTIFICADA_ID,
            text=f"{estado} · Message ID: {captura['mensaje_origen_id']}",
        )


# Permisos nativos individuales: borrador privado, nunca permisos generales.
PERMISOS_INDIVIDUALES = (
    ("can_send_messages", "ENVIAR MENSAJES"),
    ("can_send_photos", "FOTOS"),
    ("can_send_videos", "VIDEOS"),
    ("can_send_video_notes", "VIDEOMENSAJES"),
    ("can_send_audios", "MÚSICA / AUDIO"),
    ("can_send_voice_notes", "MENSAJES DE VOZ"),
    ("can_send_documents", "ARCHIVOS"),
    ("can_send_other_messages", "STICKERS / GIF / INLINE"),
    ("can_add_web_page_previews", "VISTAS PREVIAS DE ENLACES"),
    ("can_send_polls", "ENCUESTAS"),
    ("can_react_to_messages", "REACCIONES"),
    ("can_invite_users", "AÑADIR USUARIOS"),
    ("can_pin_messages", "FIJAR MENSAJES"),
    ("can_edit_tag", "EDITAR ETIQUETA PROPIA"),
    ("can_change_info", "EDITAR INFORMACIÓN"),
    ("can_manage_topics", "GESTIONAR TEMAS"),
)
BORRADORES_PERMISOS = {}


def texto_permiso_borrador(valor):
    return "➖ MANTENER" if valor is None else ("✅ PERMITIR" if valor else "🚫 BLOQUEAR")


async def actualizar_grupos_usuario_permisos(bot, captura, estado):
    async def consultar(indice):
        grupo = grupo_orma_por_indice(indice)
        try:
            miembro = await bot.get_chat_member(grupo["chat_ref"], int(captura["objetivo_id"]))
            rol = str(miembro.status)
            if rol in {"administrator", "creator"}:
                return indice, "ADMINISTRADOR", True, False
            presente = estado_es_miembro(miembro)
            return indice, ("MIEMBRO" if presente else "NO ES MIEMBRO"), presente, presente
        except TelegramError:
            return indice, "NO COMPROBADO", False, False
    consultas = await asyncio.gather(*(consultar(i) for i in range(1, TOTAL_GRUPOS_OBLIGATORIOS + 1)))
    estado["estados_grupos"] = {i: etiqueta for i, etiqueta, _, _ in consultas}
    estado["presentes"] = {i for i, _, presente, _ in consultas if presente}
    estado["disponibles"] = {i for i, _, _, permitido in consultas if permitido}
    estado["grupos"].intersection_update(estado["disponibles"])


def resumen_membresia_selector_permisos(estado):
    errores = sum(v == "NO COMPROBADO" for v in estado["estados_grupos"].values())
    return (f"Miembro confirmado: <b>{len(estado['presentes'])}/21</b>\n"
        f"Grupos donde puede restringirse: <b>{len(estado['disponibles'])}</b>\n"
        f"Seleccionados: <b>{len(estado['grupos'])}/{len(estado['disponibles'])}</b>\n"
        f"Sin comprobar: <b>{errores}</b>\n"
        "Los grupos ausentes, administrados o no comprobados no se seleccionan.")


def teclado_grupos_permisos(captura_id, estado):
    filas = [[InlineKeyboardButton(f"✅ SELECCIONAR DISPONIBLES ({len(estado['disponibles'])})", callback_data=f"orma_permall:{captura_id}"),
              InlineKeyboardButton("⬜ NINGUNO", callback_data=f"orma_permnone:{captura_id}")]]
    for indice, nombre, _, _ in GRUPOS_OFICIALES:
        if indice in estado["disponibles"]:
            marca = "✅" if indice in estado["grupos"] else "⬜"
            etiqueta = f"{marca} {indice}. {nombre}"
            callback = f"orma_permg:{captura_id}:{indice}"
        else:
            etiqueta = f"🚫 {indice}. {estado['estados_grupos'][indice]} · {nombre}"
            callback = f"orma_permno:{captura_id}:{indice}"
        filas.append([InlineKeyboardButton(etiqueta[:60], callback_data=callback)])
    filas.extend([
        [InlineKeyboardButton("🔄 ACTUALIZAR MEMBRESÍA", callback_data=f"orma_permrefresh:{captura_id}")],
        [InlineKeyboardButton("CONTINUAR · PERMISOS ➡️", callback_data=f"orma_permnext:{captura_id}")],
        [InlineKeyboardButton("⬅️ FICHA / CANCELAR", callback_data=f"orma_ficha:{captura_id}")],
    ])
    return completar_navegacion_panel(InlineKeyboardMarkup(filas))


def teclado_editor_permisos(captura_id, estado):
    filas = [[InlineKeyboardButton("🎛 MULTIMEDIA · AJUSTE CONJUNTO", callback_data=f"orma_permmedia:{captura_id}")]]
    for numero, (campo, nombre) in enumerate(PERMISOS_INDIVIDUALES):
        filas.append([InlineKeyboardButton(f"{texto_permiso_borrador(estado['cambios'].get(campo))} · {nombre}", callback_data=f"orma_permt:{captura_id}:{numero}")])
    filas.extend([
        [InlineKeyboardButton("⏱ DURACIÓN", callback_data=f"orma_permtime:{captura_id}")],
        [InlineKeyboardButton("📋 REVISAR ANTES DE APLICAR", callback_data=f"orma_permreview:{captura_id}")],
        [InlineKeyboardButton("⬅️ GRUPOS", callback_data=f"orma_perm:{captura_id}")],
        [InlineKeyboardButton("❌ CANCELAR", callback_data=f"orma_ficha:{captura_id}")],
    ])
    return completar_navegacion_panel(InlineKeyboardMarkup(filas))


def texto_editor_permisos(captura, estado):
    duracion = estado["duracion"]
    plazo = "CONSERVAR PLAZO ACTUAL" if duracion == -1 else ("PERMANENTE" if duracion == 0 else texto_separacion(duracion))
    return ("🔐 <b>PERMISOS INDIVIDUALES DE TELEGRAM</b>\n\n"
        + cabecera_identidad_orma(captura)
        + f"\n\nGrupos seleccionados: <b>{len(estado['grupos'])}/21</b>\nDuración: <b>{plazo}</b>\n\n"
        "➖ MANTENER conserva el permiso actual de esa persona en cada grupo.\n"
        "Cada toque alterna MANTENER → BLOQUEAR → PERMITIR. Todavía no aplica cambios.\n"
        "PERMITIR está sujeto a los permisos generales del grupo; no los modifica.\n"
        "Telegram agrupa stickers, GIF, juegos y bots inline en un permiso. Enlaces controla la vista previa, no borra URLs.\n"
        "Fijar mensajes y editar información pueden no aplicarse en supergrupos públicos.\n"
        "La membresía 12/12 y el control publicitario siguen siendo independientes.")


def permisos_actuales_usuario(miembro, chat):
    estado = str(miembro.status)
    if estado in {"administrator", "creator"}:
        raise RuntimeError("No se modifican permisos de administradores o propietarios")
    if estado not in {"member", "restricted"}:
        raise RuntimeError("El usuario no es miembro del grupo")
    if estado == "restricted" and not bool(getattr(miembro, "is_member", False)):
        raise RuntimeError("El usuario restringido ya no pertenece al grupo")
    origen = miembro if estado == "restricted" else getattr(chat, "permissions", None)
    if origen is None:
        raise RuntimeError("No se pudieron consultar los permisos actuales")
    datos = {campo: bool(getattr(origen, campo, False)) for campo, _ in PERMISOS_INDIVIDUALES}
    for campo, dependencia in (("can_react_to_messages", "can_send_messages"), ("can_edit_tag", "can_pin_messages"), ("can_manage_topics", "can_pin_messages")):
        if getattr(origen, campo, None) is None:
            datos[campo] = datos[dependencia]
    return datos


async def aplicar_permisos_individuales(bot, propietario_id, captura, estado):
    resultados = []
    for indice in sorted(estado["grupos"]):
        grupo = grupo_orma_por_indice(indice)
        real = grupo
        try:
            chat = await bot.get_chat(grupo["chat_ref"])
            if chat.type != ChatType.SUPERGROUP:
                raise RuntimeError("Telegram requiere un supergrupo para esta restricción")
            real = {**grupo, "chat_id": chat.id, "nombre": chat.title, "username": chat.username}
            miembro = await bot.get_chat_member(chat.id, int(captura["objetivo_id"]))
            actuales = permisos_actuales_usuario(miembro, chat)
            cambios = dict(estado["cambios"])
            solicitados = {**actuales, **cambios}
            duracion = estado["duracion"]
            until_date = 0
            if duracion > 0:
                until_date = datetime.now(timezone.utc) + timedelta(seconds=duracion)
            elif duracion == -1 and str(miembro.status) == "restricted":
                until_date = getattr(miembro, "until_date", 0) or 0
                if isinstance(until_date, datetime) and until_date.timestamp() > 0:
                    if (until_date - datetime.now(timezone.utc)).total_seconds() < 30:
                        raise RuntimeError("La restricción actual está por vencer; selecciona una duración explícita")
            await bot.restrict_chat_member(chat_id=chat.id, user_id=int(captura["objetivo_id"]),
                permissions=ChatPermissions(**solicitados), until_date=until_date,
                use_independent_chat_permissions=True)
            despues = await bot.get_chat_member(chat.id, int(captura["objetivo_id"]))
            comprobados = permisos_actuales_usuario(despues, chat)
            diferentes = [nombre for campo, nombre in PERMISOS_INDIVIDUALES if comprobados[campo] != solicitados[campo]]
            if str(despues.status) == "restricted":
                fecha_real = getattr(despues, "until_date", 0) or 0
                fecha_real = fecha_real.timestamp() if isinstance(fecha_real, datetime) else int(fecha_real)
                fecha_pedida = until_date.timestamp() if isinstance(until_date, datetime) else int(until_date)
                if abs(fecha_real - fecha_pedida) > 3:
                    diferentes.append("DURACIÓN")
            detalle = "; ".join(f"{campo}={valor}" for campo, valor in cambios.items())
            if diferentes:
                raise RuntimeError("Solicitud enviada; Telegram no confirmó: " + ", ".join(diferentes))
            registrar_auditoria_orma(propietario_id, captura, "PERMISOS_INDIVIDUALES", grupo=real,
                detalle=detalle + f"; duración={duracion}", resultado="OK")
            resultados.append((True, grupo["nombre"], "APLICADOS Y VERIFICADOS"))
        except Exception as error:
            registrar_auditoria_orma(propietario_id, captura, "PERMISOS_INDIVIDUALES", grupo=real,
                detalle="No confirmado; revisar estado actual del grupo", resultado="ERROR", error=str(error))
            resultados.append((False, grupo["nombre"], str(error)))
    return resultados


async def permisos_individuales_callback(query, usuario, context, data):
    partes = data.split(":")
    try:
        captura_id = int(partes[1]); captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            raise ValueError
    except (ValueError, IndexError, TypeError):
        await query.answer("Ficha no disponible.", show_alert=True); return
    if captura["objetivo_tipo"] != "USUARIO" or captura["objetivo_es_bot"]:
        await query.answer("Esta función es para usuarios, no bots ni canales.", show_alert=True); return
    estado = BORRADORES_PERMISOS.get(usuario.id)
    if partes[0] == "orma_perm":
        if not estado or estado["captura_id"] != captura_id:
            estado = {"captura_id": captura_id, "grupos": set(), "cambios": {}, "duracion": -1, "confirmacion": False}
            BORRADORES_PERMISOS[usuario.id] = estado
    elif not estado or estado["captura_id"] != captura_id:
        await query.answer("Borrador caducado. Abre PERMISOS INDIVIDUALES nuevamente.", show_alert=True); return
    accion = partes[0]
    if accion == "orma_permno":
        try:
            indice = int(partes[2]); etiqueta = estado["estados_grupos"][indice]
        except (ValueError, IndexError, KeyError):
            etiqueta = "Grupo no disponible"
        await query.answer(f"No se selecciona este grupo: {etiqueta}", show_alert=True); return
    if accion in {"orma_perm", "orma_permall", "orma_permnone", "orma_permg", "orma_permrefresh"}:
        await actualizar_grupos_usuario_permisos(context.bot, captura, estado)
        if accion == "orma_permall": estado["grupos"] = set(estado["disponibles"])
        if accion == "orma_permnone": estado["grupos"] = set()
        if accion == "orma_permg":
            try:
                indice = int(partes[2]); assert 1 <= indice <= 12
            except (ValueError, IndexError, AssertionError):
                await query.answer("Grupo no válido.", show_alert=True); return
            if indice not in estado["disponibles"]:
                await query.answer("El usuario no es miembro comprobado o no puede restringirse aquí.", show_alert=True); return
            if indice in estado["grupos"]: estado["grupos"].remove(indice)
            else: estado["grupos"].add(indice)
        estado["confirmacion"] = False
        await query.answer()
        await safe_query_edit_message(query, "🔐 <b>PERMISOS INDIVIDUALES · SELECCIONAR GRUPOS</b>\n\n"
            + cabecera_identidad_orma(captura) + "\n\n" + resumen_membresia_selector_permisos(estado),
            parse_mode="HTML", reply_markup=teclado_grupos_permisos(captura_id, estado)); return
    if not estado["grupos"]:
        await query.answer("Selecciona al menos un grupo.", show_alert=True); return
    if accion == "orma_permt":
        try:
            numero = int(partes[2]); assert 0 <= numero < len(PERMISOS_INDIVIDUALES)
        except (ValueError, IndexError, AssertionError):
            await query.answer("Permiso no válido.", show_alert=True); return
        campo = PERMISOS_INDIVIDUALES[numero][0]
        valor = estado["cambios"].get(campo)
        if valor is None: estado["cambios"][campo] = False
        elif valor is False: estado["cambios"][campo] = True
        else: estado["cambios"].pop(campo, None)
    if accion == "orma_permmedia":
        campos = [campo for campo, _ in PERMISOS_INDIVIDUALES[1:11]]
        valores = [estado["cambios"].get(campo) for campo in campos]
        nuevo = True if all(v is False for v in valores) else (None if all(v is True for v in valores) else False)
        for campo in campos:
            if nuevo is None: estado["cambios"].pop(campo, None)
            else: estado["cambios"][campo] = nuevo
    if accion == "orma_permtime":
        await query.answer()
        filas = [[InlineKeyboardButton(nombre, callback_data=f"orma_permdur:{captura_id}:{segundos}")] for nombre, segundos in
            [("CONSERVAR PLAZO ACTUAL", -1), ("PERMANENTE", 0), ("10 MINUTOS", 600), ("1 HORA", 3600), ("24 HORAS", 86400), ("7 DÍAS", 604800)]]
        filas.append([InlineKeyboardButton("⬅️ PERMISOS", callback_data=f"orma_permnext:{captura_id}")])
        await safe_query_edit_message(query, "⏱ <b>DURACIÓN DE LA RESTRICCIÓN INDIVIDUAL</b>\n\nConservar mantiene el vencimiento previo de cada grupo; para nuevos usuarios será permanente.",
            parse_mode="HTML", reply_markup=completar_navegacion_panel(InlineKeyboardMarkup(filas))); return
    if accion == "orma_permdur":
        try:
            segundos = int(partes[2]); assert segundos in {-1, 0, 600, 3600, 86400, 604800}
        except (ValueError, IndexError, AssertionError):
            await query.answer("Duración no válida.", show_alert=True); return
        estado["duracion"] = segundos
    if accion == "orma_permreview":
        await actualizar_grupos_usuario_permisos(context.bot, captura, estado)
        if not estado["grupos"]:
            estado["confirmacion"] = False
            await query.answer("No quedan grupos comprobados seleccionados. Actualiza la membresía.", show_alert=True); return
        if not estado["cambios"]:
            await query.answer("No seleccionaste ningún cambio.", show_alert=True); return
        estado["confirmacion"] = True
        resumen = [f"• {nombre}: {texto_permiso_borrador(estado['cambios'][campo])}" for campo, nombre in PERMISOS_INDIVIDUALES if campo in estado["cambios"]]
        duracion = estado["duracion"]
        plazo = "CONSERVAR PLAZO ACTUAL" if duracion == -1 else ("PERMANENTE" if duracion == 0 else texto_separacion(duracion))
        await query.answer()
        await safe_query_edit_message(query, "⚠️ <b>CONFIRMAR PERMISOS INDIVIDUALES</b>\n\n"
            + f"Usuario: <b>{html.escape(str(captura['objetivo_nombre'] or '')[:120])}</b> · ID <code>{captura['objetivo_id']}</code>\n"
            + f"Duración: <b>{plazo}</b>\nGrupos: <b>{len(estado['grupos'])}/21</b>\n\nCAMBIOS:\n" + "\n".join(resumen)
            + "\n\nGRUPOS:\n" + "\n".join(html.escape(grupo_orma_por_indice(i)["nombre"]) for i in sorted(estado["grupos"])),
            parse_mode="HTML", reply_markup=completar_navegacion_panel(InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ APLICAR EN LOS GRUPOS ELEGIDOS", callback_data=f"orma_permgo:{captura_id}")],
                [InlineKeyboardButton("⬅️ EDITAR / CANCELAR", callback_data=f"orma_permnext:{captura_id}")]]))); return
    if accion == "orma_permgo":
        if not estado["confirmacion"] or not estado["cambios"]:
            await query.answer("Revisa y confirma los cambios antes de aplicarlos.", show_alert=True); return
        seleccion_anterior = set(estado["grupos"])
        await actualizar_grupos_usuario_permisos(context.bot, captura, estado)
        omitidos = seleccion_anterior - estado["grupos"]
        BORRADORES_PERMISOS.pop(usuario.id, None)
        await query.answer("Aplicando solo en grupos comprobados…")
        resultados = await aplicar_permisos_individuales(context.bot, usuario.id, captura, estado)
        for indice in sorted(omitidos):
            grupo = grupo_orma_por_indice(indice)
            motivo = "OMITIDO · " + estado["estados_grupos"][indice]
            registrar_auditoria_orma(usuario.id, captura, "PERMISOS_GRUPO_OMITIDO", grupo=grupo,
                detalle=motivo, resultado="OMITIDO")
            resultados.append((False, grupo["nombre"], motivo))
        await safe_query_edit_message(query, "🔐 <b>RESULTADO POR GRUPO</b>\n\n" + texto_resultados_moderacion(resultados), parse_mode="HTML",
            reply_markup=completar_navegacion_panel(InlineKeyboardMarkup([
                [InlineKeyboardButton("🔐 NUEVO AJUSTE", callback_data=f"orma_perm:{captura_id}")],
                [InlineKeyboardButton("⬅️ FICHA", callback_data=f"orma_ficha:{captura_id}")]]))); return
    estado["confirmacion"] = False
    await query.answer()
    await safe_query_edit_message(query, texto_editor_permisos(captura, estado), parse_mode="HTML", reply_markup=teclado_editor_permisos(captura_id, estado))


CALLBACKS_FICHA = ('orma_ficha_pagina:', 'orma_ficha:', 'orma_membresia:', 'orma_actividad:', 'orma_movimientos:', 'orma_audit:', 'orma_pg_lista:', 'orma_pg:', 'orma_pgm:', 'orma_pgmset:', 'orma_pgs:', 'orma_pgsset:', 'orma_pgl:', 'orma_pgnolim:', 'orma_pgt:', 'orma_pgtog:', 'orma_pgr:', 'orma_pgin:', 'orma_mod:', 'orma_modacc:', 'orma_modtog:', 'orma_modall:', 'orma_modnone:', 'orma_modnext:', 'orma_moddur:', 'orma_modgo:', 'orma_publicidad:', 'orma_pub_modo:', 'orma_pub_setmodo:', 'orma_pub_sep:', 'orma_pub_setsep:', 'orma_pub_limites:', 'orma_pub_sinlimites:', 'orma_pub_tipos:', 'orma_pub_texto_simple:', 'orma_pub_texto_apply:', 'orma_pub_toggle:', 'orma_pub_reset:', 'orma_pub_input:', 'orma_pub_cancelinput:')
CAMPOS_TIPOS_PANEL = ('controlar_foto', 'controlar_video', 'controlar_gif', 'controlar_documento', 'controlar_enlace', 'controlar_custom_emoji', 'controlar_texto_simple')


async def orma_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    usuario = update.effective_user
    if not query or not usuario:
        return

    if not es_administrador_maximo(usuario):
        await query.answer("Acceso no autorizado.", show_alert=True)
        return

    # La entrada manual debe volver al panel privado realmente pulsado.
    if query.message and query.message.chat_id == usuario.id:
        registrar_panel_reemplazo(usuario.id, query.message)

    data = query.data or ""

    if data.startswith(("orma_perm:", "orma_permall:", "orma_permnone:", "orma_permg:", "orma_permnext:", "orma_permt:", "orma_permmedia:", "orma_permtime:", "orma_permdur:", "orma_permreview:", "orma_permgo:", "orma_permrefresh:", "orma_permno:")):
        ENTRADAS_CONTROL_PUBLICIDAD.pop(usuario.id, None)
        ENTRADAS_ORMA_TOTAL.pop(usuario.id, None)
        ENTRADAS_RAIZ.pop(usuario.id, None)
        await permisos_individuales_callback(query, usuario, context, data)
        return
    BORRADORES_PERMISOS.pop(usuario.id, None)


    ENTRADAS_CONTROL_PUBLICIDAD.pop(usuario.id, None)
    ENTRADAS_ORMA_TOTAL.pop(usuario.id, None)
    ENTRADAS_RAIZ.pop(usuario.id, None)
    if data.startswith(CALLBACKS_FICHA):
        try:
            captura_control = obtener_captura_orma(int(data.split(":")[1]))
            if not captura_pertenece_propietario(captura_control, usuario.id):
                raise ValueError
        except (ValueError, IndexError, TypeError):
            await query.answer("Ficha no disponible. Abre una ficha actual.", show_alert=True)
            return
        partes = data.split(":")
        if data.startswith(("orma_pub_input:", "orma_pgin:")) and partes[-1] not in {"separacion_minutos", "limite_hora", "limite_dia", "limite_semana", "limite_mes", "limite_anio"}:
            await query.answer("Campo no válido.", show_alert=True)
            return
        if data.startswith(("orma_modacc:", "orma_modall:", "orma_modnone:", "orma_modnext:", "orma_modgo:")) and partes[-1] not in {"MUTE", "UNMUTE", "BAN", "UNBAN", "EXPULSAR"}:
            await query.answer("Acción no válida.", show_alert=True)
            return
        if data.startswith(("orma_pg:", "orma_pgm", "orma_pgs", "orma_pgl", "orma_pgnolim:", "orma_pgt", "orma_pgr:", "orma_pgin:")):
            try:
                if not 1 <= int(partes[2]) <= TOTAL_GRUPOS_OBLIGATORIOS:
                    raise ValueError
            except (ValueError, IndexError):
                await query.answer("Grupo no válido.", show_alert=True)
                return
        if data.startswith(("orma_pgtog:", "orma_pub_toggle:")) and partes[-1] not in CAMPOS_TIPOS_PANEL:
            await query.answer("Tipo no válido.", show_alert=True)
            return
        if data.startswith(("orma_pgmset:", "orma_pub_setmodo:")) and partes[-1] not in {"HEREDADO", "PERSONALIZADO", "ILIMITADO", "BLOQUEADO", "EXCLUIDO"}:
            await query.answer("Modo no válido.", show_alert=True)
            return


    if data == "orma_cerrar":
        ENTRADAS_CONTROL_PUBLICIDAD.pop(usuario.id, None)
        ENTRADAS_ORMA_TOTAL.pop(usuario.id, None)
        ENTRADAS_RAIZ.pop(usuario.id, None)
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
        ENTRADAS_RAIZ.pop(usuario.id, None)
        SELECCIONES_MODERACION_ORMA.pop(usuario.id, None)
        await query.answer()
        try:
            await safe_query_edit_message(
                query,
                texto_menu_principal_orma(),
                parse_mode="HTML",
                reply_markup=teclado_menu_principal_orma(),
            )
        except TelegramError:
            pass
        return

    if data == "orma_raiz":
        ENTRADAS_RAIZ.pop(usuario.id, None)
        await query.answer()
        await safe_query_edit_message(query, texto_centro_control_raiz(), parse_mode="HTML", reply_markup=teclado_centro_control_raiz())
        return

    if data == "orma_raiz_membresia":
        await query.answer()
        await safe_query_edit_message(query, texto_membresia_raiz(), parse_mode="HTML", reply_markup=teclado_membresia_raiz())
        return

    if data == "orma_raiz_membresia_toggle":
        estado = alternar_config_raiz("membresia_7de7_activa")
        await query.answer("Membresía 12/12 activada" if estado else "Membresía 12/12 pausada")
        await safe_query_edit_message(query, texto_membresia_raiz(), parse_mode="HTML", reply_markup=teclado_membresia_raiz())
        return

    if data == "orma_raiz_grupos":
        await query.answer()
        await safe_query_edit_message(query, texto_grupos_raiz(), parse_mode="HTML", reply_markup=teclado_grupos_raiz())
        return

    if data.startswith("orma_raiz_grupo:"):
        username_grupo = data.split(":", 1)[1]
        grupo = obtener_grupo_raiz_db(username_grupo)
        await query.answer()
        if not grupo:
            await safe_query_edit_message(query, "⚠️ Grupo de raíz no encontrado.", parse_mode="HTML", reply_markup=teclado_grupos_raiz())
            return
        await safe_query_edit_message(
            query,
            texto_grupo_raiz(grupo),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                teclado_navegacion_raiz(
                    "orma_raiz_grupos",
                    "⬅️ 12 GRUPOS OFICIALES",
                )
            ),
        )
        return

    if data == "orma_raiz_bots":
        ENTRADAS_RAIZ.pop(usuario.id, None)
        await query.answer()
        await safe_query_edit_message(query, texto_bots_exentos_raiz(), parse_mode="HTML", reply_markup=teclado_bots_exentos_raiz())
        return

    if data == "orma_raiz_bot_protegido":
        await query.answer("Bot estructural protegido: la exención no puede retirarse desde el panel", show_alert=True)
        return

    if data == "orma_raiz_bot_agregar":
        ENTRADAS_RAIZ[usuario.id] = {"accion": "AGREGAR_BOT_EXENTO"}
        await query.answer()
        await safe_query_edit_message(
            query,
            "➕ <b>AGREGAR BOT EXENTO</b>\n\nEscribe el <b>@username</b> del bot que deseas autorizar.\n\nEl mensaje se eliminará automáticamente después de procesarlo.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCELAR", callback_data="orma_raiz_bots")]]),
        )
        return

    if data.startswith("orma_raiz_bot_quitar:"):
        username_bot = data.split(":", 1)[1]
        if username_bot in BOTS_OFICIALES_EXENTOS:
            await query.answer("Bot estructural protegido", show_alert=True)
            return
        await query.answer()
        await safe_query_edit_message(
            query,
            f"⚠️ <b>QUITAR EXENCIÓN</b>\n\n¿Retirar la exención de <b>@{html.escape(username_bot)}</b>?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ SÍ, QUITAR", callback_data=f"orma_raiz_bot_quitar_ok:{username_bot}")],
                [InlineKeyboardButton("❌ CANCELAR", callback_data="orma_raiz_bots")],
            ]),
        )
        return

    if data.startswith("orma_raiz_bot_quitar_ok:"):
        username_bot = data.split(":", 1)[1]
        quitado = quitar_bot_exento_raiz(username_bot)
        await query.answer("Exención retirada" if quitado else "No se modificó el bot")
        await safe_query_edit_message(query, texto_bots_exentos_raiz(), parse_mode="HTML", reply_markup=teclado_bots_exentos_raiz())
        return

    if data == "orma_raiz_tiempos":
        await query.answer()
        await safe_query_edit_message(query, texto_tiempos_raiz(), parse_mode="HTML", reply_markup=teclado_tiempos_raiz())
        return

    if data == "orma_raiz_tiempos_membresia":
        actual = obtener_config_raiz_entero("aviso_membresia_segundos", AVISO_MEMBRESIA_SEGUNDOS)
        await query.answer()
        await safe_query_edit_message(
            query,
            "🛡 <b>AVISO DE MEMBRESÍA</b>\n"
            + sello_version_panel()
            + f"\n\nActual: <b>{actual} segundos</b>\nSelecciona el nuevo tiempo:",
            parse_mode="HTML",
            reply_markup=teclado_selector_tiempo_raiz("membresia"),
        )
        return

    if data == "orma_raiz_tiempos_publicidad":
        actual = obtener_config_raiz_entero("aviso_publicidad_segundos", AVISO_PUBLICIDAD_SEGUNDOS)
        await query.answer()
        await safe_query_edit_message(
            query,
            "📢 <b>AVISO PUBLICITARIO</b>\n"
            + sello_version_panel()
            + f"\n\nActual: <b>{actual} segundos</b>\nSelecciona el nuevo tiempo:",
            parse_mode="HTML",
            reply_markup=teclado_selector_tiempo_raiz("publicidad"),
        )
        return

    if data.startswith("orma_raiz_tiempo:"):
        try:
            _, tipo_tiempo, segundos_txt = data.split(":", 2)
            segundos = int(segundos_txt)
        except (TypeError, ValueError):
            await query.answer("Valor no válido", show_alert=True)
            return
        if tipo_tiempo not in {"membresia", "publicidad"} or segundos not in {15, 30, 60, 120}:
            await query.answer("Tiempo no autorizado", show_alert=True)
            return
        clave = "aviso_membresia_segundos" if tipo_tiempo == "membresia" else "aviso_publicidad_segundos"
        guardar_config_raiz(clave, segundos)
        await query.answer(f"Tiempo actualizado: {segundos} s")
        await safe_query_edit_message(query, texto_tiempos_raiz(), parse_mode="HTML", reply_markup=teclado_tiempos_raiz())
        return

    if data == "orma_raiz_pruebas":
        await query.answer()
        await safe_query_edit_message(query, texto_pruebas_raiz(), parse_mode="HTML", reply_markup=teclado_pruebas_raiz())
        return

    if data == "orma_raiz_pruebas_toggle":
        estado = alternar_config_raiz("grupo_pruebas_activo")
        await query.answer("Grupo de pruebas bajo control" if estado else "Grupo de pruebas fuera de control")
        await safe_query_edit_message(query, texto_pruebas_raiz(), parse_mode="HTML", reply_markup=teclado_pruebas_raiz())
        return

    if data == "orma_raiz_reglas_protegidas":
        await query.answer()
        await safe_query_edit_message(
            query,
            texto_reglas_protegidas_raiz(),
            parse_mode="HTML",
            reply_markup=teclado_reglas_protegidas_raiz(),
        )
        return

    if data == "orma_raiz_auditoria":
        await query.answer()
        await safe_query_edit_message(
            query,
            texto_auditoria_raiz(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(teclado_navegacion_raiz()),
        )
        return

    if data == "orma_raiz_nop":
        await query.answer()
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
        if not await mostrar_ficha_orma_privada(context.bot, usuario.id, captura_id):
            await safe_query_edit_message(query, "No se pudo abrir la ficha. Puedes volver a intentar desde CLIENTES EDITADOS.",
                reply_markup=completar_navegacion_panel(InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ CLIENTES EDITADOS", callback_data="orma_clientes_editados:0")]])))
        return

    if data.startswith("orma_ficha_pagina:"):
        try:
            _, captura_txt, pagina_txt = data.split(":")
            captura_id, pagina = int(captura_txt), int(pagina_txt)
            if pagina < 0:
                raise ValueError
        except (ValueError, TypeError):
            await query.answer("Página no válida.", show_alert=True)
            return
        await query.answer()
        if not await mostrar_ficha_orma_privada(context.bot, usuario.id, captura_id, pagina):
            await safe_query_edit_message(query, "No se pudo abrir la ficha. Puedes volver a intentar desde CLIENTES EDITADOS.",
                reply_markup=completar_navegacion_panel(InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ CLIENTES EDITADOS", callback_data="orma_clientes_editados:0")]])))
        return

    if data.startswith("orma_ficha:"):
        ENTRADAS_CONTROL_PUBLICIDAD.pop(usuario.id, None)
        try:
            captura_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer()
            return
        await query.answer()
        if not await mostrar_ficha_orma_privada(context.bot, usuario.id, captura_id):
            await safe_query_edit_message(query, "No se pudo abrir la ficha. Puedes volver a intentar desde CLIENTES EDITADOS.",
                reply_markup=completar_navegacion_panel(InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ CLIENTES EDITADOS", callback_data="orma_clientes_editados:0")]])))
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
                "Esta identidad es un canal/chat y no puede evaluarse con la regla de usuario 12/12."
            )
        else:
            estado = await obtener_estado_membresia_7de7(captura["objetivo_id"])
            marcas = estado_membresia_por_username(estado)
            lineas = [
                "🔐 <b>MEMBRESÍA 21/21 · DETALLE</b>",
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
              "independientes. HEREDADO significa que obedece al control global 12/12.",
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
            "El texto simple respeta la excepción configurada. La membresía 12/12 sigue siendo independiente.",
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
        elif modo == "PERSONALIZADO" and str(obtener_control_grupo_db(captura["objetivo_tipo"], captura["objetivo_id"], grupo["chat_id"])["modo"]).upper() == "HEREDADO":
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
        if not captura_pertenece_propietario(captura, usuario.id) or not grupo or grupo["chat_id"] is None:
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
        if not captura_pertenece_propietario(captura, usuario.id) or not grupo or grupo["chat_id"] is None:
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
        if not captura_pertenece_propietario(captura, usuario.id) or not grupo or grupo["chat_id"] is None:
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
        if not captura_pertenece_propietario(captura, usuario.id) or not grupo or grupo["chat_id"] is None:
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
            "El texto simple respeta la excepción de la regla efectiva. ILIMITADO/EXCLUIDO lo liberan; la membresía 12/12 sigue siendo independiente.",
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
                [InlineKeyboardButton(f"{marca('controlar_texto_simple')} TEXTO SIMPLE", callback_data=f"orma_pgtog:{captura_id}:{indice}:controlar_texto_simple")],
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
        if not captura_pertenece_propietario(captura, usuario.id) or not grupo or grupo["chat_id"] is None:
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
            "El texto simple respeta la excepción de la regla efectiva. ILIMITADO/EXCLUIDO lo liberan; la membresía 12/12 sigue siendo independiente.",
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
                [InlineKeyboardButton(f"{marca2('controlar_texto_simple')} TEXTO SIMPLE", callback_data=f"orma_pgtog:{captura_id}:{indice}:controlar_texto_simple")],
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
        if not captura_pertenece_propietario(captura, usuario.id) or not grupo or grupo["chat_id"] is None:
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
            "🛡️ <b>CONTROL TOTAL · MODERACIÓN 21/21</b>\n\n"
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
            + "\n\nSelecciona uno, varios o TODOS 21/21.",
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
        estado["grupos"] = set(range(1, TOTAL_GRUPOS_OBLIGATORIOS + 1))
        await query.answer("Seleccionados 21/21")
        captura = obtener_captura_orma(captura_id)
        await safe_query_edit_message(
            query,
            f"{texto_accion_moderacion(accion)} · <b>SELECCIONAR GRUPOS</b>\n\n"
            + cabecera_identidad_orma(captura)
            + "\n\nSeleccionados: <b>21/21</b>",
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

        grupos_ejecucion = set(estado["grupos"])
        duracion_ejecucion = estado.get("duracion_segundos")
        SELECCIONES_MODERACION_ORMA.pop(usuario.id, None)
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
            grupos_ejecucion,
            duracion_segundos=duracion_ejecucion,
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
            "⚙️ <b>MODO DE CONTROL</b>\n\n"
            "HEREDADO: usará la regla global cuando la activemos.\n"
            "PERSONALIZADO: aplica límites propios.\n"
            "ILIMITADO: registra pero no limita.\n"
            "BLOQUEADO: elimina toda publicidad controlable.\n"
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
            "⏱ <b>SEPARACIÓN ENTRE PUBLICIDADES</b>\n\n"
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
            "🔢 <b>LÍMITES DE PUBLICIDAD</b>\n\n"
            f"Hora: <b>{texto_valor_limite(cfg['limite_hora'])}</b>\n"
            f"Día: <b>{texto_valor_limite(cfg['limite_dia'])}</b>\n"
            f"Semana: <b>{texto_valor_limite(cfg['limite_semana'])}</b>\n"
            f"Mes: <b>{texto_valor_limite(cfg['limite_mes'])}</b>\n"
            f"Año: <b>{texto_valor_limite(cfg['limite_anio'])}</b>\n\n"
            "Pulsa un periodo y escribe el máximo.\n"
            "🔐 <b>0 = ninguna publicación</b> en ese período + acceso a Membresía Publicitaria.\n"
            "♾ <b>SIN LÍMITE</b> continúa siendo una condición distinta.\n\n"
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

        await safe_query_edit_message(
            query,
            texto_tipos_publicidad_identidad(cfg),
            parse_mode="HTML",
            reply_markup=teclado_tipos_publicidad_identidad(captura_id, cfg),
        )
        return

    if data.startswith("orma_pub_texto_simple:"):
        captura_id = int(data.split(":", 1)[1])
        captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            await query.answer("Ficha no disponible.", show_alert=True)
            return

        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )
        activo = bool(cfg["controlar_texto_simple"])
        await query.answer()

        if activo:
            texto = (
                "💬 <b>TEXTO SIMPLE · EXCEPCIÓN INDIVIDUAL</b>\n\n"
                "Actualmente esta identidad tiene el texto simple "
                "<b>RESTRINGIDO</b> y se le solicita Membresía Publicitaria.\n\n"
                "¿Deseas devolver su texto simple al estado libre?"
            )
            boton = InlineKeyboardButton(
                "🔓 SÍ, DEJAR TEXTO LIBRE",
                callback_data=f"orma_pub_texto_apply:{captura_id}:0",
            )
        else:
            texto = (
                "💬 <b>TEXTO SIMPLE · EXCEPCIÓN INDIVIDUAL</b>\n\n"
                "La regla general del bot mantiene el texto normal puro libre.\n\n"
                "Si activas esta excepción, <b>solo esta identidad</b> tendrá "
                "restringido el texto simple en los grupos controlados: sus "
                "mensajes de texto puro serán eliminados y recibirá el acceso "
                "a <b>Membresía Publicitaria</b>.\n\n"
                "No afecta a ningún otro usuario ni modifica la raíz 12/12."
            )
            boton = InlineKeyboardButton(
                "🔒 ACTIVAR Y EXIGIR MEMBRESÍA",
                callback_data=f"orma_pub_texto_apply:{captura_id}:1",
            )

        await safe_query_edit_message(
            query,
            texto,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [boton],
                [
                    InlineKeyboardButton(
                        "⬅️ CANCELAR",
                        callback_data=f"orma_pub_tipos:{captura_id}",
                    )
                ],
            ]),
        )
        return

    if data.startswith("orma_pub_texto_apply:"):
        _, captura_txt, valor_txt = data.split(":", 2)
        captura_id = int(captura_txt)
        captura = obtener_captura_orma(captura_id)
        if not captura_pertenece_propietario(captura, usuario.id):
            await query.answer("Ficha no disponible.", show_alert=True)
            return

        nuevo = 1 if valor_txt == "1" else 0
        actualizar_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
            controlar_texto_simple=nuevo,
        )
        await query.answer(
            "Texto simple restringido · Membresía requerida"
            if nuevo
            else "Texto simple libre"
        )
        cfg = obtener_control_identidad_db(
            captura["objetivo_tipo"],
            captura["objetivo_id"],
        )
        await safe_query_edit_message(
            query,
            texto_tipos_publicidad_identidad(cfg),
            parse_mode="HTML",
            reply_markup=teclado_tipos_publicidad_identidad(captura_id, cfg),
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

        await safe_query_edit_message(
            query,
            texto_tipos_publicidad_identidad(cfg),
            parse_mode="HTML",
            reply_markup=teclado_tipos_publicidad_identidad(captura_id, cfg),
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
            "✍️ <b>VALOR PERSONALIZADO</b>\n\n"
            f"Escribe ahora el <b>{etiqueta}</b>.\n\n"
            "Envía un número entero igual o mayor que 0.\n\n"
            "• <b>0</b> = ninguna publicación permitida en ese período; "
            "al intentar publicar se mostrará el acceso a Membresía Publicitaria.\n"
            "• <b>SIN LÍMITE</b> = se configura desde «QUITAR TODOS LOS LÍMITES».\n"
            "• <b>BLOQUEADO</b> = bloqueo administrativo total y es una condición distinta.\n\n"
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



async def eliminar_mensaje_anti_evasion_estricto(context, mensaje, chat):
    """Doble intento de eliminación exclusivo para la barrera anti-evasión."""
    try:
        await mensaje.delete()
        return True, "ELIMINADA_PRIMER_INTENTO", None
    except TelegramError as error_1:
        logging.warning(
            "Anti-evasión primer intento falló chat=%s message=%s: %s",
            chat.id,
            getattr(mensaje, "message_id", None),
            error_1,
        )

    try:
        await context.bot.delete_message(
            chat_id=chat.id,
            message_id=mensaje.message_id,
        )
        return True, "ELIMINADA_SEGUNDO_INTENTO", str(error_1)
    except TelegramError as error_2:
        logging.exception(
            "ANTI_EVASION BLOQUEO FALLIDO chat=%s message=%s",
            chat.id,
            getattr(mensaje, "message_id", None),
        )
        return (
            False,
            "FALLO_ELIMINACION",
            f"primer_intento={error_1}; segundo_intento={error_2}",
        )


async def control_anti_evasion_spam(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Barrera temprana anti-evasión.

    Se ejecuta antes de membresía/publicidad y corta únicamente vectores
    de ejecución externa de bots no exentos:
    - bot que aparece como remitente pero está fuera del grupo;
    - mensajes enviados vía un bot externo (via_bot);
    - guest_message / guest bot caller no autorizado (PTB 22.8+).

    No altera /orma, CLIENTES EDITADOS, membresía 12/12 ni límites.
    """
    mensaje = update.effective_message
    chat = update.effective_chat

    if (
        mensaje is None
        or chat is None
        or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}
        or not es_grupo_controlado(chat)
    ):
        return

    usuario = getattr(mensaje, "from_user", None)
    via_bot = getattr(mensaje, "via_bot", None)
    guest_user = getattr(mensaje, "guest_bot_caller_user", None)
    guest_chat = getattr(mensaje, "guest_bot_caller_chat", None)
    es_guest_message = getattr(update, "guest_message", None) is not None

    # Nunca interceptar identidades estructurales/autorizadas.
    if usuario is not None and es_bot_oficial_exento(usuario):
        return
    if via_bot is not None and es_bot_oficial_exento(via_bot):
        return
    if guest_user is not None and es_bot_oficial_exento(guest_user):
        return

    motivos = []
    candidatos_ban = set()

    # Un bot visible como remitente solo se considera evasión temprana si
    # Telegram confirma que no pertenece al grupo actual.
    if usuario is not None and bool(getattr(usuario, "is_bot", False)):
        try:
            miembro = await context.bot.get_chat_member(chat.id, usuario.id)
            estado = str(getattr(miembro, "status", "") or "").lower()
            if estado in {"left", "kicked"}:
                motivos.append("BOT_FUERA_DEL_GRUPO")
                candidatos_ban.add(int(usuario.id))
        except TelegramError as error:
            # No se sanciona por un fallo transitorio de Telegram.
            logging.warning(
                "Anti-evasión: no se pudo verificar bot=%s chat=%s: %s",
                getattr(usuario, "id", None),
                chat.id,
                error,
            )

    # Los mensajes enviados a través de un bot externo no autorizado se
    # eliminan antes de que puedan entrar a membresía/publicidad.
    if via_bot is not None and bool(getattr(via_bot, "is_bot", False)):
        motivos.append("VIA_BOT_EXTERNO")
        candidatos_ban.add(int(via_bot.id))

    # Telegram/PTB 22.8 expone guest_message y los caller del bot invitado.
    if es_guest_message:
        motivos.append("GUEST_MESSAGE_EXTERNO")

    if guest_user is not None:
        motivos.append("GUEST_BOT_CALLER_USER")
        if bool(getattr(guest_user, "is_bot", False)):
            candidatos_ban.add(int(guest_user.id))

    if guest_chat is not None:
        motivos.append("GUEST_BOT_CALLER_CHAT")

    if not motivos:
        return

    eliminado, estado_eliminacion, error_eliminacion = (
        await eliminar_mensaje_anti_evasion_estricto(
            context=context,
            mensaje=mensaje,
            chat=chat,
        )
    )

    # Segunda barrera: solo se intenta banear identidades bot no exentas.
    for bot_id in sorted(candidatos_ban):
        if bot_id <= 0 or bot_id == GROUP_ANONYMOUS_BOT_ID:
            continue
        try:
            await context.bot.ban_chat_member(
                chat_id=chat.id,
                user_id=bot_id,
                revoke_messages=True,
            )
        except TelegramError as error:
            logging.info(
                "Anti-evasión: ban no aplicable bot=%s chat=%s: %s",
                bot_id,
                chat.id,
                error,
            )

    logging.warning(
        "ANTI_EVASION_SPAM chat=%s message=%s from_user=%s via_bot=%s "
        "guest_user=%s guest_chat=%s motivos=%s eliminacion=%s error=%s",
        chat.id,
        getattr(mensaje, "message_id", None),
        getattr(usuario, "id", None),
        getattr(via_bot, "id", None),
        getattr(guest_user, "id", None),
        getattr(guest_chat, "id", None),
        ",".join(motivos),
        estado_eliminacion,
        error_eliminacion,
    )

    # Impide que el mismo update continúe hacia membresía y genere el aviso 0/7.
    raise ApplicationHandlerStop




async def eliminar_publicidad_estricta(context, mensaje, chat, identidad_id):
    """Elimina solo la publicación; nunca banea, mutea ni restringe al usuario."""
    try:
        await mensaje.delete()
        return True, "ELIMINADA_PRIMER_INTENTO"
    except TelegramError as error_1:
        logging.warning(
            "Primer intento de eliminación falló identidad=%s chat=%s message=%s: %s",
            identidad_id, chat.id, mensaje.message_id, error_1,
        )

    try:
        await context.bot.delete_message(chat_id=chat.id, message_id=mensaje.message_id)
        return True, "ELIMINADA_SEGUNDO_INTENTO"
    except TelegramError as error_2:
        logging.exception(
            "BLOQUEO FALLIDO identidad=%s chat=%s message=%s",
            identidad_id, chat.id, mensaje.message_id,
        )
        return False, f"FALLO_ELIMINACION: {error_2}"


async def replicar_actividad_humana_supervision(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Replica solo actividad humana que supera 12/12 y controles previos."""
    mensaje = update.effective_message
    chat = update.effective_chat

    if context.chat_data.pop(f"supervision_bloqueada:{getattr(mensaje, 'message_id', 0)}", False):
        return

    if (
        not mensaje
        or not chat
        or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}
        or username_chat(chat) not in usernames_grupos_oficiales()
    ):
        return

    usuario = mensaje.from_user
    sender_chat = mensaje.sender_chat

    if usuario is not None and es_bot_oficial_exento(usuario):
        return

    # R014: comprobación defensiva real de membresía en el instante de la captura.
    # Solo una identidad humana 12/12 puede entrar en PUBLICIDAD NO IDENTIFICADA.
    if usuario is None or usuario.is_bot:
        return
    estado_membresia = await obtener_estado_membresia_7de7(usuario.id)
    if not estado_membresia.get("completo"):
        return

    identidad_tipo = "USUARIO"
    identidad_id = int(usuario.id)
    username = usuario.username
    nombre = nombre_visible_usuario(usuario)
    usuario_txt = f"@{username}" if username else "Sin @username"
    fecha_peru = datetime.now(ZONA_PERU).strftime("%d/%m/%Y %H:%M:%S")

    # Reutiliza la ficha /orma existente: no crea un segundo sistema de control.
    captura_id = guardar_captura_orma(
        ORMA_ADMIN_USER_ID or ADMIN_USER_ID,
        identidad_tipo, identidad_id, username, nombre, False,
        chat, mensaje.message_id,
    )
    teclado_supervision = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🛡 CONTROL INDIVIDUAL /ORMA",
            callback_data=f"orma_ficha:{captura_id}",
        )
    ], [InlineKeyboardButton(
        "🗑 ELIMINAR ESTE MENSAJE",
        callback_data=f"orma_borrar_origen:{captura_id}",
    )]])

    cabecera = (
        "🔎 <b>ACTIVIDAD NO IDENTIFICADA</b>\n"
        f"👤 <b>Identidad:</b> {html.escape(str(nombre))}\n"
        f"🏷 <b>Usuario:</b> {html.escape(usuario_txt)}\n"
        f"🆔 <b>USUARIO ID:</b> <code>{identidad_id}</code>\n"
        "✅ <b>Membresía:</b> 12/12 COMPLETA\n"
        f"📍 <b>Grupo:</b> {html.escape(str(chat.title or username_chat(chat) or chat.id))}\n"
        f"🆔 <b>Chat ID:</b> <code>{chat.id}</code>\n"
        f"💬 <b>Message ID:</b> <code>{mensaje.message_id}</code>\n"
        f"🕐 <b>Fecha:</b> {fecha_peru}"
    )

    try:
        await context.bot.send_message(
            chat_id=GRUPO_PUBLICIDAD_NO_IDENTIFICADA_ID,
            text=cabecera,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=teclado_supervision,
        )
        await context.bot.copy_message(
            chat_id=GRUPO_PUBLICIDAD_NO_IDENTIFICADA_ID,
            from_chat_id=chat.id,
            message_id=mensaje.message_id,
        )
    except TelegramError:
        logging.exception(
            "Supervisión no pudo replicar chat=%s message=%s identidad=%s:%s",
            chat.id, mensaje.message_id, identidad_tipo, identidad_id,
        )


async def replicar_publicacion_restringida(
    context,
    mensaje,
    chat,
    usuario,
    estado_membresia,
    motivo,
):
    """R020: replica al grupo privado solo mensajes de usuarios que serán restringidos."""
    if not mensaje or not chat or usuario is None or getattr(usuario, "is_bot", False):
        return

    identidad_id = int(usuario.id)
    username = usuario.username
    nombre = nombre_visible_usuario(usuario)
    usuario_txt = f"@{username}" if username else "Sin @username"
    fecha_peru = datetime.now(ZONA_PERU).strftime("%d/%m/%Y %H:%M:%S")

    completados = len((estado_membresia or {}).get("completados") or [])
    total = int((estado_membresia or {}).get("total") or TOTAL_GRUPOS_OBLIGATORIOS)
    progreso = f"{completados}/{total}"
    if (estado_membresia or {}).get("completo"):
        progreso += " COMPLETA"

    captura_id = guardar_captura_orma(
        ORMA_ADMIN_USER_ID or ADMIN_USER_ID,
        "USUARIO", identidad_id, username, nombre, False,
        chat, mensaje.message_id,
    )
    teclado = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🛡 CONTROL INDIVIDUAL /ORMA",
            callback_data=f"orma_ficha:{captura_id}",
        )
    ]])

    cabecera = (
        "🔴 <b>PUBLICACIÓN RESTRINGIDA</b>\n"
        f"👤 <b>Identidad:</b> {html.escape(str(nombre))}\n"
        f"🏷 <b>Usuario:</b> {html.escape(usuario_txt)}\n"
        f"🆔 <b>USUARIO ID:</b> <code>{identidad_id}</code>\n"
        f"✅ <b>Membresía:</b> {html.escape(progreso)}\n"
        f"📍 <b>Grupo:</b> {html.escape(str(chat.title or username_chat(chat) or chat.id))}\n"
        f"🆔 <b>Chat ID:</b> <code>{chat.id}</code>\n"
        f"💬 <b>Message ID:</b> <code>{mensaje.message_id}</code>\n"
        f"🛡 <b>Motivo:</b> {html.escape(str(motivo or 'RESTRINGIDA'))}\n"
        f"🕐 <b>Fecha:</b> {fecha_peru}"
    )

    try:
        await context.bot.send_message(
            chat_id=GRUPO_PUBLICACIONES_USUARIOS_ID,
            text=cabecera,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=teclado,
        )
        # Copiar antes de que el flujo existente elimine el mensaje original.
        await context.bot.copy_message(
            chat_id=GRUPO_PUBLICACIONES_USUARIOS_ID,
            from_chat_id=chat.id,
            message_id=mensaje.message_id,
        )
    except TelegramError:
        logging.exception(
            "R020 no pudo replicar restringida chat=%s message=%s user=%s",
            chat.id, mensaje.message_id, identidad_id,
        )


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

        # La membresía 12/12 sigue siendo la primera puerta cuando está activa.
        if config_raiz_activa("membresia_7de7_activa"):
            estado = await obtener_estado_membresia_7de7(usuario.id)
            if not estado["completo"]:
                return

        cfg_identidad_texto, _ = control_efectivo_para_chat(identidad_tipo, identidad_id, chat)
        tipo_contenido = tipo_publicitario_mensaje(
            mensaje,
            usuario,
            controlar_texto_simple=bool(
                cfg_identidad_texto["controlar_texto_simple"]
            ),
        )

    elif sender_chat is not None:
        identidad_tipo = "CANAL/CHAT"
        identidad_id = sender_chat.id
        username = sender_chat.username
        nombre = sender_chat.title or "Sin nombre visible"
        tipo_contenido = tipo_publicitario_mensaje(mensaje, None)

    else:
        return

    # Regla v1.0.8: texto puro <=50 siempre libre. Texto puro >50 se guarda
    # en vigilancia la primera vez; desde la segunda coincidencia de la misma
    # identidad, sin caducidad temporal, entra al motor como publicidad textual.
    if tipo_contenido is None and clasificar_contenido_mensaje(mensaje) == "TEXTO":
        estado_texto = clasificar_texto_repetitivo_db(
            identidad_tipo, identidad_id, mensaje
        )
        if estado_texto == "PUBLICIDAD_TEXTUAL_REPETITIVA":
            tipo_contenido = "PUBLICIDAD TEXTUAL REPETITIVA"
        else:
            return

    if tipo_contenido is None:
        return

    if (tipo_contenido == "TEXTO SIMPLE" and usuario is not None
            and str(cfg_identidad_texto["modo"]).upper() not in {"ILIMITADO", "EXCLUIDO"}):
        context.chat_data[f"supervision_bloqueada:{mensaje.message_id}"] = True
        estado_supervision = await obtener_estado_membresia_7de7(usuario.id)
        await replicar_publicacion_restringida(
            context, mensaje, chat, usuario, estado_supervision,
            "EXCEPCIÓN INDIVIDUAL · MEMBRESÍA PUBLICITARIA REQUERIDA",
        )
        eliminado, estado_eliminacion = await eliminar_publicidad_estricta(
            context, mensaje, chat, identidad_id
        )

        motivo_texto = "EXCEPCIÓN INDIVIDUAL · MEMBRESÍA PUBLICITARIA REQUERIDA"
        registrar_evento_publicidad_db(
            identidad_tipo,
            identidad_id,
            chat,
            mensaje.message_id,
            tipo_contenido,
            "BLOQUEADA",
            motivo_texto,
            eliminacion_estado=estado_eliminacion,
        )
        await mostrar_aviso_texto_simple_membresia_temporal(
            context=context,
            chat=chat,
            identidad_id=identidad_id,
        )
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

    context.chat_data[f"supervision_bloqueada:{mensaje.message_id}"] = True
    if usuario is not None and not usuario.is_bot:
        estado_supervision = await obtener_estado_membresia_7de7(usuario.id)
        await replicar_publicacion_restringida(
            context, mensaje, chat, usuario, estado_supervision, motivo,
        )
    eliminado, estado_eliminacion = await eliminar_publicidad_estricta(
        context, mensaje, chat, identidad_id
    )

    registrar_evento_publicidad_db(
        identidad_tipo,
        identidad_id,
        chat,
        mensaje.message_id,
        tipo_contenido,
        "BLOQUEADA",
        motivo,
        eliminacion_estado=estado_eliminacion,
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

async def abrir_control_desde_registro(update,context):
    usuario=update.effective_user
    if not usuario or not es_administrador_maximo(usuario) or update.effective_chat.type!=ChatType.PRIVATE:return
    match=re.fullmatch(r"registro_([0-9]{1,20})_([0-9]{1,2})",context.args[0] if context.args else '')
    if not match or BIENVENIDAS is None:
        await update.effective_message.reply_text('Enlace de control no válido.');return
    uid,order=map(int,match.groups())
    if not (0<uid<2**63 and 1<=order<=12):
        await update.effective_message.reply_text('Enlace de control no válido.');return
    target,group=BIENVENIDAS.control_payload(uid,order)
    if not target or not group:
        await update.effective_message.reply_text('El usuario no pertenece al registro en vivo.');return
    for state in (ENTRADAS_CONTROL_PUBLICIDAD,ENTRADAS_ORMA_TOTAL,ENTRADAS_RAIZ,SELECCIONES_MODERACION_ORMA,BORRADORES_PERMISOS):
        state.pop(usuario.id,None)
    from types import SimpleNamespace
    source=SimpleNamespace(id=group['chat_id'],username=group['username'],title=group['name'])
    # Membership movement has no human source message: never substitute the archive's message ID.
    captura_id=guardar_captura_orma(usuario.id,'USUARIO',uid,target['username'],target['name'],False,source,0)
    await mostrar_ficha_orma_privada(context.bot,usuario.id,captura_id)


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

    if context.args and context.args[0].startswith('registro_'):
        await abrir_control_desde_registro(update,context)
        return

    registrar_usuario_membresia(usuario)

    # /start y MENÚ PRINCIPAL comparten exactamente la misma pantalla.
    ENTRADAS_CONTROL_PUBLICIDAD.pop(usuario.id, None)
    ENTRADAS_ORMA_TOTAL.pop(usuario.id, None)
    ENTRADAS_RAIZ.pop(usuario.id, None)
    SELECCIONES_MODERACION_ORMA.pop(usuario.id, None)

    texto = texto_menu_principal_orma()
    teclado = teclado_menu_principal_orma()

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


def _comando_slash_mensaje(mensaje):
    """Devuelve el token /comando inicial de text/caption, o None."""
    if mensaje is None:
        return None

    contenido = str(
        getattr(mensaje, "text", None)
        or getattr(mensaje, "caption", None)
        or ""
    ).strip()
    if not contenido.startswith("/"):
        return None
    return contenido.split(maxsplit=1)[0]


async def eliminar_comando_raiz_estricto(context, mensaje, chat):
    """Doble intento de eliminación para comandos no autorizados de grupo."""
    try:
        await mensaje.delete()
        return True, "ELIMINADA_PRIMER_INTENTO", None
    except TelegramError as error_1:
        logging.warning(
            "Comando raíz: primer intento falló chat=%s message=%s: %s",
            chat.id,
            getattr(mensaje, "message_id", None),
            error_1,
        )

    try:
        await context.bot.delete_message(
            chat_id=chat.id,
            message_id=mensaje.message_id,
        )
        return True, "ELIMINADA_SEGUNDO_INTENTO", str(error_1)
    except TelegramError as error_2:
        logging.exception(
            "COMANDO_RAIZ BLOQUEO FALLIDO chat=%s message=%s",
            chat.id,
            getattr(mensaje, "message_id", None),
        )
        return (
            False,
            "FALLO_ELIMINACION",
            f"primer_intento={error_1}; segundo_intento={error_2}",
        )


async def control_comandos_exclusivos_raiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Solo el administrador y bots oficiales de raíz pueden usar /comandos.

    Esta barrera es independiente de la membresía 12/12: incluso un usuario 12/12
    queda bloqueado si intenta ejecutar cualquier comando que empiece con '/'.
    """
    mensaje = update.effective_message
    usuario = update.effective_user
    chat = update.effective_chat

    if (
        not mensaje
        or not chat
        or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}
        or not es_grupo_controlado(chat)
    ):
        return

    comando = _comando_slash_mensaje(mensaje)
    if not comando:
        return

    # Únicas identidades autorizadas para comandos en los grupos controlados:
    # el administrador de Máximo y los bots oficiales exentos de raíz.
    if es_administrador_maximo(usuario) or es_bot_oficial_exento(usuario):
        return

    eliminado, estado, error = await eliminar_comando_raiz_estricto(
        context=context,
        mensaje=mensaje,
        chat=chat,
    )
    logging.warning(
        "COMANDO_RAIZ_BLOQUEADO user=%s username=%s chat=%s message=%s comando=%r estado=%s error=%s",
        getattr(usuario, "id", None),
        getattr(usuario, "username", None),
        chat.id,
        getattr(mensaje, "message_id", None),
        comando,
        estado,
        error,
    )

    # No permitir que el mismo update llegue a /orma, comandos de otros bots,
    # publicidad ni handlers posteriores.
    raise ApplicationHandlerStop


async def control_membresia_grupos(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Puerta raíz 12/12 universal para cualquier mensaje de grupo.

    Se ejecuta antes de los handlers de comandos y de publicidad. /orma queda
    expresamente protegido para conservar su flujo administrativo existente.
    """
    mensaje = update.effective_message
    usuario = update.effective_user
    chat = update.effective_chat

    if (
        not mensaje
        or not chat
        or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}
        or not es_grupo_controlado(chat)
    ):
        return

    # /orma es una herramienta administrativa protegida y conserva exactamente
    # su handler actual. La puerta 12/12 no lo intercepta.
    texto = str(getattr(mensaje, "text", "") or "").strip()
    comando = texto.split(maxsplit=1)[0].lower() if texto.startswith("/") else ""
    if comando in {
        "/orma",
        f"/orma@{MAXIMO_BOT_USERNAME.lower()}",
    }:
        return

    if not config_raiz_activa("membresia_7de7_activa"):
        return

    # Telegram puede entregar publicaciones hechas en nombre de un canal/chat
    # sin effective_user. Esa identidad no puede acreditar una membresía personal
    # 12/12 y ya no queda fuera de la barrera por un simple `not usuario`.
    sender_chat = getattr(mensaje, "sender_chat", None)
    if usuario is None:
        if sender_chat is None:
            return

        identidad_id = int(getattr(sender_chat, "id", 0) or 0)
        eliminado, estado_eliminacion, error_eliminacion = (
            await eliminar_mensaje_membresia_7de7_estricto(
                context=context,
                mensaje=mensaje,
                chat=chat,
                usuario_id=identidad_id,
            )
        )
        try:
            registrar_evento_sender_chat_7de7_db(
                sender_chat=sender_chat,
                chat=chat,
                message_id=mensaje.message_id,
                eliminacion_estado=estado_eliminacion,
                error=error_eliminacion,
            )
        except Exception:
            logging.exception(
                "12/12 no pudo auditar sender_chat=%s chat=%s message=%s estado=%s",
                identidad_id,
                chat.id,
                mensaje.message_id,
                estado_eliminacion,
            )

        await mostrar_aviso_sender_chat_7de7_temporal(
            context=context,
            chat=chat,
            sender_chat=sender_chat,
        )
        raise ApplicationHandlerStop

    # ÚNICA EXCEPCIÓN estructural: bots oficiales definidos de raíz.
    # Todo lo demás (usuarios, administradores y bots externos) cumple 12/12.
    if es_bot_oficial_exento(usuario):
        return

    registrar_usuario_membresia(usuario)
    estado = await obtener_estado_membresia_7de7(usuario.id)

    if estado["completo"]:
        return

    if estado.get("errores") and not estado.get("faltantes"):
        logging.warning(
            "MEMBRESIA_21_CONSULTA_INCOMPLETA user=%s chat=%s errores=%s",
            usuario.id,
            chat.id,
            len(estado["errores"]),
        )
        return

    await replicar_publicacion_restringida(
        context,
        mensaje,
        chat,
        usuario,
        estado,
        "MEMBRESÍA 21/21 INCOMPLETA",
    )

    eliminado, estado_eliminacion, error_eliminacion = (
        await eliminar_mensaje_membresia_7de7_estricto(
            context=context,
            mensaje=mensaje,
            chat=chat,
            usuario_id=usuario.id,
        )
    )

    try:
        registrar_evento_membresia_7de7_db(
            usuario=usuario,
            chat=chat,
            message_id=mensaje.message_id,
            estado=estado,
            eliminacion_estado=estado_eliminacion,
            error=error_eliminacion,
        )
    except Exception:
        logging.exception(
            "12/12 no pudo persistir auditoría user=%s chat=%s message=%s estado=%s",
            usuario.id,
            chat.id,
            mensaje.message_id,
            estado_eliminacion,
        )

    usuario_db = obtener_usuario_membresia_db(usuario.id)
    union_iniciado = bool(usuario_db and usuario_db["union_bot_iniciado"])

    if union_iniciado:
        await mostrar_o_actualizar_panel_union(usuario.id)

    await mostrar_aviso_union_temporal(
        context=context,
        chat_id=chat.id,
        usuario=usuario,
        estado=estado,
    )

    raise ApplicationHandlerStop


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


async def mostrar_error_entrada_panel(context, usuario_id, texto, volver):
    panel_id = PANELES_ORMA.get(usuario_id) or obtener_panel_orma_db(usuario_id)
    teclado = completar_navegacion_panel(InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ CANCELAR / REGRESAR", callback_data=volver)]
    ]))
    if panel_id:
        await safe_edit_message_text(context.bot, chat_id=usuario_id, message_id=panel_id,
            text=texto, reply_markup=teclado)
    else:
        nuevo = await context.bot.send_message(chat_id=usuario_id, text=texto, reply_markup=teclado)
        registrar_panel_reemplazo(usuario_id, nuevo)


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

    if not es_administrador_maximo(usuario):
        return

    entrada_raiz = ENTRADAS_RAIZ.get(usuario.id)
    if entrada_raiz:
        try:
            await mensaje.delete()
        except TelegramError:
            pass

        if entrada_raiz.get("accion") == "AGREGAR_BOT_EXENTO":
            username = str(mensaje.text or "").strip().lstrip("@").lower()
            try:
                agregado = agregar_bot_exento_raiz(username)
            except ValueError:
                await mostrar_error_entrada_panel(context, usuario.id, "Username no válido. Escribe el @username de un bot o cancela.", "orma_raiz_bots")
                return
            ENTRADAS_RAIZ.pop(usuario.id, None)
            try:
                await context.bot.send_message(
                    chat_id=usuario.id,
                    text=f"✅ <b>@{html.escape(agregado)}</b> quedó exento de las restricciones de raíz.",
                    parse_mode="HTML",
                    reply_markup=completar_navegacion_panel(InlineKeyboardMarkup([[InlineKeyboardButton("🤖 VOLVER A BOTS EXENTOS", callback_data="orma_raiz_bots")]])),
                )
            except TelegramError:
                pass
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
            volver = (f"orma_pg:{entrada_total['captura_id']}:{entrada_total['indice']}"
                if entrada_total.get("tipo") == "PUBLICIDAD_GRUPO" else f"orma_mod:{entrada_total['captura_id']}")
            await mostrar_error_entrada_panel(context, usuario.id, "Valor no válido. Escribe un entero igual o mayor que 0; para mute debe ser mayor que 0.", volver)
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
                await mostrar_error_entrada_panel(context, usuario.id, "El mute temporal requiere al menos 1 minuto.", f"orma_mod:{captura['id']}")
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
                        "❌ <b>VALOR NO VÁLIDO</b>\n\n"
                        "Escribe únicamente un número entero igual o mayor que 0.\n"
                        "Para límites: 0 bloquea. Para separación: 0 elimina la espera.\n"
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
    usuario = update.effective_user
    mensaje = update.effective_message

    if not usuario or not mensaje:
        return

    registrar_usuario_membresia(
        usuario,
        union_bot_iniciado=True,
    )

    estado = await obtener_estado_union(usuario.id)
    registrar_verificacion_union_12_db(usuario.id, estado)

    enviado = await mensaje.reply_text(
        texto_union_membresia(estado),
        parse_mode="HTML",
        reply_markup=teclado_union_membresia(estado),
    )
    guardar_union_panel_message_id(
        usuario.id,
        enviado.message_id,
    )


async def union_verificar_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    usuario = update.effective_user

    if not query or not usuario:
        return

    await query.answer("Verificando los 21 grupos…")

    registrar_usuario_membresia(
        usuario,
        union_bot_iniciado=True,
    )

    estado = await obtener_estado_union(usuario.id)
    registrar_verificacion_union_12_db(usuario.id, estado)

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

async def iniciar_aplicacion(
    application: Application,
    *,
    drop_pending_updates=True,
):
    await application.initialize()
    await application.start()

    if application.updater is None:
        raise RuntimeError("La aplicación no tiene Updater disponible.")

    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=drop_pending_updates,
    )


async def detener_aplicacion(application: Application):
    if application.updater is not None:
        await application.updater.stop()

    await application.stop()
    await application.shutdown()


async def main():
    global MAXIMO_APP_REF, UNION_APP_REF, BIENVENIDAS

    inicializar_base_datos()
    BIENVENIDAS = WelcomeService(DATABASE_PATH, es_administrador_maximo, notify_admin=ORMA_ADMIN_USER_ID)
    BIENVENIDAS.initialize()
    iniciar_agente_respaldo()

    maximo_app = Application.builder().token(BOT_TOKEN).build()
    union_app = Application.builder().token(UNION_BOT_TOKEN).build()

    membership_service = MembershipService(maximo_app.bot)
    registry21 = Registry21(BIENVENIDAS, membership_service)
    maximo_app.add_handler(TypeHandler(Update, registry21.observe), group=-40)
    maximo_app.add_handler(TypeHandler(Update, membership_service.observe), group=-30)

    MAXIMO_APP_REF = maximo_app
    UNION_APP_REF = union_app

    # Registro de miembros atendido una sola vez por registry21, antes de la barrera 12+9.
    maximo_app.add_handler(CallbackQueryHandler(BIENVENIDAS.callback, pattern=r"^wel:"))
    maximo_app.add_handler(CommandHandler("start", maximo_start))
    maximo_app.add_handler(CommandHandler("estado", maximo_estado))
    maximo_app.add_handler(CommandHandler("orma", orma_comando))
    maximo_app.add_handler(
        CallbackQueryHandler(eliminar_origen_supervision_callback, pattern=r"^orma_borrar_origen:")
    )
    maximo_app.add_handler(
        CallbackQueryHandler(orma_callback, pattern=r"^orma_")
    )
    maximo_app.add_handler(
        TypeHandler(Update, control_anti_evasion_spam),
        group=-10,
    )
    # R014: supervisión después de membresía y control publicitario. Los mensajes
    # ya bloqueados no se replican; solo llegan los que superaron los controles.
    maximo_app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS,
            replicar_actividad_humana_supervision,
        ),
        group=6,
    )
    maximo_app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS,
            control_comandos_exclusivos_raiz,
        ),
        group=-7,
    )
    maximo_app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS,
            control_membresia_grupos,
        ),
        group=-5,
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
    # Evento independiente para los 21 grupos: chat_member y mensajes de servicio.
    # Se ejecuta antes de que registro21 limpie los avisos de ingreso.
    maximo_app.add_handler(TypeHandler(Update, union_observar_cambio_grupo), group=-35)
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

    # MaximoControlGroup no descarta mensajes acumulados durante reinicios:
    # deben pasar por la moderación al recuperar el servicio. UnionMembresia
    # conserva el comportamiento previo para no alterar su flujo privado.
    await iniciar_aplicacion(maximo_app, drop_pending_updates=False)
    bind_service(membership_service)
    await iniciar_aplicacion(union_app, drop_pending_updates=True)
    registry21.start(maximo_app.bot)
    BIENVENIDAS.start(maximo_app.bot)
    auto_union_task = asyncio.create_task(union_revision_automatica(), name="union21-auto-panel")

    logging.info("@MaximoControlGroup_bot iniciado.")
    logging.info("@UnionMembresia_bot iniciado.")
    logging.info("Membresía obligatoria configurada: 21/21.")
    logging.info("Regla 21/21 universal activa en los 21 grupos oficiales.")
    logging.info("Registro de actividad y movimientos del Bloque 3 activo.")
    logging.info("Control Publicitario Individual del Bloque 4 activo.")
    logging.info("Bots oficiales exentos de raíz: %s", sorted(BOTS_OFICIALES_EXENTOS))
    logging.info("@%s permanece como laboratorio de pruebas.", GRUPO_PRUEBAS_USERNAME)

    try:
        await asyncio.Event().wait()
    finally:
        membership_service.closed = True
        bind_service(None)
        auto_union_task.cancel()
        try:
            await auto_union_task
        except asyncio.CancelledError:
            pass
        await registry21.stop()
        await BIENVENIDAS.stop()
        await detener_aplicacion(union_app)
        await detener_aplicacion(maximo_app)


if __name__ == "__main__":
    asyncio.run(main())


