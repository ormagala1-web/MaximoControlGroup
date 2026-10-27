"""Consulta aislada 12+9: no importa ni escribe reglas, movimientos o bienvenidas."""
from __future__ import annotations
import asyncio
import hmac
import json
import os
import time
from collections import OrderedDict
from datetime import datetime,timezone
from pathlib import Path
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop

_SERVICE=None
def read_key(backup_secret=None):
    key=os.environ.get('MEMBERSHIP_BRIDGE_SECRET','').strip()
    if not 16<=len(key)<=200:return ''
    # No aceptar la clave de respaldos como clave de esta interfaz.
    if backup_secret and hmac.compare_digest(key.encode('utf-8'),backup_secret.encode('utf-8')):return ''
    return key

def bind_service(service):
    global _SERVICE
    _SERVICE=service

def state_of(member):
    status=str(member.status)
    if status in {'creator','administrator','member'}:return True
    if status=='restricted':return bool(member.is_member)
    if status in {'left','kicked'}:return False
    return None

def stamp():
    return datetime.now(timezone.utc).isoformat()

class MembershipService:
    def __init__(self,bot,loop=None,catalog=None):
        self.bot=bot
        self.loop=loop or asyncio.get_running_loop()
        self.groups=json.loads(Path(catalog or Path(__file__).with_name('pertenencia_grupos.json')).read_text(encoding='utf-8-sig'))
        assert len(self.groups)==21 and all(not g.get('query_only',False) for g in self.groups)
        self.by_name={g['username'].lower():g for g in self.groups}
        assert len(self.by_name)==21
        self.cache=OrderedDict()
        self.admin_cache={}
        self.semaphore=asyncio.Semaphore(3)
        self.pause_until=0
        self.closed=False
    def put(self,key,result):
        self.cache[key]=(time.monotonic(),result)
        self.cache.move_to_end(key)
        while len(self.cache)>10000:self.cache.popitem(last=False)
    async def access(self,group,force=False):
        name=group['username'].lower()
        entry=self.admin_cache.get(name)
        if not force and entry and time.monotonic()-entry[0]<60:return entry[1]
        result=dict(chat_id=group['chat_id'],bot_admin=None,error=None)
        try:
            if time.monotonic()<self.pause_until:
                result['error']='rate_limited';return result
            if not group['chat_id']:
                chat=await self.bot.get_chat('@'+group['username'],read_timeout=6,connect_timeout=5,pool_timeout=5)
                if str(chat.type) not in {'group','supergroup'}:
                    result['error']='not_a_group';return result
                group['chat_id']=chat.id
            result['chat_id']=group['chat_id']
            own=await self.bot.get_chat_member(group['chat_id'],self.bot.id,read_timeout=6,connect_timeout=5,pool_timeout=5)
            result['bot_admin']=str(own.status) in {'creator','administrator'}
            if not result['bot_admin']:result['error']='maximo_not_admin'
        except TelegramError as error:
            result['error']=self.failure(error)
        self.admin_cache[name]=(time.monotonic(),result)
        return result
    def failure(self,error):
        if getattr(error,'retry_after',None) is not None:
            delay=error.retry_after
            seconds=delay.total_seconds() if hasattr(delay,'total_seconds') else float(delay)
            self.pause_until=time.monotonic()+max(1,seconds)
            return 'rate_limited'
        if type(error).__name__ in {'TimedOut','NetworkError'}:return 'telegram_unavailable'
        return 'maximo_no_access'
    async def one(self,group,uid,probe,force):
        name=group['username'].lower();key=(uid,name)
        before=self.cache.get(key)
        if not probe and not force and before and time.monotonic()-before[0]<30:return dict(before[1])
        async with self.semaphore:
            access=await self.access(group,force)
            result=dict(username=group['username'],chat_id=access['chat_id'],member=None,
                status=None,checked_at=stamp(),bot_admin=access['bot_admin'],error=access['error'])
            if probe or not access['bot_admin']:
                return result
            if time.monotonic()<self.pause_until:
                result['error']='rate_limited';return result
            try:
                member=await self.bot.get_chat_member(access['chat_id'],uid,read_timeout=6,connect_timeout=5,pool_timeout=5)
                result.update(member=state_of(member),status=str(member.status))
                # Un evento recibido durante la consulta tiene prioridad.
                current=self.cache.get(key)
                if current is not before and current is not None:return dict(current[1])
                if result['member'] is not None:self.put(key,result)
            except TelegramError as error:
                result['error']=self.failure(error)
            return result
    async def query(self,payload):
        if self.closed:raise RuntimeError('service_stopped')
        if not isinstance(payload,dict):raise ValueError('invalid_request')
        probe=payload.get('probe',False);force=payload.get('force',False)
        if not isinstance(probe,bool) or not isinstance(force,bool):raise ValueError('invalid_flags')
        uid=payload.get('user_id')
        if not probe and (isinstance(uid,bool) or not isinstance(uid,int) or not 0<uid<2**63):
            raise ValueError('invalid_user')
        names=payload.get('groups')
        if not isinstance(names,list) or not 1<=len(names)<=21 or any(not isinstance(n,str) for n in names):
            raise ValueError('invalid_groups')
        groups=[]
        for name in names:
            group=self.by_name.get(name.lstrip('@').lower())
            if group is None:raise ValueError('group_not_registered')
            if group not in groups:groups.append(group)
        items=await asyncio.gather(*(self.one(g,uid,probe,force) for g in groups))
        return dict(ok=True,provider='@MaximoControlGroup_bot',user_id=uid if not probe else None,
            groups=items,checked_at=stamp())
    async def observe(self,update,context):
        event=getattr(update,'chat_member',None) or getattr(update,'my_chat_member',None)
        chat=event.chat if event else getattr(update,'effective_chat',None)
        if chat is None:return
        name=str(getattr(chat,'username',None) or '').lower()
        group=self.by_name.get(name) or next((g for g in self.groups if g['chat_id']==chat.id),None)
        if group is None:return
        group['chat_id']=chat.id
        if event:
            member=event.new_chat_member
            if getattr(update,'my_chat_member',None):
                self.admin_cache.pop(group['username'].lower(),None)
                if str(member.status) not in {'creator','administrator'}:
                    for key in list(self.cache):
                        if key[1]==group['username'].lower():self.cache.pop(key,None)
            elif not member.user.is_bot:
                key=(member.user.id,group['username'].lower())
                self.put(key,dict(username=group['username'],chat_id=chat.id,member=state_of(member),
                    status=str(member.status),checked_at=stamp(),bot_admin=True,error=None))


def handle_http(handler,backup_secret):
    """Únicamente /membership/status; usa una clave independiente de respaldos."""
    key=read_key(backup_secret)
    received=handler.headers.get('Authorization','').encode('utf-8')
    expected=('Bearer '+key).encode('utf-8')
    if not key or not hmac.compare_digest(received,expected):
        handler._json(401,{'ok':False,'error':'unauthorized'});return
    if _SERVICE is None or _SERVICE.closed or not _SERVICE.loop.is_running():
        handler._json(503,{'ok':False,'error':'maximo_not_ready'});return
    try:
        length=int(handler.headers.get('Content-Length','0'))
        if not 0<length<=8192:raise ValueError('invalid_size')
        payload=json.loads(handler.rfile.read(length))
        future=asyncio.run_coroutine_threadsafe(asyncio.wait_for(_SERVICE.query(payload),22),_SERVICE.loop)
        try:result=future.result(timeout=24)
        except TimeoutError:
            future.cancel();handler._json(504,{'ok':False,'error':'query_timeout'});return
        handler._json(200,result)
    except (ValueError,TypeError):handler._json(400,{'ok':False,'error':'invalid_query'})
    except Exception:handler._json(503,{'ok':False,'error':'query_unavailable'})
