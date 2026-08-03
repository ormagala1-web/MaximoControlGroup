import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional


DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DATABASE_PATH = DATA_DIR / "maximo_control.db"
AGENT_HOST = os.environ.get("BACKUP_AGENT_HOST", "0.0.0.0")
AGENT_PORT = int(os.environ.get("BACKUP_AGENT_PORT", "8080"))
AGENT_SECRET = os.environ.get("BACKUP_AGENT_SECRET", "").strip()

MAX_RESTORE_BYTES = int(
    os.environ.get("BACKUP_AGENT_MAX_RESTORE_BYTES", str(250 * 1024 * 1024))
)
RESTORE_BACKUPS_DIR = DATA_DIR / "restore_backups"
RESTORE_LOCK = threading.Lock()

_servidor = None
_hilo = None


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


def sha256_archivo(ruta: Path) -> str:
    digest = hashlib.sha256()
    with ruta.open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(1024 * 1024), b""):
            digest.update(bloque)
    return digest.hexdigest()


def crear_copia_consistente(origen: Path, destino: Path) -> None:
    conexion_origen = sqlite3.connect(origen)
    conexion_destino = sqlite3.connect(destino)
    try:
        conexion_origen.backup(conexion_destino)
        conexion_destino.commit()
    finally:
        conexion_destino.close()
        conexion_origen.close()


def validar_sqlite(ruta: Path) -> None:
    if not ruta.is_file() or ruta.stat().st_size <= 0:
        raise RuntimeError("La base de datos no existe o está vacía.")

    conexion = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True)
    try:
        integridad = conexion.execute("PRAGMA integrity_check").fetchone()[0]
        if str(integridad).lower() != "ok":
            raise RuntimeError(f"Integridad SQLite: {integridad}")
    finally:
        conexion.close()


def crear_paquete_remoto() -> tuple[Path, dict]:
    if not DATABASE_PATH.is_file():
        raise RuntimeError(f"No existe la base de datos: {DATABASE_PATH}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    carpeta_temp = Path(tempfile.mkdtemp(prefix="remote_backup_", dir=DATA_DIR))
    ruta_db = carpeta_temp / "maximo_control.db"
    ruta_zip = carpeta_temp / (
        "MaximoControlGroup_DB_"
        + ahora_utc().strftime("%Y%m%d_%H%M%S")
        + ".zip"
    )

    crear_copia_consistente(DATABASE_PATH, ruta_db)
    validar_sqlite(ruta_db)

    manifiesto = {
        "producto": "MaximoControlGroup",
        "tipo": "BASE_DATOS_REMOTA",
        "fecha_utc": ahora_utc().isoformat(),
        "archivo_base": "maximo_control.db",
        "tamano_bytes": ruta_db.stat().st_size,
        "sha256": sha256_archivo(ruta_db),
    }

    with zipfile.ZipFile(ruta_zip, "w", zipfile.ZIP_DEFLATED) as paquete:
        paquete.write(ruta_db, arcname="maximo_control.db")
        paquete.writestr(
            "manifest.json",
            json.dumps(manifiesto, ensure_ascii=False, indent=2),
        )

    ruta_db.unlink(missing_ok=True)
    return ruta_zip, manifiesto


def _miembro_zip_seguro(nombre: str) -> bool:
    ruta = Path(nombre)
    return (
        bool(nombre)
        and not ruta.is_absolute()
        and ".." not in ruta.parts
        and "\\" not in nombre
    )


def inspeccionar_paquete_restore(ruta_zip: Path, carpeta_temp: Path) -> tuple[Path, dict]:
    if not zipfile.is_zipfile(ruta_zip):
        raise RuntimeError("El archivo recibido no es un ZIP válido.")

    with zipfile.ZipFile(ruta_zip, "r") as paquete:
        nombres = paquete.namelist()

        if any(not _miembro_zip_seguro(nombre) for nombre in nombres):
            raise RuntimeError("El ZIP contiene rutas no seguras.")

        if "manifest.json" not in nombres:
            raise RuntimeError("El ZIP no contiene manifest.json.")

        try:
            manifiesto = json.loads(
                paquete.read("manifest.json").decode("utf-8-sig")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"manifest.json no es válido: {error}") from error

        producto = str(manifiesto.get("producto") or "").strip()
        if producto.lower() != "maximocontrolgroup":
            raise RuntimeError(
                f"El respaldo pertenece a otro producto: {producto or 'desconocido'}."
            )

        archivo_base = str(
            manifiesto.get("archivo_base") or "maximo_control.db"
        ).strip()

        if archivo_base != "maximo_control.db":
            raise RuntimeError(
                f"Archivo de base no permitido: {archivo_base}"
            )

        if archivo_base not in nombres:
            raise RuntimeError(
                f"El ZIP no contiene el archivo declarado: {archivo_base}"
            )

        destino_db = carpeta_temp / "maximo_control.db"
        with paquete.open(archivo_base, "r") as origen, destino_db.open("wb") as destino:
            shutil.copyfileobj(origen, destino, length=1024 * 1024)

    validar_sqlite(destino_db)

    sha_manifest = str(manifiesto.get("sha256") or "").strip().lower()
    sha_real = sha256_archivo(destino_db)

    if sha_manifest and not hmac.compare_digest(sha_manifest, sha_real):
        raise RuntimeError(
            "El SHA-256 de maximo_control.db no coincide con manifest.json."
        )

    tamano_manifest = manifiesto.get("tamano_bytes")
    if tamano_manifest is not None:
        try:
            esperado = int(tamano_manifest)
        except (TypeError, ValueError) as error:
            raise RuntimeError("tamano_bytes del manifest no es válido.") from error

        if esperado != destino_db.stat().st_size:
            raise RuntimeError(
                "El tamaño de maximo_control.db no coincide con manifest.json."
            )

    manifiesto["_sha256_verificado"] = sha_real
    manifiesto["_tamano_verificado"] = destino_db.stat().st_size
    return destino_db, manifiesto


def crear_respaldo_preventivo() -> tuple[Path, dict]:
    if not DATABASE_PATH.is_file():
        raise RuntimeError(
            f"No existe la base activa para crear respaldo preventivo: {DATABASE_PATH}"
        )

    RESTORE_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    marca = ahora_utc().strftime("%Y%m%d_%H%M%S")
    carpeta_temp = Path(
        tempfile.mkdtemp(prefix="preventivo_", dir=RESTORE_BACKUPS_DIR)
    )
    copia_db = carpeta_temp / "maximo_control.db"
    ruta_zip = RESTORE_BACKUPS_DIR / (
        f"MaximoControlGroup_PRE_RESTORE_{marca}.zip"
    )

    try:
        crear_copia_consistente(DATABASE_PATH, copia_db)
        validar_sqlite(copia_db)

        manifiesto = {
            "producto": "MaximoControlGroup",
            "tipo": "RESPALDO_PREVENTIVO_RESTAURACION",
            "fecha_utc": ahora_utc().isoformat(),
            "archivo_base": "maximo_control.db",
            "tamano_bytes": copia_db.stat().st_size,
            "sha256": sha256_archivo(copia_db),
        }

        with zipfile.ZipFile(ruta_zip, "w", zipfile.ZIP_DEFLATED) as paquete:
            paquete.write(copia_db, arcname="maximo_control.db")
            paquete.writestr(
                "manifest.json",
                json.dumps(manifiesto, ensure_ascii=False, indent=2),
            )

        return ruta_zip, manifiesto
    finally:
        copia_db.unlink(missing_ok=True)
        try:
            carpeta_temp.rmdir()
        except OSError:
            pass


def restaurar_base_desde_zip(
    ruta_zip: Path,
    sha_zip_esperado: Optional[str] = None,
) -> dict:
    with RESTORE_LOCK:
        if sha_zip_esperado:
            sha_zip_real = sha256_archivo(ruta_zip)
            if not hmac.compare_digest(
                sha_zip_esperado.strip().lower(),
                sha_zip_real.lower(),
            ):
                raise RuntimeError(
                    "El SHA-256 del archivo ZIP recibido no coincide."
                )
        else:
            sha_zip_real = sha256_archivo(ruta_zip)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        carpeta_temp = Path(
            tempfile.mkdtemp(prefix="remote_restore_", dir=DATA_DIR)
        )
        preventivo_zip = None
        ruta_rollback = carpeta_temp / "maximo_control_rollback.db"
        nueva_db = None

        try:
            nueva_db, manifiesto = inspeccionar_paquete_restore(
                ruta_zip,
                carpeta_temp,
            )

            if DATABASE_PATH.is_file():
                preventivo_zip, _ = crear_respaldo_preventivo()
                crear_copia_consistente(DATABASE_PATH, ruta_rollback)
                validar_sqlite(ruta_rollback)

            destino_temporal = DATA_DIR / (
                f".publicidad_restore_{os.getpid()}_{threading.get_ident()}.db"
            )
            shutil.copy2(nueva_db, destino_temporal)
            validar_sqlite(destino_temporal)

            try:
                os.replace(destino_temporal, DATABASE_PATH)
                validar_sqlite(DATABASE_PATH)
            except Exception:
                destino_temporal.unlink(missing_ok=True)
                if ruta_rollback.is_file():
                    rollback_temporal = DATA_DIR / (
                        f".publicidad_rollback_{os.getpid()}_{threading.get_ident()}.db"
                    )
                    shutil.copy2(ruta_rollback, rollback_temporal)
                    os.replace(rollback_temporal, DATABASE_PATH)
                    validar_sqlite(DATABASE_PATH)
                raise

            return {
                "ok": True,
                "producto": "MaximoControlGroup",
                "accion": "BASE_DATOS_RESTAURADA",
                "fecha_utc": ahora_utc().isoformat(),
                "archivo_restaurado": str(DATABASE_PATH),
                "sha256_zip": sha_zip_real,
                "sha256_base": manifiesto["_sha256_verificado"],
                "tamano_bytes": manifiesto["_tamano_verificado"],
                "respaldo_preventivo": (
                    str(preventivo_zip) if preventivo_zip else None
                ),
                "manifest": {
                    clave: valor
                    for clave, valor in manifiesto.items()
                    if not str(clave).startswith("_")
                },
            }
        finally:
            ruta_zip.unlink(missing_ok=True)
            if nueva_db:
                nueva_db.unlink(missing_ok=True)
            ruta_rollback.unlink(missing_ok=True)
            try:
                carpeta_temp.rmdir()
            except OSError:
                shutil.rmtree(carpeta_temp, ignore_errors=True)


class ManejadorAgente(BaseHTTPRequestHandler):
    server_version = "PublicidadBackupAgent/2.0"

    def log_message(self, format, *args):
        return

    def _autorizado(self) -> bool:
        if not AGENT_SECRET:
            return False

        recibido = self.headers.get("Authorization", "")
        esperado = f"Bearer {AGENT_SECRET}"
        return hmac.compare_digest(recibido, esperado)

    def _json(self, estado: int, contenido: dict) -> None:
        cuerpo = json.dumps(contenido, ensure_ascii=False).encode("utf-8")
        self.send_response(estado)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _leer_cuerpo_a_archivo(self, prefijo: str, sufijo: str) -> Path:
        contenido_texto = self.headers.get("Content-Length", "").strip()
        if not contenido_texto:
            raise RuntimeError("Falta el encabezado Content-Length.")

        try:
            contenido = int(contenido_texto)
        except ValueError as error:
            raise RuntimeError("Content-Length no es válido.") from error

        if contenido <= 0:
            raise RuntimeError("El cuerpo de la solicitud está vacío.")

        if contenido > MAX_RESTORE_BYTES:
            raise RuntimeError(
                f"El archivo supera el máximo permitido: {MAX_RESTORE_BYTES} bytes."
            )

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        descriptor, nombre = tempfile.mkstemp(
            prefix=prefijo,
            suffix=sufijo,
            dir=DATA_DIR,
        )
        ruta = Path(nombre)

        try:
            restante = contenido
            with os.fdopen(descriptor, "wb") as archivo:
                while restante > 0:
                    bloque = self.rfile.read(min(1024 * 1024, restante))
                    if not bloque:
                        raise RuntimeError(
                            "La conexión terminó antes de recibir el archivo completo."
                        )
                    archivo.write(bloque)
                    restante -= len(bloque)
            return ruta
        except Exception:
            ruta.unlink(missing_ok=True)
            raise

    def do_GET(self):
        if self.path != "/health":
            self._json(404, {"ok": False, "error": "Ruta no encontrada"})
            return

        self._json(
            200,
            {
                "ok": True,
                "servicio": "MaximoControlGroup Backup Agent",
                "version": "2.0",
                "base_existe": DATABASE_PATH.is_file(),
                "restore_disponible": True,
            },
        )

    def do_POST(self):
        if self.path not in {"/backup", "/restore"}:
            self._json(404, {"ok": False, "error": "Ruta no encontrada"})
            return

        if not self._autorizado():
            self._json(401, {"ok": False, "error": "No autorizado"})
            return

        if self.path == "/backup":
            self._procesar_backup()
            return

        self._procesar_restore()

    def _procesar_backup(self):
        ruta_zip = None
        try:
            ruta_zip, manifiesto = crear_paquete_remoto()
            tamano = ruta_zip.stat().st_size

            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{ruta_zip.name}"',
            )
            self.send_header("Content-Length", str(tamano))
            self.send_header("X-Backup-SHA256", manifiesto["sha256"])
            self.end_headers()

            with ruta_zip.open("rb") as archivo:
                for bloque in iter(lambda: archivo.read(1024 * 1024), b""):
                    self.wfile.write(bloque)
        except Exception as error:
            self._json(500, {"ok": False, "error": str(error)})
        finally:
            if ruta_zip:
                carpeta = ruta_zip.parent
                ruta_zip.unlink(missing_ok=True)
                try:
                    carpeta.rmdir()
                except OSError:
                    pass

    def _procesar_restore(self):
        ruta_zip = None
        try:
            ruta_zip = self._leer_cuerpo_a_archivo(
                "restore_upload_",
                ".zip",
            )
            sha_esperado = self.headers.get("X-Archive-SHA256", "").strip()
            resultado = restaurar_base_desde_zip(
                ruta_zip,
                sha_zip_esperado=sha_esperado or None,
            )
            ruta_zip = None
            self._json(200, resultado)
        except RuntimeError as error:
            self._json(400, {"ok": False, "error": str(error)})
        except Exception as error:
            self._json(
                500,
                {
                    "ok": False,
                    "error": "Error interno durante la restauración.",
                    "detalle": str(error),
                },
            )
        finally:
            if ruta_zip:
                ruta_zip.unlink(missing_ok=True)


def iniciar_agente_respaldo() -> None:
    global _servidor, _hilo

    if _servidor is not None:
        return

    if not AGENT_SECRET:
        raise RuntimeError(
            "Falta configurar BACKUP_AGENT_SECRET para activar el agente remoto."
        )

    _servidor = ThreadingHTTPServer(
        (AGENT_HOST, AGENT_PORT),
        ManejadorAgente,
    )
    _hilo = threading.Thread(
        target=_servidor.serve_forever,
        name="MaximoControlGroupBackupAgent",
        daemon=True,
    )
    _hilo.start()
