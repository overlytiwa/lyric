import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock
import time
import pytest

spec = importlib.util.spec_from_file_location('lyric', Path(__file__).with_name('lyric.py'))
l = importlib.util.module_from_spec(spec)
sys.modules['lyric'] = l
spec.loader.exec_module(l)

PLAN = {'kind': 'action', 'message': '', 'description': 'Create a greeting file in this folder.',
        'shell': 'powershell', 'command': "Set-Content -LiteralPath './hello.txt' -Value 'hello'"}

class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.calls = []
        self.edits = []
    def send(self, chat, text, markup=None):
        self.sent.append((chat, text, markup))
        return {'message_id': len(self.sent)}
    def edit(self, chat, message_id, text, markup=None):
        self.edits.append((chat, message_id, text, markup))
        return {'message_id': message_id}
    def call(self, method, data=None, files=None):
        self.calls.append((method, data))
        return True

def bot(tmp_path):
    b = l.Bot({'bot_token': 'test', 'xai_key': 'test', 'owner': 7}, tmp_path)
    b.tg = FakeTelegram()
    return b

def callback(user, ident, decision='yes', chat=None):
    return {'callback_query': {'id': 'q', 'from': {'id': user}, 'data': decision + ':' + ident,
            'message': {'message_id': 3, 'chat': {'id': user if chat is None else chat}}}}

def message(user, text, kind='private'):
    return {'message': {'from': {'id': user}, 'chat': {'id': user, 'type': kind}, 'text': text}}

def test_approval_is_one_use_owner_bound_and_expires():
    a = l.Approvals()
    ident = a.add(7, 7, PLAN, 'hello')
    assert a.take(ident, 8, 8) is None
    assert a.take(ident, 7, 7).plan == PLAN
    assert a.take(ident, 7, 7) is None
    ident = a.add(7, 7, PLAN, 'hello')
    a.items[ident] = l.Pending(7, 7, PLAN, time.monotonic() - 121, 'hello')
    assert a.take(ident, 7, 7) is None

def test_only_explicit_owner_allow_queues_execution(tmp_path):
    b = bot(tmp_path)
    ident = b.approvals.add(7, 7, PLAN, 'hello')
    b.update(callback(8, ident))
    assert b.jobs.empty()
    b.update(callback(7, ident, chat=-100))
    assert b.jobs.empty()
    b.update(callback(7, ident))
    job = b.jobs.get_nowait()
    assert job[0] == 'run' and job[3].plan == PLAN
    b.update(callback(7, ident))
    assert b.jobs.empty()

def test_cancel_never_executes(tmp_path):
    b = bot(tmp_path)
    ident = b.approvals.add(7, 7, PLAN, 'hello')
    b.update(callback(7, ident, 'no'))
    assert b.jobs.empty()
    assert not b.approvals.items
    ident = b.approvals.add(7, 7, PLAN, 'hello')
    b.update(message(7, '/cancel'))
    b.update(callback(7, ident))
    assert b.jobs.empty()

def test_unauthorized_messages_are_ignored(tmp_path):
    b = bot(tmp_path)
    b.update(message(8, 'delete something'))
    b.update(message(8, '/screenshot'))
    b.update(message(7, '/screenshot', 'group'))
    assert b.jobs.empty()
    assert not b.tg.sent

def test_pair_requires_local_code(tmp_path, monkeypatch):
    b = bot(tmp_path)
    b.config.pop('owner')
    saved = Mock()
    monkeypatch.setattr(l, 'save_config', saved)
    b.update(message(8, '/pair wrong'))
    assert not b.config.get('owner')
    b.update(message(7, '/pair ' + b.pair_code))
    assert b.config['owner'] == 7
    assert saved.call_count == 1
    b.update(message(8, '/pair ' + b.pair_code))
    assert b.config['owner'] == 7

def test_expired_pair_code_is_rejected(tmp_path):
    b = bot(tmp_path)
    b.config.pop('owner')
    b.pair_until = 0
    b.update(message(8, '/pair ' + b.pair_code))
    assert not b.config.get('owner')

def test_screenshot_only_for_owner(tmp_path):
    b = bot(tmp_path)
    b.update(message(7, '/screenshot'))
    assert b.jobs.get_nowait()[0] == 'screenshot'

def test_windows_protection_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(l, 'DATA', tmp_path)
    monkeypatch.setattr(l, 'CONFIG', tmp_path / 'settings.dat')
    data = {'bot_token': 'SECRET_BOT', 'provider': 'ollama'}
    l.save_config(data)
    assert b'SECRET' not in l.CONFIG.read_bytes()
    assert l.load_config() == data

def test_powershell_execution_in_selected_folder(tmp_path):
    result = l.run_action(PLAN, tmp_path)
    assert 'Exit code: 0' in result
    assert (tmp_path / 'hello.txt').read_text().strip() == 'hello'

def test_cmd_execution_and_failures(tmp_path):
    result = l.run_action(dict(PLAN, shell='cmd', command='echo greeting> cmd.txt'), tmp_path)
    assert 'Exit code: 0' in result
    assert (tmp_path / 'cmd.txt').read_text().strip() == 'greeting'
    result = l.run_action(dict(PLAN, command="throw 'deliberate test failure'"), tmp_path)
    assert 'Exit code: 1' in result

def test_execution_timeout(tmp_path):
    result = l.run_action(dict(PLAN, command='Start-Sleep -Seconds 20'), tmp_path, timeout=1)
    assert 'Stopped after the time limit' in result

@pytest.mark.parametrize('bad', [{}, dict(PLAN, command=''), dict(PLAN, shell='bash'), dict(PLAN, description='')])
def test_invalid_actions_rejected(bad):
    with pytest.raises(ValueError):
        l.validate_plan(bad)



def test_proposal_does_not_execute_and_hides_command(tmp_path):
    b = bot(tmp_path)
    b.ai.ask = Mock(return_value=PLAN)
    original = b.tg.edit
    def send_then_stop(*args, **kwargs):
        original(*args, **kwargs)
        b.stop.set()
    b.tg.edit = send_then_stop
    b.enqueue(('chat', 7, 7, 'create a greeting file'))
    b.worker()
    assert not (tmp_path / 'hello.txt').exists()
    assert len(b.approvals.items) == 1
    assert PLAN['command'] not in b.tg.edits[0][2]
    assert 'Allow once' == b.tg.edits[0][3]['inline_keyboard'][0][0]['text']

@pytest.fixture(autouse=True)
def isolate_local_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(l, 'DATA', tmp_path / 'appdata')
    monkeypatch.setattr(l, 'CONFIG', tmp_path / 'appdata' / 'settings.dat')

def test_computer_context_has_shell_paths_and_no_keys(tmp_path):
    context = l.computer_context(tmp_path)
    assert context['working_directory'] == str(tmp_path)
    assert context['powershell_version']
    assert context['architecture']
    assert isinstance(context['elevated'], bool)
    assert 'gemini_key' not in context and 'bot_token' not in context



def test_installer_preserves_path_and_avoids_duplicate(tmp_path, monkeypatch):
    exe = tmp_path / 'Lyric-Setup.exe'
    exe.write_bytes(b'test executable')
    monkeypatch.setattr(l, 'install_extension', Mock(return_value=tmp_path/'extension'))
    monkeypatch.setattr(l.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(l.sys, 'executable', str(exe))
    key = Mock()
    key.__enter__ = Mock(return_value=key)
    key.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(l.winreg, 'CreateKey', Mock(return_value=key))
    query = Mock(return_value=(r'C:\Existing\Bin', l.winreg.REG_EXPAND_SZ))
    monkeypatch.setattr(l.winreg, 'QueryValueEx', query)
    write = Mock()
    monkeypatch.setattr(l.winreg, 'SetValueEx', write)
    monkeypatch.setattr(l.ctypes, 'WinDLL', Mock(return_value=Mock()))
    monkeypatch.setattr('builtins.input', Mock(return_value='n'))
    l.install()
    assert (l.DATA / 'bin' / 'lyric.exe').read_bytes() == b'test executable'
    new_path = write.call_args.args[-1]
    assert new_path == r'C:\Existing\Bin' + ';' + str(l.DATA / 'bin')
    query.return_value = (new_path, l.winreg.REG_EXPAND_SZ)
    write.reset_mock()
    l.install()
    write.assert_not_called()




@pytest.mark.parametrize('command', [
    'Get-Date; Remove-Item x', 'Get-Date\nRemove-Item x', 'Get-Date | Invoke-Expression',
    'Get-Date $(Remove-Item x)', 'Get-Date > output.txt', 'Get-Date & calc',
    'Get-ChildItem -LiteralPath C:\\', 'Get-Content secret.txt', 'Invoke-WebRequest https://example.com',
    'get-process | select-object processname,id,cpu; calc', 'Get-Date # harmless'])
def test_readonly_rejects_added_syntax_and_arbitrary_reads(command):
    assert l.automatic_plan(dict(PLAN, command=command)) is None

def test_readonly_is_exact_and_can_be_disabled():
    plan = dict(PLAN, command='GET-DATE')
    assert l.automatic_plan(plan)['command'] == 'Get-Date'
    assert l.automatic_plan(plan, False) is None
    assert l.automatic_plan(dict(plan, shell='cmd')) is None
    assert l.automatic_plan(PLAN) is None

def test_auto_check_uses_single_card_without_approval(tmp_path, monkeypatch):
    b = bot(tmp_path)
    action = dict(PLAN, command='Get-Date', description='Untrusted model wording')
    def ask(text, result=False):
        if result:
            b.stop.set()
            return dict(PLAN, kind='reply', message='It is Wednesday.')
        return action
    b.ai.ask = ask
    run = Mock(return_value='Exit code: 0.\nWednesday')
    monkeypatch.setattr(l, 'run_action', run)
    b.enqueue(('chat', 7, 7, 'What day is it?'))
    b.worker()
    assert run.call_count == 1
    assert run.call_args.args[0]['description'] == 'Check the current date and time'
    assert not b.approvals.items
    assert len(b.tg.sent) == 1
    assert [entry[2] for entry in b.tg.edits] == ['Check the current date and time…', 'It is Wednesday.']
    assert all(entry[1] == 1 for entry in b.tg.edits)

def test_disabled_auto_check_requires_approval(tmp_path, monkeypatch):
    b = bot(tmp_path)
    b.config['auto_readonly'] = False
    b.ai.ask = Mock(return_value=dict(PLAN, command='Get-Date'))
    run = Mock()
    monkeypatch.setattr(l, 'run_action', run)
    original = b.tg.edit
    def edit(*args, **kwargs):
        original(*args, **kwargs)
        b.stop.set()
    b.tg.edit = edit
    b.enqueue(('chat', 7, 7, 'What day is it?'))
    b.worker()
    assert len(b.approvals.items) == 1
    run.assert_not_called()
    assert len(b.tg.sent) == 1

def test_approved_action_edits_existing_approval_card(tmp_path, monkeypatch):
    b = bot(tmp_path)
    pending = l.Pending(7, 7, PLAN, time.monotonic(), 'create file', 42)
    monkeypatch.setattr(l, 'run_action', Mock(return_value='Exit code: 0.'))
    def ask(*args):
        b.stop.set()
        return dict(PLAN, kind='reply', message='Created the file.')
    b.ai.ask = ask
    b.enqueue(('run', 7, 7, pending))
    b.worker()
    assert not b.tg.sent
    assert len(b.tg.edits) == 2
    assert all(entry[1] == 42 for entry in b.tg.edits)
    assert b.tg.edits[-1][2] == 'Created the file.'

def test_edit_failure_falls_back_to_one_message(tmp_path):
    b = bot(tmp_path)
    b.tg.edit = Mock(side_effect=l.FriendlyError('deleted'))
    assert b.card(7, 42, 'Done') == 1
    assert len(b.tg.sent) == 1

def test_telegram_edit_clears_buttons_and_limits_length():
    tg = l.Telegram('fake')
    tg.call = Mock()
    tg.edit(7, 42, 'x' * 5000)
    method, data = tg.call.call_args.args
    assert method == 'editMessageText'
    assert data['message_id'] == 42 and len(data['text']) <= 3800
    assert l.json.loads(data['reply_markup']) == {'inline_keyboard': []}

def test_normal_reply_edits_thinking_card(tmp_path):
    b = bot(tmp_path)
    def ask(*args):
        b.stop.set()
        return dict(PLAN, kind='reply', message='Hello there.')
    b.ai.ask = ask
    b.enqueue(('chat', 7, 7, 'Hello'))
    b.worker()
    assert len(b.tg.sent) == 1
    assert b.tg.edits[-1][2] == 'Hello there.'

def test_request_error_edits_thinking_card(tmp_path):
    b = bot(tmp_path)
    def ask(*args):
        b.stop.set()
        raise l.FriendlyError('Quota reached.')
    b.ai.ask = ask
    b.enqueue(('chat', 7, 7, 'Hello'))
    b.worker()
    assert len(b.tg.sent) == 1
    assert b.tg.edits[-1][2] == 'Quota reached.'

def test_cancel_callback_edits_card_without_new_message(tmp_path):
    b = bot(tmp_path)
    ident = b.approvals.add(7, 7, PLAN, 'create file', 3)
    b.update(callback(7, ident, 'no'))
    assert not b.tg.sent and not b.approvals.items and b.jobs.empty()
    assert b.tg.edits[-1][1:3] == (3, 'Cancelled. Nothing was run.')
    count = len(b.tg.edits)
    b.update(callback(7, ident, 'yes'))
    assert len(b.tg.edits) == count and b.jobs.empty()

def test_actual_builtin_readonly_check(tmp_path):
    plan = l.automatic_plan(dict(PLAN, command='Get-Location'))
    result = l.run_action(plan, tmp_path)
    assert 'Exit code: 0.' in result
    assert tmp_path.name in result



import extension_bridge as web

def test_migration_removes_old_keys_and_keeps_pairing():
    config=l.migrate_config({'xai_key':'drop','gemini_key':'drop','bot_token':'keep','owner':7,'auto_readonly':False})
    assert 'xai_key' not in config and 'gemini_key' not in config
    assert config['provider']=='gemini_extension'
    assert config['bot_token']=='keep' and config['owner']==7 and not config['auto_readonly']

def test_setup_only_needs_bot_token(monkeypatch,capsys):
    monkeypatch.setattr(l,'load_config',Mock(return_value={'bot_token':'keep'}))
    monkeypatch.setattr(l.getpass,'getpass',Mock(return_value=''))
    monkeypatch.setattr('builtins.input',Mock(return_value=''))
    monkeypatch.setattr(l,'save_config',Mock())
    config=l.setup(prepare_browser=False)
    assert config['bot_token']=='keep' and l.getpass.getpass.call_count==1
    assert 'No AI API key' in capsys.readouterr().out

@pytest.mark.parametrize('url,expected',[
    ('https://gemini.google.com/app',True),('https://gemini.google.com/app/abcdef',True),
    ('https://gemini.google.com/app?hl=en',True),('https://gemini.google.com/',False),
    ('https://gemini.google.com.evil.com/app',False),('http://gemini.google.com/app',False),
    ('https://accounts.google.com/app',False),('https://gemini.google.com:444/app',False),
    ('https://gemini.google.com/apply',False)])
def test_only_actual_gemini_app_url_is_accepted(url,expected):
    assert web.is_app_url(url)==expected


def test_full_message_is_parsed_with_escaped_command():
    assert l.parse_browser_plan(l.json.dumps(PLAN))==PLAN
    assert l.parse_browser_plan('```json\n'+l.json.dumps(PLAN)+'\n```')==PLAN

@pytest.mark.parametrize('text',['hello', '{"kind":"action"}',
    'Here is JSON: '+l.json.dumps(PLAN),l.json.dumps(dict(PLAN,command=''))])
def test_bad_browser_output_never_becomes_command(text):
    with pytest.raises(l.FriendlyError,match='No new command was executed'):
        l.parse_browser_plan(text)


def test_browser_prompt_contains_context_and_plain_text_protocol(tmp_path):
    ai=l.BrowserAI({},tmp_path)
    ai.browser=Mock()
    ai.browser.exchange.return_value=l.json.dumps(PLAN)
    assert ai.ask('create a greeting file')==PLAN
    prompt=ai.browser.exchange.call_args.args[0]
    assert 'COMPUTER CONTEXT' in prompt and 'powershell_version' in prompt
    assert 'plain text without Markdown' in prompt
    assert 'create a greeting file' in prompt

def test_summary_cannot_generate_followup_action(tmp_path):
    ai=l.BrowserAI({},tmp_path)
    ai.browser=Mock()
    ai.browser.exchange.return_value=l.json.dumps(PLAN)
    with pytest.raises(l.FriendlyError,match='summary was unavailable'):
        ai.ask('exit code 0',result=True)

def test_browser_failure_becomes_friendly_error(tmp_path):
    ai=l.BrowserAI({},tmp_path)
    ai.browser=Mock()
    ai.browser.exchange.side_effect=web.BrowserError('Sign in again')
    with pytest.raises(l.FriendlyError,match='Sign in again'):
        ai.ask('hello')




def test_no_paid_api_or_local_model_client_remains():
    source=Path(l.__file__).read_text(encoding='utf-8-sig')
    assert 'api.x.ai' not in source and 'generativelanguage.googleapis.com' not in source
    assert 'ollama_runtime' not in source

def test_ascii_banner_retained():
    assert l.BANNER.splitlines()[1]==' /$$       /$$     /$$ /$$$$$$$      /$$$$$$      /$$$$$$'

@pytest.mark.parametrize('text',[l.json.dumps(dict(PLAN,extra='unexpected')),
    '{"kind":"reply","kind":"action","message":"","description":"run","shell":"powershell","command":"Get-Date"}'])
def test_duplicate_and_unknown_browser_fields_rejected(text):
    with pytest.raises(l.FriendlyError):
        l.parse_browser_plan(text)

import io
import struct
import hashlib
import threading
import requests

def test_native_frames_roundtrip_unicode_and_partial_reads():
    stream=io.BytesIO()
    message={'text':'Hello ✓\nSecond line','type':'result'}
    web.write_frame(stream,message);stream.seek(0)
    assert web.read_frame(stream)==message
    assert web.read_frame(stream) is None

@pytest.mark.parametrize('raw',[b'12',struct.pack('<I',web.MAX_FRAME+1),struct.pack('<I',3)+b'x',struct.pack('<I',2)+b'[]'])
def test_bad_native_frames_rejected(raw):
    with pytest.raises(ValueError):web.read_frame(io.BytesIO(raw))

def test_extension_manifest_scope_and_stable_identity():
    import base64
    manifest=l.json.loads((Path(l.__file__).parent.parent/'extension'/'manifest.json').read_text(encoding='utf-8-sig'))
    assert set(manifest['permissions'])=={'nativeMessaging','storage'}
    assert manifest['host_permissions']==['https://gemini.google.com/*']
    digest=hashlib.sha256(base64.b64decode(manifest['key'])).hexdigest()[:32]
    assert ''.join(chr(97+int(c,16)) for c in digest)==web.EXTENSION_ID
    assert 'cookies' not in manifest['permissions'] and 'debugger' not in manifest['permissions']

@pytest.fixture
def broker(tmp_path):
    instance=web.Broker(tmp_path);instance.start()
    try:yield instance
    finally:instance.close()

def post_event(broker,event,client='test-client'):
    with requests.Session() as session:
        session.trust_env=False
        return session.post(f'http://127.0.0.1:{broker.server.server_port}/event',json=event,
            headers={'Authorization':'Bearer '+broker.token,'X-Lyric-Client':client},timeout=3)

def test_broker_requires_token_and_actual_gemini_url(broker):
    with requests.Session() as session:
        session.trust_env=False
        response=session.post(f'http://127.0.0.1:{broker.server.server_port}/event',json={'type':'status','ready':True,'url':'https://gemini.google.com/app'},timeout=3)
        assert response.status_code==401
    assert not broker.available()
    assert post_event(broker,{'type':'status','ready':True,'url':'https://evil.com/app'}).status_code==200
    assert not broker.available()
    post_event(broker,{'type':'status','ready':True,'url':'https://gemini.google.com/app'})
    assert broker.available()

def test_broker_matches_result_to_request_and_client(broker):
    post_event(broker,{'type':'status','ready':True,'url':'https://gemini.google.com/app'})
    result=[]
    thread=threading.Thread(target=lambda:result.append(broker.request('request','Hello',timeout=5)))
    thread.start()
    command=broker.commands.get(timeout=2)
    assert command['text']=='Hello'
    assert post_event(broker,{'type':'result','id':command['id'],'text':'wrong'},client='other-client').status_code==409
    post_event(broker,{'type':'result','id':'old-request','text':'old'})
    assert not result
    post_event(broker,{'type':'result','id':command['id'],'text':'correct'})
    thread.join(timeout=2)
    assert result==['correct']

def test_broker_timeout_does_not_keep_live_request(broker):
    post_event(broker,{'type':'status','ready':True,'url':'https://gemini.google.com/app'})
    with pytest.raises(web.BrowserError,match='did not finish'):
        broker.request('request','Hello',timeout=.05)
    assert not broker.pending

def test_broker_disconnect_clears_descriptor(tmp_path):
    instance=web.Broker(tmp_path);instance.start()
    assert (tmp_path/'extension-bridge.json').exists()
    instance.close()
    assert not (tmp_path/'extension-bridge.json').exists()

def test_existing_extension_skips_signin_prompt(monkeypatch,tmp_path):
    browser=web.GeminiBrowser(tmp_path)
    browser.broker=Mock();browser.broker.available.return_value=True
    monkeypatch.setattr(web,'install_extension',Mock(return_value=tmp_path/'extension'))
    prompt=Mock(side_effect=AssertionError('Should reuse the regular Chrome tab'))
    monkeypatch.setattr('builtins.input',prompt)
    browser.connect()
    prompt.assert_not_called()

def test_installer_registers_only_our_extension_origin(tmp_path,monkeypatch):
    exe=tmp_path/'lyric.exe';exe.write_bytes(b'test executable')
    monkeypatch.setattr(web.sys,'frozen',True,raising=False)
    monkeypatch.setattr(web.sys,'executable',str(exe))
    monkeypatch.setattr(web.sys,'_MEIPASS',str(Path(l.__file__).parent.parent),raising=False)
    key=Mock();key.__enter__=Mock(return_value=key);key.__exit__=Mock(return_value=False)
    monkeypatch.setattr(web.winreg,'CreateKey',Mock(return_value=key))
    write=Mock();monkeypatch.setattr(web.winreg,'SetValueEx',write)
    target=web.install_extension(tmp_path/'installed')
    manifest=l.json.loads((tmp_path/'installed'/'native-host.json').read_text())
    assert manifest['allowed_origins']==[web.ORIGIN]
    assert manifest['type']=='stdio'
    assert Path(manifest['path']).read_bytes()==b'test executable'
    assert (target/'manifest.json').exists()
    assert web.HOST in web.winreg.CreateKey.call_args.args[1]

def test_regular_chrome_has_no_debugging_or_profile_flags():
    source=Path(web.__file__).read_text(encoding='utf-8-sig')
    assert '--remote-debugging' not in source and '--user-data-dir' not in source
    assert 'playwright' not in source

# Full native-host transport, with a simulated extension and a real loopback broker.
def test_native_host_process_relays_requests_and_results(tmp_path):
    import subprocess,sys,os,queue
    instance=web.Broker(tmp_path/'native-data');instance.start()
    source=str(Path(l.__file__).parent)
    code="import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from extension_bridge import native_main;native_main(Path(sys.argv[2]),sys.argv[3:])"
    proc=subprocess.Popen([sys.executable,'-u','-c',code,source,str(instance.data),web.ORIGIN],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    frames=queue.Queue()
    def read():
        try:
            while True:
                value=web.read_frame(proc.stdout)
                if value is None:break
                frames.put(value)
        except (ValueError,OSError):pass
    reader=threading.Thread(target=read,daemon=True);reader.start()
    try:
        web.write_frame(proc.stdin,{'type':'status','ready':True,'url':'https://gemini.google.com/app'})
        deadline=time.monotonic()+10
        while not instance.available() and time.monotonic()<deadline:time.sleep(.05)
        assert instance.available()
        result=[]
        worker=threading.Thread(target=lambda:result.append(instance.request('request','native roundtrip',timeout=10)))
        worker.start()
        command=None
        while time.monotonic()<deadline:
            message=frames.get(timeout=5)
            if message.get('type')=='request':command=message;break
        assert command and command['text']=='native roundtrip'
        web.write_frame(proc.stdin,{'type':'result','id':command['id'],'text':'native answer ✓'})
        worker.join(timeout=6)
        assert result==['native answer ✓']
    finally:
        proc.stdin.close()
        try:proc.wait(timeout=8)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        instance.close()
    assert proc.returncode==0,proc.stderr.read().decode(errors='replace')

def test_second_broker_cannot_replace_active_connection(tmp_path):
    first=web.Broker(tmp_path);first.start()
    second=web.Broker(tmp_path)
    try:
        with pytest.raises(web.BrowserError,match='already connected'):
            second.start()
        descriptor=l.json.loads((tmp_path/'extension-bridge.json').read_text())
        assert descriptor['token']==first.token
    finally:second.close();first.close()

BROKEN_THEME_RESPONSE = r'''{
"kind": "action",
"message": "",
"description": "Modifies system registry settings under HKEY_CURRENT_USER to set Windows Apps and System theme modes to Light.",
"shell": "powershell",
"command": "Set-ItemProperty -Path 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize' -Name 'AppsUseLightTheme' -Value 1; Set-ItemProperty -Path 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize' -Name 'SystemUsesLightTheme' -Value 1"
}'''


def test_user_theme_response_repairs_only_json_path_escaping():
    expected = r"Set-ItemProperty -Path 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize' -Name 'AppsUseLightTheme' -Value 1; Set-ItemProperty -Path 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize' -Name 'SystemUsesLightTheme' -Value 1"
    result = l.parse_browser_plan(BROKEN_THEME_RESPONSE)
    assert result['command'] == expected
    assert result['kind'] == 'action' and result['message'] == ''
    assert l.automatic_plan(result) is None


@pytest.mark.parametrize('path', [r'C:\Users\Example\file.txt', r'C:\temp\new\file.txt',
    r'HKCU:\Software\test\new', r"C:\Users\O''Brien\file.txt", r'C:\users\name\file.txt'])
def test_windows_path_escapes_do_not_become_tabs_or_newlines(path):
    raw = '{"kind":"action","message":"","description":"Check a path","shell":"powershell","command":"Get-Item -LiteralPath \'' + path + '\'"}'
    result = l.parse_browser_plan(raw)
    assert result['command'] == "Get-Item -LiteralPath '" + path + "'"
    assert not l.windows_path_controls(result['command'])


def test_valid_json_command_newlines_quotes_unicode_are_preserved():
    command = "Write-Output \"Hello\"\nGet-Item -LiteralPath 'C:\\café\\file.txt'"
    value = dict(PLAN, command=command)
    assert l.parse_browser_plan(l.json.dumps(value)) == value


def test_valid_unicode_path_not_rewritten_when_other_path_needs_repair():
    raw = r'''{"kind":"action","message":"","description":"Check paths","shell":"powershell","command":"Get-Item 'C:\\caf\u00e9'; Get-Item 'HKCU:\Software\Test'"}'''
    result = l.parse_browser_plan(raw)
    assert result['command'] == "Get-Item 'C:\\café'; Get-Item 'HKCU:\\Software\\Test'"


@pytest.mark.parametrize('raw', [
    '{"kind":"action","message":"","description":"x","shell":"powershell","command":"Get-Date",}',
    BROKEN_THEME_RESPONSE.replace('"message": "",', '"message": "", "message": "duplicate",'),
    BROKEN_THEME_RESPONSE[:-2],
    'Here is your response: ' + BROKEN_THEME_RESPONSE,
])
def test_path_repair_does_not_relax_json_structure_or_duplicate_fields(raw):
    with pytest.raises(l.FriendlyError):
        l.parse_browser_plan(raw)


def test_repaired_registry_action_still_waits_for_owner_approval(tmp_path,monkeypatch):
    b = bot(tmp_path)
    b.ai.ask = Mock(return_value=l.parse_browser_plan(BROKEN_THEME_RESPONSE))
    execute = Mock()
    monkeypatch.setattr(l,'run_action',execute)
    edit = b.tg.edit
    def stop_after_card(*args,**kwargs):
        edit(*args,**kwargs)
        b.stop.set()
    b.tg.edit=stop_after_card
    b.enqueue(('chat',7,7,'Use light mode'))
    b.worker()
    execute.assert_not_called()
    assert len(b.approvals.items)==1
    assert 'Allow once' in str(b.tg.edits[-1][3])
