"""Estado comprobado de los 21 grupos, separado del historial heredado."""
import json
from datetime import datetime,timezone

def inicializar(conexion):
    conexion.execute('''CREATE TABLE IF NOT EXISTS membresia_estado21 (
      user_id INTEGER PRIMARY KEY, actuales INTEGER NOT NULL, fuera INTEGER NOT NULL,
      errores INTEGER NOT NULL, maximo INTEGER NOT NULL, alcanzo INTEGER NOT NULL,
      perdio INTEGER NOT NULL, verificaciones INTEGER NOT NULL,
      fecha TEXT NOT NULL, dentro_json TEXT NOT NULL, fuera_json TEXT NOT NULL,
      errores_json TEXT NOT NULL)''')

def guardar(conexion,user_id,estado,oficiales):
    universo={str(g['username']).casefold() for g in oficiales}
    if len(universo)!=21:raise ValueError('El catálogo obligatorio debe tener 21 grupos distintos')
    def names(items):return {str(g['username']).casefold() for g in items}
    dentro=names(estado.get('completados',[]));fuera=names(estado.get('faltantes',[]))
    errores=names([e[0] for e in estado.get('errores',[])])
    if dentro|fuera|errores != universo or dentro&fuera or dentro&errores or fuera&errores:
        raise ValueError('Verificación incompleta o inconsistente; no guardar un estado inventado')
    inicializar(conexion)
    old=conexion.execute('SELECT maximo,alcanzo,perdio,verificaciones FROM membresia_estado21 WHERE user_id=?',(user_id,)).fetchone()
    maximo,alcanzo,perdio,verificaciones=tuple(old) if old else (0,0,0,0)
    completo=len(dentro)==21
    # Un error de consulta nunca acredita una salida.
    perdio=bool(perdio or (alcanzo and fuera));alcanzo=bool(alcanzo or completo)
    conexion.execute('''INSERT INTO membresia_estado21 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(user_id) DO UPDATE SET actuales=excluded.actuales,fuera=excluded.fuera,
      errores=excluded.errores,maximo=excluded.maximo,alcanzo=excluded.alcanzo,
      perdio=excluded.perdio,verificaciones=excluded.verificaciones,fecha=excluded.fecha,
      dentro_json=excluded.dentro_json,fuera_json=excluded.fuera_json,errores_json=excluded.errores_json''',
      (user_id,len(dentro),len(fuera),len(errores),max(maximo,len(dentro)),int(alcanzo),int(perdio),verificaciones+1,
       datetime.now(timezone.utc).isoformat(),json.dumps(sorted(dentro)),json.dumps(sorted(fuera)),json.dumps(sorted(errores))))

def resumen(conexion):
    inicializar(conexion)
    totales=conexion.execute('''SELECT COUNT(*) total,
      COALESCE(SUM(s.user_id IS NULL),0) sin_verificar,
      COALESCE(SUM(s.actuales=0 AND s.errores=0),0) cero,
      COALESCE(SUM(s.actuales BETWEEN 1 AND 20 AND s.errores=0),0) proceso,
      COALESCE(SUM(s.actuales=21 AND s.errores=0),0) completos,
      COALESCE(SUM(s.errores>0),0) sin_confirmar,
      COALESCE(SUM(s.alcanzo=1),0) alcanzo_total,
      COALESCE(SUM(s.perdio=1),0) perdieron_total
      FROM usuarios_membresia u LEFT JOIN membresia_estado21 s ON s.user_id=u.user_id
      WHERE u.union_bot_iniciado=1''').fetchone()
    origenes=conexion.execute('''SELECT COALESCE(origen_nombre,origen_username,'Acceso directo / sin origen') origen,COUNT(*) total
      FROM usuarios_membresia WHERE union_bot_iniciado=1
      GROUP BY origen_chat_id,origen_username,origen_nombre ORDER BY total DESC,origen''').fetchall()
    recientes=conexion.execute('''SELECT u.user_id,u.username,u.nombre,u.fecha_ultimo_acceso,
      s.actuales,s.errores,s.fecha FROM usuarios_membresia u
      LEFT JOIN membresia_estado21 s ON s.user_id=u.user_id WHERE u.union_bot_iniciado=1
      ORDER BY COALESCE(u.fecha_ultimo_acceso,u.fecha_actualizacion) DESC LIMIT 8''').fetchall()
    return dict(totales=totales,origenes=origenes,recientes=recientes)
