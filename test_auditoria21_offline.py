import ast,asyncio,json,pathlib,re,sqlite3,types,unittest,html
from datetime import datetime,timezone
import monitoreo21
ROOT=pathlib.Path(__file__).parent;SRC=(ROOT/'bot.py').read_text();TREE=ast.parse(SRC)
GROUPS=json.loads((ROOT/'pertenencia_grupos.json').read_text())
def state(n,errors=0):return dict(completados=GROUPS[:n],faltantes=GROUPS[n:21-errors],errores=[(g,'API') for g in GROUPS[21-errors:]] if errors else [])
def load(name,scope,file='bot.py',cls=None):
 tree=ast.parse((ROOT/file).read_text());body=tree.body
 if cls:body=next(n for n in body if isinstance(n,ast.ClassDef) and n.name==cls).body
 n=next(n for n in body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
 exec(compile(ast.Module(body=[n],type_ignores=[]),file,'exec'),scope)
async def noop(*a,**kw):pass
class Audit(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
  self.db.execute('CREATE TABLE usuarios_membresia(user_id INTEGER PRIMARY KEY,union_bot_iniciado INTEGER,origen_nombre TEXT,origen_username TEXT,origen_chat_id INTEGER,username TEXT,nombre TEXT,fecha_ultimo_acceso TEXT,fecha_actualizacion TEXT)')
  for uid in (1,2):self.db.execute("INSERT INTO usuarios_membresia VALUES (?,1,NULL,NULL,NULL,NULL,'Usuario',NULL,'2026-10-30')",(uid,))
  self.db.commit();monitoreo21.inicializar(self.db)
 def tearDown(self):self.db.close()
 def test_12_is_partial_21_is_complete(self):
  monitoreo21.guardar(self.db,1,state(12),GROUPS);monitoreo21.guardar(self.db,2,state(21),GROUPS)
  t=monitoreo21.resumen(self.db)['totales'];self.assertEqual(t['proceso'],1);self.assertEqual(t['completos'],1)
 def test_history_is_not_invented(self):
  t=monitoreo21.resumen(self.db)['totales'];self.assertEqual(t['sin_verificar'],2);self.assertEqual(t['completos'],0)
 def test_unknown_is_not_exit(self):
  monitoreo21.guardar(self.db,1,state(21),GROUPS);monitoreo21.guardar(self.db,1,state(20,1),GROUPS)
  row=self.db.execute('SELECT * FROM membresia_estado21 WHERE user_id=1').fetchone()
  self.assertEqual(row['perdio'],0);self.assertEqual(row['errores'],1);self.assertEqual(row['maximo'],21)
  monitoreo21.guardar(self.db,1,state(20),GROUPS)
  self.assertEqual(self.db.execute('SELECT perdio FROM membresia_estado21 WHERE user_id=1').fetchone()[0],1)
 def test_bad_snapshot_rejected(self):
  with self.assertRaises(ValueError):monitoreo21.guardar(self.db,1,dict(completados=GROUPS[:12],faltantes=[],errores=[]),GROUPS)
  self.assertEqual(self.db.execute('SELECT count(*) FROM membresia_estado21').fetchone()[0],0)
 def test_zero_and_unknown_counts(self):
  monitoreo21.guardar(self.db,1,state(0),GROUPS);monitoreo21.guardar(self.db,2,state(0,21),GROUPS)
  t=monitoreo21.resumen(self.db)['totales'];self.assertEqual(t['cero'],1);self.assertEqual(t['sin_confirmar'],1)
 def test_monitor_render_has_no_old_or_damaged_labels(self):
  monitoreo21.guardar(self.db,1,state(21),GROUPS)
  ns=dict(resumen_monitoreo_union_db=lambda:monitoreo21.resumen(self.db),html=html,datetime=datetime,timezone=timezone,formatear_fecha_peru=lambda x:str(x))
  load('texto_monitoreo_union',ns);text=ns['texto_monitoreo_union']()
  self.assertIn('21/21',text);self.assertNotIn('12/12',text);self.assertNotIn('MEMBRES?A',text);self.assertIn('sin verificar 21 grupos',text)
 def test_catalogues_agree_and_all_active(self):
  base={g['username'].casefold():g['chat_id'] for g in GROUPS};self.assertEqual(len(base),21)
  for f in ('bienvenidas_grupos.json','registro_grupos.json'):
   gs=json.loads((ROOT/f).read_text());self.assertEqual({g['username'].casefold():g['chat_id'] for g in gs},base)
   self.assertTrue(all(not g.get('registration_only') for g in gs))
  self.assertTrue(all(not g.get('query_only') for g in GROUPS))
 def test_permissions_accept_13_through_21(self):
  drafts={7:dict(captura_id=1,grupos=set(),cambios={},duracion=-1,confirmacion=False)}
  async def update(bot,capture,draft):draft['disponibles']=set(range(1,22))
  ns=dict(BORRADORES_PERMISOS=drafts,obtener_captura_orma=lambda x:dict(objetivo_tipo='USUARIO',objetivo_es_bot=False),captura_pertenece_propietario=lambda *a:True,actualizar_grupos_usuario_permisos=update,TOTAL_GRUPOS_OBLIGATORIOS=21,safe_query_edit_message=noop,cabecera_identidad_orma=lambda *a:'',resumen_membresia_selector_permisos=lambda *a:'',teclado_grupos_permisos=lambda *a:None)
  load('permisos_individuales_callback',ns)
  q=types.SimpleNamespace(answer=noop);u=types.SimpleNamespace(id=7);ctx=types.SimpleNamespace(bot=None)
  for index in range(13,22):asyncio.run(ns['permisos_individuales_callback'](q,u,ctx,f'orma_permg:1:{index}'))
  self.assertEqual(drafts[7]['grupos'],set(range(13,22)))
  asyncio.run(ns['permisos_individuales_callback'](q,u,ctx,'orma_permg:1:22'));self.assertNotIn(22,drafts[7]['grupos'])
 def test_registry_link_accepts_21_rejects_22(self):
  calls=[]
  class Welcome:
   def control_payload(self,uid,order):calls.append(order);return dict(username='u',name='Nombre'),dict(chat_id=-100,username='g',name='Grupo')
  ns=dict(ChatType=types.SimpleNamespace(PRIVATE='private'),re=re,BIENVENIDAS=Welcome(),es_administrador_maximo=lambda x:True,TOTAL_GRUPOS_OBLIGATORIOS=21,guardar_captura_orma=lambda *a:1,mostrar_ficha_orma_privada=noop)
  for n in ('ENTRADAS_CONTROL_PUBLICIDAD','ENTRADAS_ORMA_TOTAL','ENTRADAS_RAIZ','SELECCIONES_MODERACION_ORMA','BORRADORES_PERMISOS'):ns[n]={}
  load('abrir_control_desde_registro',ns)
  u=types.SimpleNamespace(effective_user=types.SimpleNamespace(id=7),effective_chat=types.SimpleNamespace(type='private'),effective_message=types.SimpleNamespace(reply_text=noop))
  for order in range(13,23):asyncio.run(ns['abrir_control_desde_registro'](u,types.SimpleNamespace(args=[f'registro_123_{order}'],bot=None)))
  self.assertEqual(calls,list(range(13,22)))
 def test_exit_observed_but_only_entry_cleaned(self):
  events=[];cleaned=[]
  async def observe(*a):events.append('observado')
  async def clean(*a):cleaned.append(True)
  class DB:
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def execute(self,*a):pass
  welcome=types.SimpleNamespace(by_chat={-100:{"username":"grupo"}},observe=observe,db=DB)
  service=types.SimpleNamespace(welcome=welcome,clean=clean)
  ns={};load('observe',ns,'registro21.py','Registry21')
  chat=types.SimpleNamespace(id=-100,type='supergroup')
  for entrada in (False,True):
   msg=types.SimpleNamespace(new_chat_members=[1] if entrada else [],left_chat_member=None if entrada else object(),message_id=1,date=datetime.now(timezone.utc))
   asyncio.run(ns['observe'](service,types.SimpleNamespace(effective_chat=chat,message=msg),types.SimpleNamespace(bot=None)))
  self.assertEqual(len(events),2);self.assertEqual(len(cleaned),1)
if __name__=='__main__':unittest.main(verbosity=2)
