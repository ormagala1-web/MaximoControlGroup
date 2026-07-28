import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
UNION_BOT_TOKEN = os.environ["UNION_BOT_TOKEN"]

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
DATABASE_PATH = os.path.join(DATA_DIR, "maximo_control.db")

TOTAL_GRUPOS_OBLIGATORIOS = 7


def conectar_db():
    conexion = sqlite3.connect(DATABASE_PATH)
    conexion.row_factory = sqlite3.Row
    return conexion


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

        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS grupos_obligatorios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER UNIQUE,
                username TEXT,
                nombre TEXT NOT NULL,
                enlace TEXT,
                obligatorio INTEGER NOT NULL DEFAULT 1,
                activo INTEGER NOT NULL DEFAULT 1,
                orden INTEGER NOT NULL DEFAULT 0,
                fecha_creacion TEXT NOT NULL
            )
            """
        )

        conexion.commit()


def registrar_usuario_membresia(user, union_bot_iniciado=False):
    ahora = datetime.now(timezone.utc).isoformat()
    nombre = " ".join(
        parte for parte in [user.first_name, user.last_name] if parte
    ).strip()

    with conectar_db() as conexion:
        existente = conexion.execute(
            """
            SELECT user_id, union_bot_iniciado
            FROM usuarios_membresia
            WHERE user_id = ?
            """,
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
                    user_id,
                    username,
                    nombre,
                    union_bot_iniciado,
                    fecha_primer_contacto,
                    fecha_actualizacion
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
            "La moderación de membresía y publicidad todavía no está activada. "
            "Primero estamos validando la infraestructura."
        )
        return

    await mensaje.reply_text(
        "🛡️ MaximoControlGroup está conectado.\n"
        "Modo de prueba: moderación todavía desactivada."
    )


async def maximo_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.effective_message
    if not mensaje:
        return

    await mensaje.reply_text(
        "✅ MaximoControlGroup operativo\n"
        "🔐 Membresía: pendiente de configuración\n"
        "🛡️ Control publicitario general: pendiente de configuración"
    )


async def union_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.effective_message
    usuario = update.effective_user
    chat = update.effective_chat

    if not mensaje or not usuario or not chat or chat.type != ChatType.PRIVATE:
        return

    registrar_usuario_membresia(usuario, union_bot_iniciado=True)

    origen = context.args[0] if context.args else None

    texto = (
        "🔐 MEMBRESÍA DE USUARIO\n\n"
        "Tu asistente privado de membresía está activado correctamente.\n\n"
        f"Grupos obligatorios previstos: {TOTAL_GRUPOS_OBLIGATORIOS}\n\n"
        "En el siguiente bloque configuraremos los 7 grupos oficiales. "
        "Después este mismo panel mostrará únicamente los grupos que todavía "
        "te falten y se irá actualizando hasta completar 7/7."
    )

    if origen:
        texto += f"\n\nOrigen recibido: {origen}"

    await mensaje.reply_text(texto)


async def union_membresia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = update.effective_message
    usuario = update.effective_user
    chat = update.effective_chat

    if not mensaje or not usuario or not chat or chat.type != ChatType.PRIVATE:
        return

    registrar_usuario_membresia(usuario, union_bot_iniciado=True)

    await mensaje.reply_text(
        "🔐 ESTADO DE MEMBRESÍA\n\n"
        "Los 7 grupos oficiales todavía no han sido cargados en la base de datos.\n\n"
        "Cuando los configuremos, aquí verás automáticamente:\n"
        "• grupos completados\n"
        "• grupos faltantes\n"
        "• progreso hasta 7/7"
    )


async def union_grupos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await union_membresia(update, context)


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
    inicializar_base_datos()

    maximo_app = Application.builder().token(BOT_TOKEN).build()
    union_app = Application.builder().token(UNION_BOT_TOKEN).build()

    maximo_app.add_handler(CommandHandler("start", maximo_start))
    maximo_app.add_handler(CommandHandler("estado", maximo_estado))

    union_app.add_handler(CommandHandler("start", union_start))
    union_app.add_handler(CommandHandler("membresia", union_membresia))
    union_app.add_handler(CommandHandler("grupos", union_grupos))

    await iniciar_aplicacion(maximo_app)
    await iniciar_aplicacion(union_app)

    logging.info("@MaximoControlGroup_bot iniciado.")
    logging.info("@UnionMembresia_bot iniciado.")
    logging.info("Base de datos compartida: %s", DATABASE_PATH)

    try:
        await asyncio.Event().wait()
    finally:
        await detener_aplicacion(union_app)
        await detener_aplicacion(maximo_app)


if __name__ == "__main__":
    asyncio.run(main())
