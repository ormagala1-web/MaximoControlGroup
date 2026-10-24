"""Persistent welcome cleanup and per-user community passport. No other moderation."""
from __future__ import annotations
import asyncio
import html
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LOG = logging.getLogger(__name__)
DESTINATION = -1004303379365
TOPIC = 3
PERU = ZoneInfo('America/Lima')


def stamp(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def visible_date(value):
    return datetime.fromisoformat(value).astimezone(PERU).strftime('%d/%m/%Y %H:%M:%S') if value else 'No registrado'


def is_member(member):
    return str(member.status) in ('member', 'administrator', 'creator') or (str(member.status) == 'restricted' and bool(member.is_member))


class WelcomeService:
    def __init__(self, database, authorized, notify_admin=None):
        self.database = str(database)
        self.authorized = authorized
        self.groups = json.loads(Path(__file__).with_name('bienvenidas_grupos.json').read_text(encoding='utf-8'))
        self.by_chat = {g['chat_id']: g for g in self.groups}
        self.locks = {}
        self.task = None
        self.boot = time.time()
        self.archive_error = ''
        self.notify_admin = notify_admin
        self.destination_thread = None
        self.destination_label = "Sin comprobar"
        self.destination_checked = 0
        self.api_pause_until = 0

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.database, timeout=20)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def initialize(self):
        with self.db() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS bienvenida_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bienvenida_bots(chat_id INTEGER PRIMARY KEY,bot_id INTEGER NOT NULL,username TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bienvenida_mensajes(chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,block_key TEXT NOT NULL,sent_at TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'ACTIVE',error TEXT,next_try REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(chat_id,message_id));
                CREATE TABLE IF NOT EXISTS bienvenida_usuarios(user_id INTEGER PRIMARY KEY,name TEXT NOT NULL,username TEXT,premium INTEGER,language TEXT,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bienvenida_eventos(id INTEGER PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,user_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,kind TEXT NOT NULL,occurred_at TEXT NOT NULL,received_at TEXT NOT NULL,name TEXT,username TEXT,actor_id INTEGER,source TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS bienvenida_historial ON bienvenida_eventos(user_id,occurred_at,id);
                CREATE TABLE IF NOT EXISTS bienvenida_estados(user_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,present INTEGER,status TEXT,changed_at TEXT,checked_at TEXT,source TEXT NOT NULL,PRIMARY KEY(user_id,chat_id));
                CREATE TABLE IF NOT EXISTS bienvenida_fichas(user_id INTEGER PRIMARY KEY,message_id INTEGER,dirty INTEGER NOT NULL DEFAULT 1,revision INTEGER NOT NULL DEFAULT 1,next_try REAL NOT NULL DEFAULT 0,error TEXT);
                CREATE TABLE IF NOT EXISTS bienvenida_verificaciones(user_id INTEGER PRIMARY KEY,next_try REAL NOT NULL DEFAULT 0,error TEXT);
            ''')
            if c.execute("SELECT 1 FROM bienvenida_meta WHERE key='import_r028'").fetchone():
                return
            for u in c.execute('SELECT user_id,username,nombre,fecha_primer_contacto,fecha_actualizacion FROM usuarios_membresia').fetchall():
                c.execute('INSERT OR IGNORE INTO bienvenida_usuarios VALUES(?,?,?,?,?,?,?)', (u['user_id'],u['nombre'] or 'Sin nombre',u['username'],None,None,u['fecha_primer_contacto'],u['fecha_actualizacion']))
            for e in c.execute('SELECT * FROM movimientos_grupo ORDER BY fecha_evento,id').fetchall():
                if e['chat_id'] not in self.by_chat:
                    continue
                when = stamp(e['fecha_evento'])
                c.execute('INSERT OR IGNORE INTO bienvenida_usuarios VALUES(?,?,?,?,?,?,?)',(e['user_id'],e['nombre'] or 'Sin nombre',e['username'],None,None,when,when))
                c.execute('INSERT OR IGNORE INTO bienvenida_eventos(event_key,user_id,chat_id,kind,occurred_at,received_at,name,username,source) VALUES(?,?,?,?,?,?,?,?,?)',('legacy:'+str(e['id']),e['user_id'],e['chat_id'],e['tipo_movimiento'],when,when,e['nombre'],e['username'],'HISTORICO'))
                # Observed historical movement is NOT a current membership check.
                c.execute('INSERT OR REPLACE INTO bienvenida_estados VALUES(?,?,?,?,?,?,?)',(e['user_id'],e['chat_id'],None,None,when,None,'HISTORICO'))
            c.execute("INSERT INTO bienvenida_meta VALUES('import_r028',?)",(stamp(),))

    def dirty(self, c, uid):
        c.execute('INSERT INTO bienvenida_fichas(user_id) VALUES(?) ON CONFLICT(user_id) DO UPDATE SET dirty=1,revision=revision+1,next_try=0',(uid,))

    def record_change(self, update):
        event = getattr(update, 'chat_member', None)
        if not event or event.chat.id not in self.by_chat:
            return False
        user = event.new_chat_member.user
        if user.is_bot or is_member(event.old_chat_member) == is_member(event.new_chat_member):
            return False
        when = stamp(event.date)
        kind = 'ENTRADA' if is_member(event.new_chat_member) else ('EXPULSION' if str(event.new_chat_member.status)=='kicked' else 'SALIDA')
        name = ' '.join(x for x in (user.first_name, getattr(user,'last_name',None)) if x) or 'Sin nombre'
        now = stamp()
        with self.db() as c:
            result=c.execute('INSERT OR IGNORE INTO bienvenida_eventos(event_key,user_id,chat_id,kind,occurred_at,received_at,name,username,actor_id,source) VALUES(?,?,?,?,?,?,?,?,?,?)',('tg:'+str(update.update_id),user.id,event.chat.id,kind,when,now,name,user.username,getattr(event.from_user,'id',None),'TELEGRAM'))
            if not result.rowcount:
                return False
            c.execute('INSERT INTO bienvenida_usuarios VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,username=excluded.username,premium=excluded.premium,language=excluded.language,last_seen=excluded.last_seen',(user.id,name,user.username,int(bool(getattr(user,'is_premium',False))),getattr(user,'language_code',None),when,now))
            old=c.execute('SELECT changed_at FROM bienvenida_estados WHERE user_id=? AND chat_id=?',(user.id,event.chat.id)).fetchone()
            if not old or not old['changed_at'] or when >= old['changed_at']:
                c.execute('INSERT OR REPLACE INTO bienvenida_estados VALUES(?,?,?,?,?,?,?)',(user.id,event.chat.id,int(is_member(event.new_chat_member)),str(event.new_chat_member.status),when,when,'TELEGRAM'))
            self.dirty(c,user.id)
            c.execute('INSERT OR IGNORE INTO bienvenida_verificaciones(user_id) VALUES(?)',(user.id,))
        return True

    def welcome_group(self, message):
        g=self.by_chat.get(message.chat.id)
        user=getattr(message,'from_user',None)
        # Do not guess an anonymous sender from its displayed name or caption.
        if not g or not user or not user.is_bot or getattr(message,'sender_chat',None):
            return None
        with self.db() as c:
            pinned=c.execute('SELECT bot_id FROM bienvenida_bots WHERE chat_id=?',(g['chat_id'],)).fetchone()
        if pinned:
            return g if user.id == pinned['bot_id'] else None
        return g if str(user.username or '').casefold()==g['bot_username'].casefold() else None

    async def capture_welcome(self, bot, message):
        g=self.welcome_group(message)
        if not g:
            return False
        async with self.locks.setdefault(g['chat_id'],asyncio.Lock()):
            with self.db() as c:
                if c.execute('SELECT 1 FROM bienvenida_mensajes WHERE chat_id=? AND message_id=?',(g['chat_id'],message.message_id)).fetchone():
                    return True
                c.execute('INSERT OR IGNORE INTO bienvenida_bots VALUES(?,?,?)',(g['chat_id'],message.from_user.id,g['bot_username']))
                # Album messages share a block; ordinary video+caption is one message.
                block='album:'+str(message.media_group_id) if getattr(message,'media_group_id',None) else 'msg:'+str(message.message_id)
                when=stamp(message.date)
                active=c.execute("SELECT * FROM bienvenida_mensajes WHERE chat_id=? AND state='ACTIVE' ORDER BY sent_at DESC,message_id DESC LIMIT 1",(g['chat_id'],)).fetchone()
                newer=not active or (when,message.message_id)>(active['sent_at'],active['message_id'])
                same=active and active['block_key']==block
                state='ACTIVE' if newer or same else 'PENDING'
                if newer and not same:
                    c.execute("UPDATE bienvenida_mensajes SET state='PENDING',next_try=0 WHERE chat_id=? AND state='ACTIVE' AND block_key<>?",(g['chat_id'],block))
                c.execute('INSERT INTO bienvenida_mensajes(chat_id,message_id,block_key,sent_at,state) VALUES(?,?,?,?,?)',(g['chat_id'],message.message_id,block,when,state))
            await self.clean_pending(bot,g['chat_id'])
        return True

    async def clean_pending(self, bot, chat_id=None):
        if time.time()<self.api_pause_until:
            return
        with self.db() as c:
            rows=c.execute("SELECT * FROM bienvenida_mensajes WHERE state='PENDING' AND next_try<=?"+(' AND chat_id=?' if chat_id else '')+' ORDER BY sent_at LIMIT 30', (time.time(),chat_id) if chat_id else (time.time(),)).fetchall()
        for row in rows:
            error=None; state='DELETED'; retry=0
            try:
                if time.time()-datetime.fromisoformat(row['sent_at']).timestamp() >= 48*3600:
                    state='BLOCKED'; error='Supera el limite de 48 horas de Telegram; requiere limpieza manual.'
                else:
                    await bot.delete_message(chat_id=row['chat_id'],message_id=row['message_id'])
            except Exception as exc:
                error=str(exc)
                if 'message to delete not found' in error.lower():
                    state='DELETED'; error=None
                elif "can't be deleted" in error.lower() or 'message_delete_forbidden' in error.lower():
                    state='BLOCKED'
                else:
                    state='PENDING'; retry=time.time()+self.retry_delay(exc,row['attempts'])
                    if getattr(exc,"retry_after",None) is not None: self.api_pause_until=retry
            with self.db() as c:
                c.execute('UPDATE bienvenida_mensajes SET state=?,error=?,next_try=?,attempts=attempts+1 WHERE chat_id=? AND message_id=?',(state,error,retry,row['chat_id'],row['message_id']))
            if error:
                LOG.warning('Bienvenida pendiente chat=%s msg=%s: %s',row['chat_id'],row['message_id'],error)
            if time.time()<self.api_pause_until: break

    @staticmethod
    def retry_delay(exc, attempt=0):
        retry=getattr(exc,'retry_after',None)
        if retry is not None:
            return max(3,float(retry.total_seconds() if hasattr(retry,'total_seconds') else retry)+1)
        return min(900,10*(2**min(attempt,6)))

    async def observe(self, update, context):
        try:
            self.record_change(update)
            # Edited greetings must not reactivate an obsolete message.
            message=getattr(update,'message',None)
            if message:
                await self.capture_welcome(context.bot,message)
        except Exception:
            LOG.exception('Modulo de bienvenidas: evento no procesado; moderacion continua')

    def data(self,uid):
        with self.db() as c:
            user=c.execute('SELECT * FROM bienvenida_usuarios WHERE user_id=?',(uid,)).fetchone()
            states={r['chat_id']:dict(r) for r in c.execute('SELECT * FROM bienvenida_estados WHERE user_id=?',(uid,))}
            latest=c.execute('SELECT * FROM bienvenida_eventos WHERE user_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1',(uid,)).fetchone()
        return dict(user) if user else None,states,dict(latest) if latest else None

    def card_text(self,uid):
        user,states,last=self.data(uid)
        if not user:
            return 'Expediente no disponible.'
        inside=sum(s['present']==1 for s in states.values()); outside=sum(s['present']==0 for s in states.values())
        name=html.escape(user['name'][:200]); username=('@'+html.escape(user['username'])) if user['username'] else 'Sin @usuario'
        text=(f'🛂 <b>PASAPORTE DE COMUNIDAD</b>\n━━━━━━━━━━━━━━━━━━━━\n'
              f'<a href="tg://user?id={uid}"><b>{name}</b></a>\n{username} · ID <code>{uid}</code>\n\n'
              f'🟢 <b>{inside} dentro</b>   🔴 <b>{outside} fuera</b>   ⚪ <b>{12-inside-outside} sin verificar</b>\n\n')
        if user['premium'] is not None:
            text+=f'Premium: {"Sí" if user["premium"] else "No indicado"} · Idioma: {html.escape(user["language"] or "No disponible")}\n'
        with self.db() as c:
            first=c.execute("SELECT MIN(occurred_at) FROM bienvenida_eventos WHERE user_id=? AND kind='ENTRADA'",(uid,)).fetchone()[0]
        text+=f'📍 Primer ingreso registrado: {visible_date(first)}\n'
        if last:
            group=self.by_chat[last['chat_id']]
            text+=f'\n<b>ÚLTIMO MOVIMIENTO</b>\n{html.escape(last["kind"])} · {html.escape(group["name"])}\n🕒 {visible_date(last["occurred_at"])}\n'
        text+='\nToca un grupo para ver sus fechas y reingresos.\nHora de Perú · Sin verificar no significa fuera.'
        return text

    def keyboard(self,uid):
        from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup
        _,states,_=self.data(uid)
        buttons=[]
        for g in self.groups:
            state=states.get(g['chat_id'],{}).get('present')
            mark='🟢' if state==1 else ('🔴' if state==0 else '⚪')
            buttons.append(B(f'{mark} {g["short"]}',callback_data=f'wel:g:{uid}:{g["order"]}'))
        rows=[buttons[i:i+3] for i in range(0,12,3)]
        rows += [[B('📖 MOVIMIENTOS',callback_data=f'wel:h:{uid}:0'),B('🔄 VERIFICAR 12/12',callback_data=f'wel:v:{uid}')], [B('👤 ABRIR PERFIL',url=f'tg://user?id={uid}')]]
        return InlineKeyboardMarkup(rows)

    def group_text(self,uid,order):
        g=next(g for g in self.groups if g['order']==order)
        user,states,_=self.data(uid)
        with self.db() as c:
            events=c.execute('SELECT * FROM bienvenida_eventos WHERE user_id=? AND chat_id=? ORDER BY occurred_at,id',(uid,g['chat_id'])).fetchall()
        entries=[e for e in events if e['kind']=='ENTRADA']; exits=[e for e in events if e['kind']!='ENTRADA']
        s=states.get(g['chat_id'],{}); present=s.get('present')
        label='🟢 DENTRO' if present==1 else ('🔴 FUERA' if present==0 else '⚪ SIN VERIFICAR')
        return (f'🛂 <b>{html.escape(user["name"][:200])}</b> · <code>{uid}</code>\n\n'
                f'<b>{html.escape(g["name"])}</b>\n{label}\n\n'
                f'Primer ingreso conocido: {visible_date(entries[0]["occurred_at"] if entries else None)}\n'
                f'Último ingreso: {visible_date(entries[-1]["occurred_at"] if entries else None)}\n'
                f'Última salida: {visible_date(exits[-1]["occurred_at"] if exits else None)}\n'
                f'Ingresos registrados: {len(entries)} · Reingresos observados: {max(0,len(entries)-1)}\n'
                f'Estado observado/comprobado: {visible_date(s.get("checked_at"))}\n\n'
                'Las fechas no registradas no se reconstruyen. Hora de Perú.')

    def history_text(self,uid,page):
        with self.db() as c:
            rows=c.execute('SELECT * FROM bienvenida_eventos WHERE user_id=? ORDER BY occurred_at DESC,id DESC LIMIT 8 OFFSET ?',(uid,page*8)).fetchall()
        body=f'📖 <b>MOVIMIENTOS · ID {uid}</b>\nPágina {page+1}\n'
        for e in rows:
            g=self.by_chat[e['chat_id']]
            body+=f'\n{"🟢" if e["kind"]=="ENTRADA" else "🔴"} <b>{e["kind"]}</b> · {html.escape(g["short"])}\n{visible_date(e["occurred_at"])} · {e["source"]}\n'
        return body+'\nHora de Perú'

    async def reconcile(self,bot,uid):
        failed=[]
        delay=60
        for g in self.groups:
            with self.db() as c:
                previous=c.execute('SELECT * FROM bienvenida_estados WHERE user_id=? AND chat_id=?',(uid,g['chat_id'])).fetchone()
                baseline=dict(previous) if previous else None
            try:
                member=await bot.get_chat_member(chat_id=g['chat_id'],user_id=uid)
                with self.db() as c:
                    current=c.execute('SELECT * FROM bienvenida_estados WHERE user_id=? AND chat_id=?',(uid,g['chat_id'])).fetchone()
                    current=dict(current) if current else None
                    # A live event received during the API request always wins.
                    if current==baseline:
                        c.execute('INSERT OR REPLACE INTO bienvenida_estados VALUES(?,?,?,?,?,?,?)',(uid,g['chat_id'],int(is_member(member)),str(member.status),(baseline or {}).get('changed_at'),stamp(),'CONSULTA'))
                        self.dirty(c,uid)
            except Exception as exc:
                failed.append(str(exc))
                if getattr(exc,'retry_after',None) is not None:
                    delay=max(60,self.retry_delay(exc))
                    self.api_pause_until=time.time()+delay
                    break
            await asyncio.sleep(0.12)
        with self.db() as c:
            if failed:
                c.execute('UPDATE bienvenida_verificaciones SET next_try=?,error=? WHERE user_id=?',(time.time()+delay, '; '.join(failed)[:800],uid))
            else:
                c.execute('DELETE FROM bienvenida_verificaciones WHERE user_id=?',(uid,))

    async def check_destination(self,bot,force=False):
        if not force and self.destination_checked and time.time()-self.destination_checked<60:
            return
        try:
            chat=await bot.get_chat(chat_id=DESTINATION)
            member=await bot.get_chat_member(chat_id=DESTINATION,user_id=bot.id)
            if str(member.status) not in ('administrator','creator'):
                raise RuntimeError('Agrega @MaximoControlGroup_bot como administrador al grupo de registro.')
            self.destination_thread=TOPIC if bool(getattr(chat,'is_forum',False)) else None
            self.destination_label=('Tema '+str(TOPIC)) if self.destination_thread else 'Grupo normal (sin tema)'
            with self.db() as c:
                c.execute("INSERT OR REPLACE INTO bienvenida_meta VALUES('destination_status',?)",('Acceso confirmado · '+self.destination_label,))
            self.destination_checked=time.time()
        except Exception as exc:
            self.destination_checked=0
            self.archive_error='No puedo publicar en el grupo de registro: '+str(exc)
            with self.db() as c:
                c.execute("INSERT OR REPLACE INTO bienvenida_meta VALUES('destination_status',?)",(self.archive_error,))
            await self.notify_problem(bot,self.archive_error)
            raise

    async def notify_problem(self,bot,detail):
        if not self.notify_admin:
            return
        with self.db() as c:
            previous=c.execute("SELECT value FROM bienvenida_meta WHERE key='notified_destination_error'").fetchone()
        if previous and previous['value']==detail:
            return
        try:
            await bot.send_message(chat_id=self.notify_admin,text='⚠️ BIENVENIDAS · REGISTRO PENDIENTE\n\n'+detail+'\n\nAñade @MaximoControlGroup_bot como administrador al grupo INGRESO DE USUARIOS A GRUPOS. Los eventos recibidos permanecen en cola. Revisa BIENVENIDAS Y PASAPORTES en el centro de raíz.')
            with self.db() as c:
                c.execute("INSERT OR REPLACE INTO bienvenida_meta VALUES('notified_destination_error',?)",(detail,))
        except Exception as exc:
            LOG.warning('No se pudo avisar al administrador del destino: %s',exc)

    async def publish_one(self,bot):
        with self.db() as c:
            row=c.execute('SELECT * FROM bienvenida_fichas WHERE dirty=1 AND next_try<=? ORDER BY next_try,user_id LIMIT 1',(time.time(),)).fetchone()
        if not row:
            return False
        uid=row['user_id']; message_id=row['message_id']
        try:
            await self.check_destination(bot)
            kwargs=dict(chat_id=DESTINATION,text=self.card_text(uid),parse_mode='HTML',reply_markup=self.keyboard(uid))
            if message_id:
                try:
                    await bot.edit_message_text(message_id=message_id,**kwargs)
                except Exception as exc:
                    detail=str(exc).lower()
                    if 'message is not modified' in detail:
                        pass
                    elif 'message to edit not found' in detail:
                        message_id=None
                    else:
                        raise
            if not message_id:
                if self.destination_thread is not None:
                    kwargs['message_thread_id']=self.destination_thread
                try:
                    result=await bot.send_message(disable_notification=True,**kwargs)
                except Exception as exc:
                    if 'thread' in str(exc).lower():
                        raise RuntimeError('El grupo tiene temas, pero el tema 3 no es un destino valido o esta cerrado. '+str(exc)) from exc
                    raise
                message_id=result.message_id
            with self.db() as c:
                c.execute('UPDATE bienvenida_fichas SET message_id=?,dirty=CASE WHEN revision=? THEN 0 ELSE 1 END,error=NULL,next_try=0 WHERE user_id=?',(message_id,row['revision'],uid))
            self.archive_error=''
        except Exception as exc:
            self.archive_error=(str(exc) if self.destination_checked else (self.archive_error or str(exc)))
            await self.notify_problem(bot,self.archive_error)
            if getattr(exc,'retry_after',None) is not None: self.api_pause_until=time.time()+self.retry_delay(exc)
            with self.db() as c:
                c.execute('UPDATE bienvenida_fichas SET error=?,next_try=? WHERE user_id=?',(str(exc),time.time()+self.retry_delay(exc),uid))
            LOG.warning('Ficha pendiente uid=%s: %s',uid,exc)
        return True

    async def worker(self,bot):
        last_publish=0
        last_probe=0
        while True:
            try:
                if time.time()<self.api_pause_until:
                    await asyncio.sleep(min(5,self.api_pause_until-time.time()))
                    continue
                if time.time()-last_probe>=60:
                    last_probe=time.time()
                    try: await self.check_destination(bot,force=True)
                    except Exception: LOG.warning('Registro privado sin acceso: %s',self.archive_error)
                await self.clean_pending(bot)
                with self.db() as c:
                    row=c.execute('SELECT user_id FROM bienvenida_verificaciones WHERE next_try<=? ORDER BY next_try,user_id LIMIT 1',(time.time(),)).fetchone()
                if row:
                    await self.reconcile(bot,row['user_id'])
                if time.monotonic()-last_publish>=3.2:
                    if await self.publish_one(bot):
                        last_publish=time.monotonic()
            except Exception:
                LOG.exception('Trabajador de bienvenidas: reintentando')
            await asyncio.sleep(1)

    def root_text(self):
        with self.db() as c:
            linked=c.execute('SELECT COUNT(*) FROM bienvenida_bots').fetchone()[0]
            pending=c.execute("SELECT COUNT(*) FROM bienvenida_mensajes WHERE state IN ('PENDING','BLOCKED')").fetchone()[0]
            cards=c.execute('SELECT COUNT(*) FROM bienvenida_fichas WHERE message_id IS NOT NULL').fetchone()[0]
            queued=c.execute('SELECT COUNT(*) FROM bienvenida_fichas WHERE dirty=1').fetchone()[0]
            failures=c.execute("SELECT chat_id,message_id,error FROM bienvenida_mensajes WHERE error IS NOT NULL ORDER BY sent_at DESC LIMIT 3").fetchall()
        text=(f'🛂 <b>BIENVENIDAS Y PASAPORTES · CANDIDATA</b>\n\n'
              f'12 grupos configurados · {linked}/12 bots observados y vinculados\n'
              f'Bienvenidas pendientes de limpieza: {pending}\n'
              f'Fichas publicadas: {cards} · Pendientes: {queued}\n\n'
              'Destino: <code>-1004303379365</code>\n'
              f'Ruta: {html.escape(self.destination_label)}\n'
              'Una bienvenida por grupo. Una ficha por usuario.\n'
              'Consultar usuario conocido: /pasaporte seguido de su ID, en privado.\n\n'
              'Máximo debe ser administrador, poder borrar mensajes y tener recepción Bot-to-Bot habilitada.\n'
              'Los residuos desconocidos o mayores de 48 h requieren limpieza inicial manual. No se borran mensajes ajenos.\n')
        if self.archive_error:
            text+='\n<b>Archivo pendiente:</b> '+html.escape(self.archive_error[:350])
        for row in failures:
            text+=f'\n⚠ {self.by_chat[row["chat_id"]]["short"]} · mensaje {row["message_id"]}: '+html.escape(str(row['error'])[:180])
        return text

    async def open_passport(self,update,context):
        user=update.effective_user
        if not user or not self.authorized(user) or update.effective_chat.type!='private':
            return
        try:
            uid=int(context.args[0])
            if uid<=0 or not self.data(uid)[0]:
                raise ValueError()
        except (IndexError,ValueError):
            await update.effective_message.reply_text('Consulta un usuario conocido con /pasaporte seguido de su ID de Telegram.')
            return
        with self.db() as c:
            self.dirty(c,uid)
            c.execute('INSERT INTO bienvenida_verificaciones(user_id) VALUES(?) ON CONFLICT(user_id) DO UPDATE SET next_try=0',(uid,))
        await update.effective_message.reply_text(self.card_text(uid),parse_mode='HTML',reply_markup=self.keyboard(uid))

    async def callback(self,update,context):
        from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup
        q=update.callback_query
        if not self.authorized(q.from_user):
            await q.answer('Solo administración.',show_alert=True)
            return
        try:
            parts=q.data.split(':')
            if parts[1]=='root':
                await q.answer()
                try: await self.check_destination(context.bot,force=True)
                except Exception: pass
                await q.edit_message_text(text=self.root_text(),parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[B('🔄 ACTUALIZAR',callback_data='wel:root')],[B('⚙️ RAÍZ',callback_data='orma_raiz')]]))
                return
            uid=int(parts[2]); user,_,_=self.data(uid)
            if not user:
                await q.answer('Usuario no disponible.',show_alert=True); return
            if parts[1]=='v':
                with self.db() as c:
                    c.execute('INSERT INTO bienvenida_verificaciones(user_id) VALUES(?) ON CONFLICT(user_id) DO UPDATE SET next_try=0',(uid,))
                    self.dirty(c,uid)
                await q.answer('Verificación en cola. La ficha se actualizará.'); return
            await q.answer()
            text=self.card_text(uid)
            markup=self.keyboard(uid)
            if parts[1]=='g':
                text=self.group_text(uid,int(parts[3]))
            elif parts[1]=='h':
                page=max(0,int(parts[3])); text=self.history_text(uid,page)
                with self.db() as c:
                    count=c.execute('SELECT COUNT(*) FROM bienvenida_eventos WHERE user_id=?',(uid,)).fetchone()[0]
                nav=[]
                if page: nav.append(B('⬅️',callback_data=f'wel:h:{uid}:{page-1}'))
                if (page+1)*8<count: nav.append(B('➡️',callback_data=f'wel:h:{uid}:{page+1}'))
                markup=InlineKeyboardMarkup(([nav] if nav else [])+[[B('🛂 PASAPORTE',callback_data=f'wel:p:{uid}')]])
            if q.message.chat.type=='private':
                await q.edit_message_text(text=text,parse_mode='HTML',reply_markup=markup)
            else:
                await context.bot.send_message(chat_id=q.from_user.id,text=text,parse_mode='HTML',reply_markup=markup)
        except Exception as exc:
            if 'message is not modified' in str(exc).lower(): return
            LOG.warning('Consulta bienvenida: %s',exc)
            try: await q.answer('No se pudo abrir. Inicia /start en Máximo y vuelve a intentar.',show_alert=True)
            except Exception: pass

    def start(self,bot):
        self.task=asyncio.create_task(self.worker(bot),name='bienvenidas-pasaportes')

    async def stop(self):
        if self.task:
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass
