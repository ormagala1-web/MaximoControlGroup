"""Persistent welcome cleanup and per-user community passport. No other moderation."""
from __future__ import annotations
import asyncio
import html
import json
import logging
import sqlite3
import time
from types import SimpleNamespace
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
        self.groups = json.loads(Path(__file__).with_name('registro_grupos.json').read_text(encoding='utf-8'))
        self.by_chat = {g['chat_id']: g for g in self.groups if g['chat_id'] is not None}
        self.locks = {}
        self.task = None
        self.verification_task = None
        self.delivery_lock = asyncio.Lock()
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
                CREATE TABLE IF NOT EXISTS bienvenida_vistas(user_id INTEGER PRIMARY KEY,kind TEXT NOT NULL DEFAULT 'p',value INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS bienvenida_usuarios_vivos(user_id INTEGER PRIMARY KEY,first_entry_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bienvenida_fichas_anteriores(message_id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'PENDING',error TEXT,next_try REAL NOT NULL DEFAULT 0);
            ''')
            columns={r['name'] for r in c.execute('PRAGMA table_info(bienvenida_fichas)')}
            for column,definition in (('move_event_id','INTEGER NOT NULL DEFAULT 0'),('published_event_id','INTEGER NOT NULL DEFAULT 0'),('hidden','INTEGER NOT NULL DEFAULT 0')):
                if column not in columns:c.execute(f'ALTER TABLE bienvenida_fichas ADD COLUMN {column} {definition}')
            retired_columns={r['name'] for r in c.execute('PRAGMA table_info(bienvenida_fichas_anteriores)')}
            if 'replacement_id' not in retired_columns:c.execute('ALTER TABLE bienvenida_fichas_anteriores ADD COLUMN replacement_id INTEGER')
            if not c.execute("SELECT 1 FROM bienvenida_meta WHERE key='live_registration_started_at'").fetchone():
                original=c.execute("SELECT value FROM bienvenida_meta WHERE key='import_r028'").fetchone()
                cutoff=original['value'] if original else stamp(datetime.now(timezone.utc).replace(microsecond=0))
                c.execute("INSERT INTO bienvenida_meta VALUES('live_registration_started_at',?)",(cutoff,))
            cutoff=c.execute("SELECT value FROM bienvenida_meta WHERE key='live_registration_started_at'").fetchone()['value']
            if not c.execute("SELECT 1 FROM bienvenida_meta WHERE key='live_filter_revision_4'").fetchone():
                c.execute("INSERT OR IGNORE INTO bienvenida_usuarios_vivos SELECT user_id,MIN(occurred_at) FROM bienvenida_eventos WHERE kind='ENTRADA' AND source IN ('TELEGRAM','SERVICIO') AND occurred_at>=? GROUP BY user_id",(cutoff,))
                c.execute("INSERT OR IGNORE INTO bienvenida_fichas_anteriores(message_id,user_id) SELECT message_id,user_id FROM bienvenida_fichas WHERE message_id IS NOT NULL AND user_id NOT IN (SELECT user_id FROM bienvenida_usuarios_vivos)")
                c.execute("UPDATE bienvenida_fichas SET dirty=0,message_id=NULL,error=NULL WHERE user_id NOT IN (SELECT user_id FROM bienvenida_usuarios_vivos)")
                c.execute("DELETE FROM bienvenida_verificaciones WHERE user_id NOT IN (SELECT user_id FROM bienvenida_usuarios_vivos)")
                c.execute("INSERT INTO bienvenida_meta VALUES('live_filter_revision_4',?)",(stamp(),))
            if not c.execute("SELECT 1 FROM bienvenida_meta WHERE key='privacy_safe_revision_4'").fetchone():
                c.execute("UPDATE bienvenida_fichas SET dirty=1,next_try=0,revision=revision+1 WHERE user_id IN (SELECT user_id FROM bienvenida_usuarios_vivos)")
                c.execute("INSERT INTO bienvenida_meta VALUES('privacy_safe_revision_4',?)",(stamp(),))
            if not c.execute("SELECT 1 FROM bienvenida_meta WHERE key='group_only_revision_5'").fetchone():
                c.execute("UPDATE bienvenida_fichas SET dirty=1,next_try=0,revision=revision+1 WHERE user_id IN (SELECT user_id FROM bienvenida_usuarios_vivos)")
                c.execute("INSERT INTO bienvenida_meta VALUES('group_only_revision_5',?)",(stamp(),))
            if not c.execute("SELECT 1 FROM bienvenida_meta WHERE key='card_design_revision_6'").fetchone():
                c.execute("UPDATE bienvenida_fichas SET dirty=1,next_try=0,revision=revision+1 WHERE hidden=0 AND user_id IN (SELECT user_id FROM bienvenida_usuarios_vivos)")
                c.execute("INSERT INTO bienvenida_meta VALUES('card_design_revision_6',?)",(stamp(),))
            if not c.execute("SELECT 1 FROM bienvenida_meta WHERE key='registro_21_revision_1'").fetchone():
                c.execute("UPDATE bienvenida_fichas SET dirty=1,next_try=0,revision=revision+1 WHERE hidden=0 AND user_id IN (SELECT user_id FROM bienvenida_usuarios_vivos)")
                c.execute("INSERT OR IGNORE INTO bienvenida_verificaciones(user_id) SELECT user_id FROM bienvenida_usuarios_vivos")
                c.execute("INSERT INTO bienvenida_meta VALUES('registro_21_revision_1',?)",(stamp(),))
            c.execute("CREATE TABLE IF NOT EXISTS bienvenida_seguimiento(user_id INTEGER PRIMARY KEY,first_observed_at TEXT NOT NULL,first_kind TEXT NOT NULL)")
            c.execute("INSERT OR IGNORE INTO bienvenida_seguimiento SELECT user_id,first_entry_at,'ENTRADA' FROM bienvenida_usuarios_vivos")
            verification_columns={r['name'] for r in c.execute('PRAGMA table_info(bienvenida_verificaciones)')}
            for column in ('prioridad','revision'):
                if column not in verification_columns:c.execute('ALTER TABLE bienvenida_verificaciones ADD COLUMN '+column+' INTEGER NOT NULL DEFAULT 0')
            if 'prioridad' not in columns:c.execute('ALTER TABLE bienvenida_fichas ADD COLUMN prioridad INTEGER NOT NULL DEFAULT 0')
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

    def dirty(self,c,uid,movement_id=0):
        if not c.execute('SELECT 1 FROM bienvenida_seguimiento WHERE user_id=?',(uid,)).fetchone():return
        if movement_id:
            c.execute('INSERT INTO bienvenida_fichas(user_id,move_event_id,prioridad) VALUES(?,?,3) ON CONFLICT(user_id) DO UPDATE SET dirty=1,revision=revision+1,next_try=0,move_event_id=excluded.move_event_id,hidden=0,prioridad=3',(uid,movement_id))
            c.execute("INSERT OR REPLACE INTO bienvenida_vistas VALUES(?,'p',0)",(uid,))
        else:
            c.execute('INSERT INTO bienvenida_fichas(user_id) VALUES(?) ON CONFLICT(user_id) DO UPDATE SET dirty=CASE WHEN hidden=0 THEN 1 ELSE 0 END,revision=revision+1,next_try=0',(uid,))

    def record_change(self, update, source="TELEGRAM"):
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
            cutoff=c.execute("SELECT value FROM bienvenida_meta WHERE key='live_registration_started_at'").fetchone()['value']
            if when<cutoff:
                return False
            if kind=='ENTRADA':
                c.execute('INSERT OR IGNORE INTO bienvenida_usuarios_vivos VALUES(?,?)',(user.id,when))
            c.execute('INSERT OR IGNORE INTO bienvenida_seguimiento VALUES(?,?,?)',(user.id,when,kind))
            key=f'{source}:{update.update_id}:{event.chat.id}:{user.id}'
            if c.execute('SELECT 1 FROM bienvenida_eventos WHERE event_key IN (?,?)',(key,'tg:'+str(update.update_id))).fetchone():
                return False
            same=c.execute('SELECT * FROM bienvenida_eventos WHERE user_id=? AND chat_id=? AND kind=? AND occurred_at=? AND source<>? ORDER BY id DESC LIMIT 1',(user.id,event.chat.id,kind,when,source)).fetchone()
            merged=bool(same and same['source'] in ('SERVICIO','TELEGRAM'))
            if merged:
                if source=='SERVICIO':
                    return False
                c.execute('UPDATE bienvenida_eventos SET event_key=?,source=?,actor_id=? WHERE id=?',(key,source,getattr(event.from_user,'id',None),same['id']))
            else:
                c.execute('INSERT INTO bienvenida_eventos(event_key,user_id,chat_id,kind,occurred_at,received_at,name,username,actor_id,source) VALUES(?,?,?,?,?,?,?,?,?,?)',(key,user.id,event.chat.id,kind,when,now,name,user.username,getattr(event.from_user,'id',None),source))
            c.execute('INSERT INTO bienvenida_usuarios VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,username=excluded.username,premium=excluded.premium,language=excluded.language,last_seen=excluded.last_seen',(user.id,name,user.username,int(bool(getattr(user,'is_premium',False))),getattr(user,'language_code',None),when,now))
            old=c.execute('SELECT changed_at FROM bienvenida_estados WHERE user_id=? AND chat_id=?',(user.id,event.chat.id)).fetchone()
            if not old or not old['changed_at'] or when >= old['changed_at']:
                c.execute('INSERT OR REPLACE INTO bienvenida_estados VALUES(?,?,?,?,?,?,?)',(user.id,event.chat.id,int(is_member(event.new_chat_member)),str(event.new_chat_member.status),when,when,'TELEGRAM'))
            recorded=c.execute('SELECT id FROM bienvenida_eventos WHERE event_key=?',(key,)).fetchone()['id']
            latest=c.execute('SELECT id FROM bienvenida_eventos WHERE user_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1',(user.id,)).fetchone()['id']
            self.dirty(c,user.id,movement_id=recorded if recorded==latest and not merged else 0)
            LOG.info('Registro 21 movimiento uid=%s grupo=%s tipo=%s origen=%s evento=%s',user.id,event.chat.id,kind,source,recorded)
            c.execute('INSERT INTO bienvenida_verificaciones(user_id,prioridad) VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET next_try=0,prioridad=MAX(prioridad,1),revision=revision+1',(user.id,))
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
            message=getattr(update,'message',None)
            if not message or message.chat.id not in self.by_chat:
                return
            users=list(getattr(message,'new_chat_members',None) or [])
            leaving=getattr(message,'left_chat_member',None)
            if leaving:
                users.append(leaving)
            for user in users:
                if user.is_bot:
                    continue
                old=SimpleNamespace(status='member' if leaving else 'left',is_member=False)
                new=SimpleNamespace(status='left' if leaving else 'member',is_member=False,user=user)
                event=SimpleNamespace(chat=message.chat,date=message.date,old_chat_member=old,new_chat_member=new,from_user=getattr(message,'from_user',None))
                self.record_change(SimpleNamespace(update_id=update.update_id,chat_member=event),source='SERVICIO')
        except Exception:
            LOG.exception('Registro de miembros: evento no procesado; moderacion continua')
            return False

    def data(self,uid):
        with self.db() as c:
            user=c.execute('SELECT * FROM bienvenida_usuarios WHERE user_id=?',(uid,)).fetchone()
            states={r['chat_id']:dict(r) for r in c.execute('SELECT * FROM bienvenida_estados WHERE user_id=?',(uid,))}
            latest=c.execute('SELECT * FROM bienvenida_eventos WHERE user_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1',(uid,)).fetchone()
        return dict(user) if user else None,states,dict(latest) if latest else None

    def selected_group(self,uid):
        with self.db() as c:
            view=c.execute('SELECT kind,value FROM bienvenida_vistas WHERE user_id=?',(uid,)).fetchone()
        _,_,last=self.data(uid)
        if view and view['kind']=='g':
            return next(g for g in self.groups if g['order']==view['value'])
        return self.by_chat[last['chat_id']] if last else self.groups[0]

    def card_text(self,uid):
        user,states,last=self.data(uid)
        if not user:return 'Ficha no disponible.'
        g=self.selected_group(uid)
        # El resumen debe contar los mismos 21 grupos que muestra el teclado.
        # Los estados históricos de chats fuera del catálogo no se eliminan.
        current_states=[states.get(g['chat_id'],{}).get('present') for g in self.groups]
        inside=sum(present==1 for present in current_states)
        outside=sum(present==0 for present in current_states)
        username=('@'+html.escape(user['username'])) if user['username'] else 'Sin @usuario público'
        text=(f'🛂 <b>FICHA INDIVIDUAL · REGISTRO EN VIVO</b>\n━━━━━━━━━━━━━━━━━━━━\n'
              f'<b>{html.escape(user["name"][:200])}</b>\n'
              f'🆔 <b>ID:</b> <code>{uid}</code>\n👤 <b>USER:</b> {username}\n\n'
              f'🟢 {inside} dentro · 🔴 {outside} fuera · ⚪ {len(self.groups)-inside-outside} sin verificar\n')
        if last:
            current=self.by_chat[last['chat_id']]
            text+=f'\n<b>ÚLTIMA ACTIVIDAD</b>\n{last["kind"]} · {html.escape(current["name"])}\nChat ID: <code>{current["chat_id"]}</code>\n🕒 {visible_date(last["occurred_at"])}\n'
        text+='\n'+self.group_detail(uid,g['order'])
        text+='\nHora de Perú · Selecciona un grupo para consultar sus fechas.\nUna ficha por usuario · Historial completo en MOVIMIENTOS.'
        return text

    def keyboard(self,uid):
        from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup
        user,states,_=self.data(uid)
        buttons=[]
        for g in self.groups:
            state=states.get(g['chat_id'],{}).get('present')
            mark='🟢' if state==1 else ('🔴' if state==0 else '⚪')
            buttons.append(B(f'{mark} {g["short"]}',callback_data=f'wel:g:{uid}:{g["order"]}'))
        rows=[buttons[i:i+3] for i in range(0,len(buttons),3)]
        rows += [[B('📖 MOVIMIENTOS',callback_data=f'wel:h:{uid}:0'),B('🔄 VERIFICAR 21/21',callback_data=f'wel:v:{uid}')]]
        profile=B('👤 ABRIR PERFIL',url='https://t.me/'+user['username']) if user and user['username'] and all(ch.isascii() and (ch.isalnum() or ch=='_') for ch in user['username']) else B('👤 IDENTIDAD',callback_data=f'wel:i:{uid}')
        rows.append([profile,B('🛂 FICHA PRINCIPAL',callback_data=f'wel:p:{uid}')])
        group=self.selected_group(uid)
        if not group.get('registration_only'):
            rows.append([B('🛡 CONTROL INDIVIDUAL /ORMA',url=f'https://t.me/MaximoControlGroup_bot?start=registro_{uid}_{group["order"]}')])
        else:
            rows.append([B('ℹ️ SOLO REGISTRO · SIN RESTRICCIONES',callback_data=f'wel:r:{uid}')])
        rows.append([B('🗑 ELIMINAR ESTE MENSAJE',callback_data=f'wel:d:{uid}')])
        return InlineKeyboardMarkup(rows)

    def group_detail(self,uid,order):
        g=next(g for g in self.groups if g['order']==order)
        _,states,_=self.data(uid)
        with self.db() as c:
            events=c.execute("SELECT * FROM bienvenida_eventos WHERE user_id=? AND chat_id=? AND source<>'HISTORICO' AND occurred_at>=(SELECT value FROM bienvenida_meta WHERE key='live_registration_started_at') ORDER BY occurred_at,id",(uid,g['chat_id'])).fetchall()
        entries=[e for e in events if e['kind']=='ENTRADA'];exits=[e for e in events if e['kind']!='ENTRADA']
        state=states.get(g['chat_id'],{});present=state.get('present')
        label='🟢 DENTRO' if present==1 else ('🔴 FUERA' if present==0 else '⚪ SIN VERIFICAR')
        return (f'<b>GRUPO SELECCIONADO</b>\n<b>{html.escape(g["name"])}</b>\nChat ID: <code>{g['chat_id'] if g['chat_id'] is not None else 'Pendiente de resolver'}</code> · {label}\n\n'
                f'Primer ingreso: {visible_date(entries[0]["occurred_at"] if entries else None)}\n'
                f'Último ingreso: {visible_date(entries[-1]["occurred_at"] if entries else None)}\n'
                f'Última salida: {visible_date(exits[-1]["occurred_at"] if exits else None)}\n'
                f'Ingresos: {len(entries)} · Reingresos: {max(0,len(entries)-1)}\n'
                f'Última comprobación: {visible_date(state.get("checked_at"))}\n')

    def group_text(self,uid,order):
        return self.card_text(uid)

    def control_payload(self,uid,order):
        with self.db() as c:
            live=c.execute('SELECT 1 FROM bienvenida_seguimiento WHERE user_id=?',(uid,)).fetchone()
        group=next((g for g in self.groups if g['order']==order),None)
        user,_,_=self.data(uid)
        return (user,group) if live and user and group and not group.get('registration_only') else (None,None)

    def history_text(self,uid,page):
        with self.db() as c:
            rows=c.execute("SELECT * FROM bienvenida_eventos WHERE user_id=? AND source<>'HISTORICO' AND occurred_at>=(SELECT value FROM bienvenida_meta WHERE key='live_registration_started_at') ORDER BY occurred_at DESC,id DESC LIMIT 8 OFFSET ?",(uid,page*8)).fetchall()
        body=f'📖 <b>MOVIMIENTOS · ID {uid}</b>\nPágina {page+1}\n'
        for e in rows:
            g=self.by_chat[e['chat_id']]
            body+=f'\n{"🟢" if e["kind"]=="ENTRADA" else "🔴"} <b>{e["kind"]}</b> · {html.escape(g["short"])}\n{visible_date(e["occurred_at"])} · {e["source"]}\n'
        return body+'\nHora de Perú'

    async def reconcile(self,bot,uid):
        with self.db() as c:job=c.execute('SELECT revision FROM bienvenida_verificaciones WHERE user_id=?',(uid,)).fetchone()
        job_revision=job['revision'] if job else None
        failed=[]
        delay=60
        for g in self.groups:
            if g['chat_id'] is None:
                failed.append('ID pendiente: @'+g['username'])
                continue
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
                c.execute('UPDATE bienvenida_verificaciones SET next_try=?,error=? WHERE user_id=? AND revision=?',(time.time()+delay, '; '.join(failed)[:800],uid,job_revision))
            else:
                c.execute('DELETE FROM bienvenida_verificaciones WHERE user_id=? AND revision=?',(uid,job_revision))

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
            with self.db() as c:
                ready=c.execute("SELECT value FROM bienvenida_meta WHERE key='registration_ready_revision'").fetchone()
            if not ready or ready['value']!='candidate.8':
                params=dict(chat_id=DESTINATION,text='🛂 REGISTRO DE USUARIOS ACTIVO · v1.0.20-rc.2 CANDIDATA\n\nDestino comprobado. Se registran entradas, salidas y reingresos nuevos desde la activación original. Una salida real también inicia el seguimiento, sin inventar un ingreso previo. No se incorporan miembros antiguos por consultar su estado. Todos los botones y consultas se atienden en este grupo. Las bienvenidas personalizadas se conservan. Se eliminan los avisos de ingreso en los 21 grupos cuando Telegram lo permite.',disable_notification=True)
                if self.destination_thread is not None: params['message_thread_id']=self.destination_thread
                await bot.send_message(**params)
                with self.db() as c:
                    c.execute("INSERT OR REPLACE INTO bienvenida_meta VALUES('registration_ready_revision','candidate.8')")
        except Exception as exc:
            self.destination_checked=0
            self.archive_error='No puedo publicar en el grupo de registro: '+str(exc)
            with self.db() as c:
                c.execute("INSERT OR REPLACE INTO bienvenida_meta VALUES('destination_status',?)",(self.archive_error,))
            await self.notify_problem(bot,self.archive_error)
            raise

    async def notify_problem(self,bot,detail):
        # Registration diagnostics stay in its own storage/logs, never in control DM.
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO bienvenida_meta VALUES('registration_error',?)",(detail,))
        LOG.warning('Registro de usuarios: %s',detail)

    def view_payload(self,uid):
        from telegram import InlineKeyboardButton as B,InlineKeyboardMarkup
        with self.db() as c:
            stored=c.execute('SELECT kind,value FROM bienvenida_vistas WHERE user_id=?',(uid,)).fetchone()
        text=self.card_text(uid);keyboard=self.keyboard(uid)
        if stored and stored['kind']=='h':
            with self.db() as c:
                count=c.execute("SELECT COUNT(*) FROM bienvenida_eventos WHERE user_id=? AND source<>'HISTORICO' AND occurred_at>=(SELECT value FROM bienvenida_meta WHERE key='live_registration_started_at')",(uid,)).fetchone()[0]
            page=min(max(0,stored['value']),max(0,(count-1)//8))
            text+='\n━━━━━━━━━━━━━━━━━━━━\n'+self.history_text(uid,page)
            nav=[]
            if page:nav.append(B('⬅️ HISTORIAL',callback_data=f'wel:h:{uid}:{page-1}'))
            if (page+1)*8<count:nav.append(B('HISTORIAL ➡️',callback_data=f'wel:h:{uid}:{page+1}'))
            if nav:keyboard=InlineKeyboardMarkup([nav]+list(keyboard.inline_keyboard))
        return text,keyboard

    async def publish_one(self,bot):
        async with self.delivery_lock:
            return await self._publish_one_unlocked(bot)

    async def _publish_one_unlocked(self,bot):
        with self.db() as c:
            row=c.execute('SELECT * FROM bienvenida_fichas WHERE dirty=1 AND hidden=0 AND user_id IN (SELECT user_id FROM bienvenida_seguimiento) AND next_try<=? ORDER BY prioridad DESC,CASE WHEN move_event_id>published_event_id THEN 0 ELSE 1 END,move_event_id,next_try,user_id LIMIT 1',(time.time(),)).fetchone()
        if not row:
            return False
        uid=row['user_id']; message_id=row['message_id']
        old_message_id=message_id
        move=bool(message_id and row['move_event_id']>row['published_event_id'])
        if move:message_id=None
        try:
            await self.check_destination(bot)
            text,keyboard=self.view_payload(uid)
            kwargs=dict(chat_id=DESTINATION,text=text,parse_mode='HTML',reply_markup=keyboard)
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
                c.execute('UPDATE bienvenida_fichas SET message_id=?,published_event_id=?,dirty=CASE WHEN revision=? THEN 0 ELSE 1 END,error=NULL,next_try=0,prioridad=CASE WHEN revision=? THEN 0 ELSE prioridad END WHERE user_id=?',(message_id,row['move_event_id'],row['revision'],row['revision'],uid))
                if old_message_id and old_message_id!=message_id:
                    c.execute('INSERT OR IGNORE INTO bienvenida_fichas_anteriores(message_id,user_id,replacement_id) VALUES(?,?,?)',(old_message_id,uid,message_id))
            self.archive_error=''
            if old_message_id and old_message_id!=message_id:await self.retire_previous_card(bot,old_message_id)
        except Exception as exc:
            self.archive_error=(str(exc) if self.destination_checked else (self.archive_error or str(exc)))
            await self.notify_problem(bot,self.archive_error)
            if getattr(exc,'retry_after',None) is not None: self.api_pause_until=time.time()+self.retry_delay(exc)
            with self.db() as c:
                c.execute('UPDATE bienvenida_fichas SET error=?,next_try=? WHERE user_id=?',(str(exc),time.time()+self.retry_delay(exc),uid))
            LOG.warning('Ficha pendiente uid=%s: %s',uid,exc)
        return True

    async def retire_previous_card(self,bot,message_id=None):
        with self.db() as c:
            sql="SELECT * FROM bienvenida_fichas_anteriores WHERE state='PENDING' AND next_try<=? AND message_id NOT IN (SELECT message_id FROM bienvenida_fichas WHERE message_id IS NOT NULL)"
            args=[time.time()]
            if message_id:sql+=' AND message_id=?';args.append(message_id)
            row=c.execute(sql+' ORDER BY message_id LIMIT 1',args).fetchone()
        if not row:
            return False
        try:
            await bot.delete_message(chat_id=DESTINATION,message_id=row['message_id'])
            with self.db() as c:c.execute("UPDATE bienvenida_fichas_anteriores SET state='REMOVED',error=NULL WHERE message_id=?",(row['message_id'],))
        except Exception as exc:
            detail=str(exc)
            gone='message to delete not found' in detail.lower()
            blocked="can't be deleted" in detail.lower() or '48' in detail
            if blocked and row['replacement_id']:
                try:
                    link=f'https://t.me/c/{str(DESTINATION)[4:]}/{row["replacement_id"]}'
                    await bot.edit_message_text(chat_id=DESTINATION,message_id=row['message_id'],text='↪️ Ficha sustituida por un movimiento posterior.\nFicha vigente: '+link,reply_markup=None)
                    with self.db() as c:c.execute("UPDATE bienvenida_fichas_anteriores SET state='REPLACED',error=NULL WHERE message_id=?",(row['message_id'],))
                    return True
                except Exception as edit_error:detail+='; '+str(edit_error)
            with self.db() as c:c.execute('UPDATE bienvenida_fichas_anteriores SET state=?,error=?,next_try=? WHERE message_id=?',('REMOVED' if gone else ('BLOCKED' if blocked else 'PENDING'),detail,time.time()+max(60,self.retry_delay(exc)),row['message_id']))
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
                if time.monotonic()-last_publish>=3.2:
                    if await self.publish_one(bot):
                        last_publish=time.monotonic()
                    elif await self.retire_previous_card(bot):
                        last_publish=time.monotonic()
                # Las consultas tienen un trabajador separado para no demorar movimientos.
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
        text=(f'🛂 <b>REGISTRO DE USUARIOS · v1.0.20-rc.2 CANDIDATA</b>\n\n'
              '21 grupos registrados · Entradas, salidas y reingresos\n'
              f'Fichas publicadas: {cards} · Pendientes: {queued}\n\n'
              'Destino: <code>-1004303379365</code>\n'
              f'Ruta: {html.escape(self.destination_label)}\n'
              'Una ficha por usuario. Bienvenidas personalizadas conservadas. Avisos de ingreso con limpieza automática.\n'
              'Consultar usuario conocido: /pasaporte seguido de su ID, en privado.\n\n'
              'Máximo debe recibir eventos de miembros de los 21 grupos y poder publicar en el destino.\n'
              'Este registro no depende de leer mensajes de otros bots.\n')
        if self.archive_error:
            text+='\n<b>Archivo pendiente:</b> '+html.escape(self.archive_error[:350])

        return text

    async def open_passport(self,update,context):
        user=update.effective_user
        if not user or not self.authorized(user) or update.effective_chat.id!=DESTINATION:
            return
        try:
            uid=int(context.args[0])
            with self.db() as c:
                live=c.execute('SELECT 1 FROM bienvenida_seguimiento WHERE user_id=?',(uid,)).fetchone()
            if uid<=0 or not self.data(uid)[0] or not live:
                raise ValueError()
        except (IndexError,ValueError):
            await update.effective_message.reply_text('Solo se publican perfiles con un movimiento real registrado desde la activación del registro en vivo. Usa /pasaporte seguido de su ID.')
            return
        with self.db() as c:
            self.dirty(c,uid)
            c.execute('INSERT INTO bienvenida_verificaciones(user_id,prioridad) VALUES(?,2) ON CONFLICT(user_id) DO UPDATE SET next_try=0,prioridad=2,revision=revision+1',(uid,))
            c.execute('UPDATE bienvenida_fichas SET prioridad=MAX(prioridad,2) WHERE user_id=?',(uid,))
        await update.effective_message.reply_text(self.card_text(uid),parse_mode='HTML',reply_markup=self.keyboard(uid))

    async def callback(self,update,context):
        q=update.callback_query
        if not self.authorized(q.from_user):
            await q.answer('Solo administración.',show_alert=True)
            return
        if not q.message or q.message.chat.id!=DESTINATION:
            await q.answer('Este registro se consulta exclusivamente en INGRESO DE USUARIOS A GRUPOS.',show_alert=True)
            return
        try:
            parts=q.data.split(':')
            if parts[1]=='root':
                await q.answer('Usa las fichas publicadas en este grupo.',show_alert=True)
                return
            uid=int(parts[2])
            with self.db() as c:
                live=c.execute('SELECT 1 FROM bienvenida_seguimiento WHERE user_id=?',(uid,)).fetchone()
                card=c.execute('SELECT message_id FROM bienvenida_fichas WHERE user_id=?',(uid,)).fetchone()
            if not live or not card or card['message_id']!=q.message.message_id:
                await q.answer('Esta ficha ya no es la ficha activa del usuario.',show_alert=True)
                return
            if parts[1]=='d':
                await q.answer()
                async with self.delivery_lock:
                    with self.db() as c:
                        current=c.execute('SELECT message_id,move_event_id FROM bienvenida_fichas WHERE user_id=?',(uid,)).fetchone()
                    if not current or current['message_id']!=q.message.message_id:return
                    await context.bot.delete_message(chat_id=DESTINATION,message_id=q.message.message_id)
                    with self.db() as c:c.execute('UPDATE bienvenida_fichas SET message_id=NULL,dirty=CASE WHEN move_event_id=? THEN 0 ELSE 1 END,hidden=CASE WHEN move_event_id=? THEN 1 ELSE 0 END,revision=revision+1 WHERE user_id=? AND message_id=?',(current['move_event_id'],current['move_event_id'],uid,q.message.message_id))
                return
            if parts[1]=='i':
                user,_,_=self.data(uid)
                await q.answer('ID: '+str(uid)+' · USER: '+('@'+user['username'] if user['username'] else 'Sin @usuario público'),show_alert=True)
                return
            if parts[1]=='r':
                await q.answer('Estos nueve grupos sólo tienen registro y limpieza del aviso de ingreso. No se aplican reglas de moderación.',show_alert=True)
                return
            if parts[1]=='v':
                with self.db() as c:
                    c.execute('INSERT INTO bienvenida_verificaciones(user_id,prioridad) VALUES(?,2) ON CONFLICT(user_id) DO UPDATE SET next_try=0,prioridad=2,revision=revision+1',(uid,))
                    c.execute('UPDATE bienvenida_fichas SET prioridad=MAX(prioridad,2) WHERE user_id=?',(uid,))
                    self.dirty(c,uid)
                await q.answer('Verificación en cola. Esta ficha se actualizará aquí.')
                return
            kind=parts[1]
            if kind not in ('p','g','h','i'):
                await q.answer('Vista no disponible.',show_alert=True)
                return
            value=int(parts[3]) if kind in ('g','h') else 0
            if kind=='g' and value not in {g['order'] for g in self.groups}:
                await q.answer('Grupo no válido.',show_alert=True)
                return
            with self.db() as c:
                c.execute('INSERT OR REPLACE INTO bienvenida_vistas VALUES(?,?,?)',(uid,kind,value))
                self.dirty(c,uid)
            text,keyboard=self.view_payload(uid)
            await q.answer()
            await q.edit_message_text(text=text,parse_mode='HTML',reply_markup=keyboard)
        except Exception as exc:
            if 'message is not modified' in str(exc).lower():return
            LOG.warning('Consulta del registro en su grupo: %s',exc)
            try:await q.answer('No se pudo actualizar la vista. La ficha se reintentará aquí.',show_alert=True)
            except Exception:pass

    def next_verification(self):
        with self.db() as c:return c.execute('SELECT user_id FROM bienvenida_verificaciones WHERE next_try<=? ORDER BY prioridad DESC,next_try,user_id LIMIT 1',(time.time(),)).fetchone()

    async def verification_worker(self,bot):
        while True:
            try:
                if time.time()<self.api_pause_until:
                    await asyncio.sleep(min(5,self.api_pause_until-time.time()))
                    continue
                row=self.next_verification()
                if row:await self.reconcile(bot,row['user_id'])
            except asyncio.CancelledError:raise
            except Exception:LOG.exception('Registro 21: verificación pendiente; publicación de movimientos continúa')
            await asyncio.sleep(1)

    def start(self,bot):
        self.task=asyncio.create_task(self.worker(bot),name='bienvenidas-pasaportes')
        self.verification_task=asyncio.create_task(self.verification_worker(bot),name='registro21-verificaciones')

    async def stop(self):
        tasks=[t for t in (self.task,self.verification_task) if t]
        for task in tasks:task.cancel()
        for task in tasks:
            try:await task
            except asyncio.CancelledError:pass
