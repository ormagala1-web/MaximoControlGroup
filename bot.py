import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
AVISO_MEMBRESIA_SEGUNDOS = 120
UNION_BOT_USERNAME = "UnionMembresia_bot"
GRUPO_PRUEBAS_USERNAME = "Orma_Pruebas"

GRUPOS_OFICIALES = [
    (1, "DISTRITO STREAMING UNIVERSAL 🌎🌍", "DistritoStreamingUniversal", "DistritoStreamingUniversal_Bot"),
    (2, "STREAMING DIGITAL PERUCHO 🇵🇪", "StreamingDigitalPerucho", "StreamingDigitalPerucho_bot"),
    (3, "PERÚ ENTRETENIMIENTO STREAMING 🇵🇪", "PeruEntretenimientoStreaming", "PeruEntretenimientoStreaming_Bot"),
    (4, "MUNDO CACHINERO STREAMING 🌎", "MundoCachineroStreaming", "MUCASTBOT"),
    (5, "🌎 UNIVERSO CIBERNÉTICO PERÚ 🇵🇪", "mundocibertetico", "UniversoCibertneticoPeru_bot"),
    (6, "💻 Metaverso Streaming Perú 🇵🇪", "metaversostreaminggo", "MetaversoPeru_bot"),
    (7, "🎭 MUNDO STREAMING PERÚ 🇵🇪", "mymundostreaming", "MundoStreamingPeru_bot"),
]

MAXIMO_APP_REF = None
UNION_APP_REF = None

# Un solo aviso temporal por usuario y grupo.
# Clave: (chat_id, user_id) -> message_id
AVISOS_MEMBRESIA_ACTIVOS = {}


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
        except TelegramError:
            pass

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
    user_id,
):
    enlace_union = (
        f"https://t.me/{UNION_BOT_USERNAME}?start=membresia"
    )

    clave = (chat_id, user_id)
    anterior_id = AVISOS_MEMBRESIA_ACTIVOS.get(clave)

    # Evita acumular avisos si el usuario insiste varias veces.
    if anterior_id:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=anterior_id,
            )
        except TelegramError:
            pass

    aviso = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🔒 <b>MEMBRESÍA PENDIENTE</b>\n\n"
            "Para participar debes completar tu membresía en los 7 grupos oficiales.\n\n"
            "Pulsa el botón para continuar de forma privada."
        ),
        parse_mode="HTML",
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
        await mensaje.reply_text(
            "🛡️ MAXIMO CONTROL GROUP\n\n"
            "Bot moderador conectado correctamente.\n\n"
            "Membresía 7/7 activa en los 7 grupos oficiales.\n"
            "@Orma_Pruebas continúa habilitado como laboratorio."
        )


async def maximo_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.effective_message
    if not mensaje:
        return

    await mensaje.reply_text(
        "✅ MaximoControlGroup operativo\n"
        "🔐 Membresía: 7/7 activa\n"
        "🌐 Moderación: 7 grupos oficiales + @Orma_Pruebas\n"
        "🚫 Castigos/baneos: desactivados\n"
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

    # Los bots tendrán reglas de publicidad propias en un bloque posterior.
    if usuario.is_bot:
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
        user_id=usuario.id,
    )


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
    except TelegramError:
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
    maximo_app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND,
            control_membresia_grupos,
        )
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
    logging.info("Moderación 7/7 activa en los 7 grupos oficiales.")
    logging.info("@%s permanece como laboratorio de pruebas.", GRUPO_PRUEBAS_USERNAME)

    try:
        await asyncio.Event().wait()
    finally:
        await detener_aplicacion(union_app)
        await detener_aplicacion(maximo_app)


if __name__ == "__main__":
    asyncio.run(main())
