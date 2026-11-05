"""Authenticated loopback relay for the Chrome native-messaging host."""
from __future__ import annotations
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import os
from pathlib import Path
import queue
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
import winreg
import msvcrt
import requests

EXTENSION_ID = 'hhgbcadklafamkngbpdjajmhojdmnppl'
ORIGIN = 'chrome-extension://' + EXTENSION_ID + '/'
HOST = 'com.lyric.gemini_bridge'
MAX_FRAME = 1024 * 1024
FILES = ['manifest.json','background.js','content.js','popup.html','popup.js']

class BrowserError(Exception):
    pass

def is_app_url(url):
    try:
        p=urlparse(url)
        return p.scheme=='https' and p.hostname=='gemini.google.com' and p.port in (None,443) and (p.path=='/app' or p.path.startswith('/app/'))
    except (ValueError,TypeError):
        return False

def find_chrome():
    for root in [os.environ.get('PROGRAMFILES',''),os.environ.get('PROGRAMFILES(X86)',''),os.environ.get('LOCALAPPDATA','')]:
        if root and (path:=Path(root)/'Google/Chrome/Application/chrome.exe').is_file():
            return path
    raise BrowserError('Google Chrome was not found. Install Chrome, then run Lyric again.')

def install_extension(data):
    if not getattr(sys,'frozen',False):
        raise BrowserError('Use the packaged Lyric installer to register its Chrome extension connection.')
    data=Path(data)
    source=Path(getattr(sys,'_MEIPASS',Path(__file__).parent.parent))/'extension'
    target=data/'extension'
    target.mkdir(parents=True,exist_ok=True)
    for name in FILES:
        if (source/name).resolve()!=(target/name).resolve():
            shutil.copy2(source/name,target/name)
    digest=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()[:12]
    native=data/'bin'/('Lyric-native-'+digest+'.exe')
    native.parent.mkdir(parents=True,exist_ok=True)
    if not native.exists():
        shutil.copy2(sys.executable,native)
    manifest=data/'native-host.json'
    manifest.write_text(json.dumps({'name':HOST,'description':'Lyric Gemini connection',
        'path':str(native.resolve()),'type':'stdio','allowed_origins':[ORIGIN]},indent=2),encoding='utf-8')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,'Software\\Google\\Chrome\\NativeMessagingHosts\\'+HOST) as key:
        winreg.SetValueEx(key,'',0,winreg.REG_SZ,str(manifest.resolve()))
    return target

class Broker:
    def __init__(self,data):
        self.data=Path(data)
        self.token=secrets.token_urlsafe(32)
        self.commands=queue.Queue()
        self.pending={}
        self.lock=threading.Lock()
        self.ready=False
        self.client=None
        self.last_seen=0
        self.server=None
        self.file_lock=None
    def start(self):
        self.data.mkdir(parents=True,exist_ok=True)
        self.file_lock=(self.data/'extension-session.lock').open('a+b')
        self.file_lock.seek(0,2)
        if self.file_lock.tell()==0:
            self.file_lock.write(b'0');self.file_lock.flush()
        self.file_lock.seek(0)
        try: msvcrt.locking(self.file_lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            self.file_lock.close();self.file_lock=None
            raise BrowserError('Lyric is already connected to Chrome. Stop its other terminal before opening settings or extension setup.') from None
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def reply(self,code,obj):
                raw=json.dumps(obj,ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                try: self.wfile.write(raw)
                except (OSError,ConnectionError): pass
            def authorized(self):
                value=self.headers.get('Authorization','')
                return secrets.compare_digest(value,'Bearer '+owner.token)
            def do_GET(self):
                if not self.authorized(): return self.reply(401,{'error':'Unauthorized'})
                if self.path!='/poll': return self.reply(404,{})
                client=self.headers.get('X-Lyric-Client','')
                with owner.lock:
                    if owner.client!=client: return self.reply(409,{'error':'Another Chrome connection is active'})
                    owner.last_seen=time.monotonic()
                try: command=owner.commands.get(timeout=1)
                except queue.Empty: command={'type':'idle'}
                if command.get('id'):
                    with owner.lock:
                        item=owner.pending.get(command['id'])
                        if item and item[1]!=client:
                            if not item[0].done(): item[0].set_exception(BrowserError('The Chrome connection changed. Please reconnect the intended Gemini tab.'))
                            command={'type':'idle'}
                        elif not item: command={'type':'idle'}
                return self.reply(200,command)
            def do_POST(self):
                if not self.authorized(): return self.reply(401,{'error':'Unauthorized'})
                if self.path!='/event': return self.reply(404,{})
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=MAX_FRAME: return self.reply(413,{})
                    event=json.loads(self.rfile.read(size))
                    if not isinstance(event,dict): return self.reply(400,{})
                except (ValueError,OSError): return self.reply(400,{})
                client=self.headers.get('X-Lyric-Client','')
                with owner.lock:
                    if not client or (owner.client not in (None,client) and time.monotonic()-owner.last_seen<15):
                        return self.reply(409,{'error':'Another Chrome connection is active'})
                    if event.get('type')=='status':
                        ready=event.get('ready') is True and is_app_url(event.get('url',''))
                        if ready:
                            owner.client=client
                            owner.ready=True
                            owner.last_seen=time.monotonic()
                        elif owner.client==client:
                            owner.ready=False
                            owner.client=None
                    elif event.get('type')=='result':
                        item=owner.pending.get(event.get('id'))
                        if item and item[1]==client and not item[0].done():
                            item[0].set_result(event)
                return self.reply(200,{'ok':True})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        path=self.data/'extension-bridge.json'
        temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps({'port':self.server.server_port,'token':self.token}),encoding='utf-8')
        temp.replace(path)
    def available(self):
        with self.lock:
            return self.ready and time.monotonic()-self.last_seen<10
    def request(self,kind,text='',timeout=250):
        if not self.available():
            raise BrowserError('The Gemini extension is not ready. Open Gemini in your normal Chrome, sign in, then click Lyric → Connect this tab.')
        ident=secrets.token_hex(16)
        future=concurrent.futures.Future()
        with self.lock:
            self.pending[ident]=(future,self.client)
        self.commands.put({'type':kind,'id':ident,'text':text})
        try:
            deadline=time.monotonic()+timeout
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0: raise concurrent.futures.TimeoutError()
                try:
                    event=future.result(timeout=min(1,remaining))
                    break
                except concurrent.futures.TimeoutError:
                    if not self.available():
                        raise BrowserError('The Chrome extension disconnected. Reconnect its Gemini tab; Lyric did not resend the request.') from None
            if event.get('error'): raise BrowserError(str(event['error'])[:1000])
            return event.get('text','')
        except concurrent.futures.TimeoutError:
            raise BrowserError('Gemini did not finish in time. Check the Chrome tab. Lyric did not resend the message or execute a new command.') from None
        finally:
            with self.lock: self.pending.pop(ident,None)
    def close(self):
        with self.lock:
            for future,_ in self.pending.values():
                if not future.done(): future.set_exception(BrowserError('Lyric stopped.'))
        path=self.data/'extension-bridge.json'
        try:
            if path.exists() and json.loads(path.read_text()).get('token')==self.token: path.unlink()
        except (OSError,ValueError): pass
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server=None
        if self.file_lock:
            self.file_lock.close();self.file_lock=None

def read_frame(stream):
    header=stream.read(4)
    if not header: return None
    if len(header)!=4: raise ValueError('Incomplete native header')
    size=struct.unpack('<I',header)[0]
    if not 0<size<=MAX_FRAME: raise ValueError('Invalid native frame size')
    raw=bytearray()
    while len(raw)<size:
        part=stream.read(size-len(raw))
        if not part: raise ValueError('Incomplete native payload')
        raw.extend(part)
    value=json.loads(raw.decode('utf-8'))
    if not isinstance(value,dict): raise ValueError('Invalid native message')
    return value

def write_frame(stream,message):
    raw=json.dumps(message,ensure_ascii=False).encode('utf-8')
    if len(raw)>MAX_FRAME: raise ValueError('Native message too large')
    stream.write(struct.pack('<I',len(raw))+raw)
    stream.flush()

def native_main(data,args):
    if ORIGIN not in args: return
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(),os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(),os.O_BINARY)
    incoming=queue.Queue(maxsize=20)
    stopped=threading.Event()
    client=secrets.token_hex(16)
    def reader():
        try:
            while not stopped.is_set():
                event=read_frame(sys.stdin.buffer)
                if event is None: break
                incoming.put(event,timeout=2)
        except (ValueError,OSError,queue.Full): pass
        finally: stopped.set()
    threading.Thread(target=reader,daemon=True).start()
    latest_status={'type':'status','ready':False,'url':''}
    previous=None
    offline_since=time.monotonic()
    try:
        with requests.Session() as session:
            session.trust_env=False
            while not stopped.is_set():
                try:
                    descriptor=json.loads((Path(data)/'extension-bridge.json').read_text())
                    port=descriptor['port']
                    token=descriptor['token']
                    if not isinstance(port,int) or not 1<=port<=65535 or not isinstance(token,str): raise ValueError()
                    url=f'http://127.0.0.1:{port}'
                    headers={'Authorization':'Bearer '+token,'X-Lyric-Client':client}
                    identity=(port,token)
                    if identity!=previous:
                        session.post(url+'/event',headers=headers,json=latest_status,timeout=3)
                        write_frame(sys.stdout.buffer,{'type':'host-connected'})
                        previous=identity
                    while not incoming.empty():
                        event=incoming.get_nowait()
                        if event.get('type')=='status': latest_status=event
                        r=session.post(url+'/event',headers=headers,json=event,timeout=3)
                        if r.status_code==409: write_frame(sys.stdout.buffer,{'type':'host-error','error':'Another Chrome profile is connected to Lyric.'})
                    r=session.get(url+'/poll',headers=headers,timeout=3)
                    if r.status_code==200:
                        offline_since=time.monotonic()
                        command=r.json()
                        if command.get('type')!='idle': write_frame(sys.stdout.buffer,command)
                    else:
                        previous=None
                        stopped.wait(1)
                except (OSError,ValueError,KeyError,requests.RequestException):
                    previous=None
                    # Keep the port alive, but do not start Lyric or make external requests.
                    while not incoming.empty():
                        event=incoming.get_nowait()
                        if event.get('type')=='status': latest_status=event
                    write_frame(sys.stdout.buffer,{'type':'host-offline'})
                    if time.monotonic()-offline_since>60: break
                    stopped.wait(2)
    except (OSError,ValueError): pass
    finally: stopped.set()

class GeminiBrowser:
    def __init__(self,data):
        self.data=Path(data)
        self.broker=Broker(data)
    def connect(self):
        folder=install_extension(self.data)
        self.broker.start()
        for _ in range(12):
            if self.broker.available():
                print('Gemini extension connected. Reusing your regular Chrome tab.\n')
                return
            time.sleep(0.25)
        chrome=find_chrome()
        print('\nUsing your regular Chrome profile. Sign in normally at Gemini.google.com.')
        print('One-time extension setup: chrome://extensions → Developer mode → Load unpacked')
        print('Choose this folder: '+str(folder))
        print('Then open Gemini and click the Lyric extension → Connect this tab.\n')
        marker=self.data/'extension-connected'
        pages=['https://gemini.google.com/app']
        if not marker.exists(): pages.insert(0,'chrome://extensions')
        subprocess.Popen([str(chrome)]+pages,stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(20):
            if self.broker.available():
                marker.write_text('Connected through Chrome native messaging.',encoding='utf-8')
                print('Gemini extension connected. Reusing your normal Chrome sign-in.\n')
                return
            time.sleep(0.25)
        print('If the extension is not installed yet, open chrome://extensions in Chrome.')
        while True:
            input('After signing in and connecting the extension, press Enter here (Ctrl+C to stop): ')
            for _ in range(20):
                if self.broker.available():
                    marker.write_text('Connected through Chrome native messaging.',encoding='utf-8')
                    print('Verified Gemini chat page. Lyric is ready.\n')
                    return
                time.sleep(0.25)
            print('Not connected yet. Check that the extension is enabled on Gemini’s /app page and click Connect this tab.')
    def exchange(self,text): return self.broker.request('request',text)
    def reset(self): self.broker.request('reset',timeout=30)
    def close(self): self.broker.close()
