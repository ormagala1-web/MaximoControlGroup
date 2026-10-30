"""Registro de 21 grupos y limpieza exclusiva de avisos de ingreso."""
import asyncio,logging,time
from telegram.error import BadRequest,Forbidden,RetryAfter,TelegramError
LOG=logging.getLogger(__name__)
class Registry21:
 def __init__(self,welcome,membership):
  self.welcome=welcome;self.membership=membership;self.task=None;self.pause=0;self.lock=asyncio.Lock();self.catalog_lock=asyncio.Lock()
  with welcome.db() as c:
   c.executescript('''CREATE TABLE IF NOT EXISTS registro_grupos21(username TEXT PRIMARY KEY,chat_id INTEGER NOT NULL UNIQUE,title TEXT,delete_allowed INTEGER,error TEXT,checked REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS registro_avisos_ingreso(chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,fecha REAL NOT NULL,estado TEXT NOT NULL,intentos INTEGER NOT NULL DEFAULT 0,siguiente REAL NOT NULL DEFAULT 0,error TEXT,PRIMARY KEY(chat_id,message_id));''')
   rows=c.execute('SELECT username,chat_id FROM registro_grupos21').fetchall()
   pause=c.execute("SELECT value FROM bienvenida_meta WHERE key='registro21_retry_after'").fetchone()
   self.pause=float(pause['value']) if pause else 0
  for row in rows:self.bind(row['username'],row['chat_id'],persist=False)
 def bind(self,username,chat_id,title='',persist=True):
  name=str(username or '').lstrip('@').casefold()
  group=next((g for g in self.welcome.groups if g['username'].casefold()==name),None)
  if not group or type(chat_id) is not int or chat_id>=0:return None
  if group['chat_id'] and group['chat_id']!=chat_id:return None
  conflict=self.welcome.by_chat.get(chat_id)
  if conflict and conflict is not group:return None
  group['chat_id']=chat_id;self.welcome.by_chat[chat_id]=group
  self.membership.by_name[name]['chat_id']=chat_id
  if persist:
   with self.welcome.db() as c:c.execute('INSERT INTO registro_grupos21(username,chat_id,title) VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET chat_id=excluded.chat_id,title=excluded.title',(name,chat_id,title))
  return group
 def wait_telegram(self,delay):
  self.pause=max(self.pause,time.time()+delay)
  with self.welcome.db() as c:c.execute("INSERT OR REPLACE INTO bienvenida_meta VALUES('registro21_retry_after',?)",(str(self.pause),))
 async def resolve(self,bot):
  async with self.catalog_lock:
   for group in self.welcome.groups:
    if time.time()<self.pause:break
    with self.welcome.db() as c:r=c.execute('SELECT checked FROM registro_grupos21 WHERE username=?',(group['username'].casefold(),)).fetchone()
    if r and time.time()-r['checked']<300:continue
    try:
     chat=await bot.get_chat(chat_id=group['chat_id'] or '@'+group['username'],read_timeout=8,connect_timeout=5,pool_timeout=5)
     if str(chat.type) not in ('group','supergroup'):raise ValueError('El destino no es un grupo')
     if not group['chat_id'] and str(getattr(chat,'username','') or '').casefold()!=group['username'].casefold():raise ValueError('Username no coincide con el catálogo')
     if not self.bind(group['username'],chat.id,getattr(chat,'title','')):raise ValueError('Identidad de grupo incompatible')
     own=await bot.get_chat_member(chat_id=chat.id,user_id=bot.id,read_timeout=8,connect_timeout=5,pool_timeout=5)
     allowed=str(own.status)=='creator' or (str(own.status)=='administrator' and bool(getattr(own,'can_delete_messages',False)))
     with self.welcome.db() as c:c.execute('UPDATE registro_grupos21 SET delete_allowed=?,error=?,checked=? WHERE username=?',(int(allowed),None if allowed else 'Falta permiso para eliminar mensajes',time.time(),group['username'].casefold()))
     if not allowed:LOG.warning('Avisos de ingreso: falta permiso de borrado en @%s',group['username'])
    except RetryAfter as e:
     d=e.retry_after;self.wait_telegram((d.total_seconds() if hasattr(d,'total_seconds') else float(d))+1);break
    except (TelegramError,ValueError):LOG.warning('Registro 21: ID o permiso pendiente de @%s',group['username'])
 async def observe(self,update,context):
  chat=getattr(update,'effective_chat',None)
  if not chat or str(getattr(chat,'type','')) not in ('group','supergroup'):return
  group=self.welcome.by_chat.get(chat.id)
  if not group:group=self.bind(getattr(chat,'username',None),chat.id,getattr(chat,'title',''))
  if not group:return
  # Registrar antes de eliminar el servicio; los controles de los 21 grupos actúan después.
  if await self.welcome.observe(update,context) is False:return
  msg=getattr(update,'message',None)
  if not msg:return
  entrada=bool(getattr(msg,'new_chat_members',None))
  if not entrada:return
  with self.welcome.db() as c:c.execute("INSERT OR IGNORE INTO registro_avisos_ingreso(chat_id,message_id,fecha,estado) VALUES(?,?,?,'PENDIENTE')",(chat.id,msg.message_id,msg.date.timestamp()))
  await self.clean(context.bot,chat.id,msg.message_id)
 async def clean(self,bot,chat_id=None,message_id=None):
  if self.lock.locked() or time.time()<self.pause:return
  async with self.lock:
   with self.welcome.db() as c:
    sql="SELECT * FROM registro_avisos_ingreso WHERE estado='PENDIENTE' AND siguiente<=?";args=[time.time()]
    if chat_id is not None:sql+=' AND chat_id=? AND message_id=?';args.extend([chat_id,message_id])
    rows=c.execute(sql+' ORDER BY siguiente,fecha LIMIT 8',args).fetchall()
   for row in rows:
    if time.time()<self.pause:break
    state='BORRADO';error=None;delay=0
    if row['chat_id'] not in self.welcome.by_chat:continue
    if time.time()-row['fecha']>=48*3600:state='NO_RETIRABLE';error='Telegram: aviso mayor a 48 horas'
    else:
     try:await bot.delete_message(chat_id=row['chat_id'],message_id=row['message_id'],read_timeout=8,connect_timeout=5,pool_timeout=5)
     except RetryAfter as e:
      d=e.retry_after;delay=(d.total_seconds() if hasattr(d,'total_seconds') else float(d))+1;self.wait_telegram(delay);state='PENDIENTE';error=str(e)
     except BadRequest as e:
      if 'message to delete not found' not in str(e).casefold():state='PENDIENTE';delay=300;error=str(e)
     except Forbidden as e:state='PENDIENTE';delay=300;error=str(e)
     except TelegramError as e:state='PENDIENTE';delay=min(900,10*2**min(row['intentos'],6));error=str(e)
    with self.welcome.db() as c:c.execute('UPDATE registro_avisos_ingreso SET estado=?,error=?,intentos=intentos+1,siguiente=? WHERE chat_id=? AND message_id=?',(state,error,time.time()+delay,row['chat_id'],row['message_id']))
    if error:LOG.warning('Aviso ingreso pendiente chat=%s msg=%s: %s',row['chat_id'],row['message_id'],error)
 async def worker(self,bot):
  while True:
   try:await self.resolve(bot);await self.clean(bot)
   except asyncio.CancelledError:raise
   except Exception:LOG.exception('Registro 21: reintento de IDs o avisos pendiente')
   await asyncio.sleep(30)
 def start(self,bot):self.task=asyncio.create_task(self.worker(bot),name='registro21-servicios')
 async def stop(self):
  if self.task:
   self.task.cancel()
   try:await self.task
   except asyncio.CancelledError:pass
