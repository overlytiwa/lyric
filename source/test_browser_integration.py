from pathlib import Path
import sys
import json
import pytest
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).parent))
import extension_bridge as web

HTML=r'''<!doctype html><html><body>
<model-response><message-content><div id="model-response-message-content-old" aria-busy="false">Old response must not be reused.</div></message-content></model-response>
<rich-textarea><div contenteditable="true" role="textbox"></div></rich-textarea>
<button aria-label="Send message" onclick="send()">Send</button>
<script>
let turn=0;
function send(){
 window.lastPrompt=document.querySelector('[contenteditable]').innerText;
 document.querySelector('[contenteditable]').innerText='';
 const el=document.createElement('model-response');
 el.innerHTML='<message-content><div id="model-response-message-content-new'+(++turn)+'" aria-busy="true">Partial</div></message-content>';
 document.querySelector('rich-textarea').before(el);
 setTimeout(()=>{
  const c=el.querySelector('div');
  const value=JSON.stringify({kind:'reply',message:'Full response '+turn+' with multiple paragraphs.',description:'',shell:'powershell',command:''});
  if(window.mode==='code'){
   c.innerHTML='<div>json</div><button>Copy code</button><pre><code></code></pre>';
   c.querySelector('code').innerText=value;
  }else if(window.mode==='paragraphs'){c.innerHTML='<p><span>First</span><span> paragraph.</span></p><p>Second paragraph.</p>';}
  else c.textContent=value;
  c.setAttribute('aria-busy','false');
 },200);
}
</script></body></html>'''

STUB='''window.chrome={runtime:{id:'test-extension',onMessage:{addListener(fn){window.lyricListener=fn}},sendMessage(){return Promise.resolve({})}}};'''

@pytest.fixture(scope='module')
def chrome_browser():
    try:exe=web.find_chrome()
    except web.BrowserError:pytest.skip('Chrome not installed')
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=str(exe),headless=True)
        try:yield browser
        finally:browser.close()

@pytest.fixture
def page(chrome_browser):
    context=chrome_browser.new_context()
    context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=HTML))
    context.add_init_script(STUB)
    page=context.new_page();page.goto('https://gemini.google.com/app')
    page.add_script_tag(path=str(Path(__file__).parent.parent/'extension'/'content.js'))
    try:yield page
    finally:context.close()

def invoke(page,text='Hello',kind='request'):
    return page.evaluate('''([text,kind]) => new Promise(resolve=>window.lyricListener({type:'lyric-command',command:{type:kind,id:'test-request',text}},{id:'test-extension'},resolve))''',[text,kind])

def test_content_script_sends_and_reads_new_complete_turns(page):
    first=invoke(page,'A Unicode prompt ✓')
    assert not first.get('error')
    assert json.loads(first['text'])['message'].startswith('Full response 1')
    assert page.evaluate('window.lastPrompt')=='A Unicode prompt ✓'
    second=invoke(page,'Second prompt')
    assert json.loads(second['text'])['message'].startswith('Full response 2')
    assert 'Partial' not in second['text'] and 'Old response' not in second['text']

def test_content_script_extracts_all_paragraphs_and_spans(page):
    page.evaluate("window.mode='paragraphs'")
    result=invoke(page)
    assert 'First paragraph.' in result['text'] and 'Second paragraph.' in result['text']

def test_content_script_normalizes_code_block_ui_labels(page):
    page.evaluate("window.mode='code'")
    result=invoke(page)
    assert json.loads(result['text'])['kind']=='reply'
    assert 'Copy code' not in result['text']

def test_content_script_preserves_unsent_draft(page):
    page.locator('[contenteditable]').fill('My personal unsent draft')
    result=invoke(page)
    assert 'unsent draft' in result['error']
    assert page.locator('[contenteditable]').inner_text()=='My personal unsent draft'

def test_content_script_rejects_outside_gemini_origin(page):
    page.goto('https://example.test/app')
    page.add_script_tag(path=str(Path(__file__).parent.parent/'extension'/'content.js'))
    result=invoke(page)
    assert 'signing in' in result['error']

def test_content_script_requires_own_extension_sender(page):
    assert page.evaluate('''() => {let replied=false;const value=window.lyricListener({type:'lyric-status'},{id:'unrelated-extension'},()=>{replied=true});return value===undefined&&!replied}''')

@pytest.mark.parametrize('markup',[
    '<button aria-label="Send" onclick="send()">↑</button>',
    '<button class="send-button submit" onclick="send()"><svg></svg></button>',
    '<button data-testid="composer-send-button" onclick="send()">↑</button>',
    '<button onclick="send()"><mat-icon>arrow_upward</mat-icon></button>',
])
def test_current_composer_send_variants(page,markup):
    page.evaluate('''markup=>{
      const original=document.querySelector('button[aria-label="Send message"]');
      const area=document.createElement('input-area-v2');
      original.before(area);area.append(document.querySelector('rich-textarea'));
      area.insertAdjacentHTML('beforeend','<button aria-label="Use microphone" onclick="window.wrongClick=true">mic</button>'+markup);
      original.remove();
    }''',markup)
    result=invoke(page,'New composer Send variant')
    assert not result.get('error'),result
    assert json.loads(result['text'])['message'].startswith('Full response 1')
    assert page.evaluate('window.wrongClick!==true')

def test_send_waits_for_enabled_replacement_button(page):
    page.evaluate('''()=>{
      const button=document.querySelector('button[aria-label="Send message"]');
      button.disabled=true;
      document.querySelector('[contenteditable]').addEventListener('input',()=>setTimeout(()=>{
        const replacement=document.createElement('button');replacement.className='send-button';
        replacement.innerHTML='<mat-icon>arrow_upward</mat-icon>';replacement.onclick=send;
        button.replaceWith(replacement);
      },200),{once:true});
    }''')
    result=invoke(page)
    assert not result.get('error'),result

def test_pointer_submission_does_not_click_twice(page):
    page.evaluate('''()=>{
      const button=document.querySelector('button[aria-label="Send message"]');
      window.clicks=0;button.onclick=()=>{window.clicks++;send()};
      button.onpointerup=()=>{window.clicks++;send()};
    }''')
    result=invoke(page)
    assert not result.get('error'),result
    assert page.evaluate('window.clicks')==1

def test_missing_send_acknowledgement_reports_failure_without_retry(page):
    page.evaluate('''()=>{
      window.clicks=0;
      document.querySelector('button[aria-label="Send message"]').onclick=()=>window.clicks++;
    }''')
    result=invoke(page)
    assert 'did not confirm' in result['error']
    assert page.evaluate('window.clicks')==1
    assert page.locator('[contenteditable]').inner_text()=='Hello'

def test_send_feedback_is_never_selected(page):
    page.evaluate('''()=>{
      const button=document.createElement('button');
      button.setAttribute('aria-label','Send feedback');button.onclick=()=>{window.wrongClick=true};
      document.querySelector('button[aria-label="Send message"]').before(button);
    }''')
    result=invoke(page)
    assert not result.get('error'),result
    assert page.evaluate('window.wrongClick!==true')


def test_send_label_on_gem_icon_button_wrapper(page):
    page.evaluate('''()=>{
      const original=document.querySelector('button[aria-label="Send message"]');
      const area=document.createElement('input-area-v2');original.before(area);
      area.append(document.querySelector('rich-textarea'));
      area.insertAdjacentHTML('beforeend','<gem-icon-button role="button" aria-label="Send message" class="send-button"><button onclick="send()"><svg></svg></button></gem-icon-button>');
      original.remove();
    }''')
    result=invoke(page)
    assert not result.get('error'),result
    assert json.loads(result['text'])['kind']=='reply'


def install_quill_fixture(page):
    markup=(Path(__file__).parent/'fixtures'/'gemini-send-button.html').read_text(encoding='utf-8')
    page.evaluate('''markup=>{
      const original=document.querySelector('button[aria-label="Send message"]');
      const area=document.createElement('input-area-v2');original.before(area);
      area.append(document.querySelector('rich-textarea'));
      area.insertAdjacentHTML('beforeend',markup);original.remove();
      const input=document.querySelector('[role="textbox"]');
      input.className='ql-editor textarea new-input-ui rich-query-formatting-enabled';
      input.innerHTML='<p><br></p>';
      const clipboard=document.createElement('div');clipboard.className='ql-clipboard';
      clipboard.contentEditable='true';clipboard.style.cssText='position:absolute;left:-10000px';
      input.parentElement.append(clipboard);
      area.querySelector('[data-test-id="send-button-container"] button').onclick=()=>{
        window.sendClicks=(window.sendClicks||0)+1;send();
      };
    }''',markup)


def test_exact_gemini_quill_paragraphs_send_multiline_prompt(page):
    install_quill_fixture(page)
    prompt='You are Lyric.\nReturn JSON only.\n\nUSER REQUEST:\n"hello"'.replace('\\n','\n')
    result=invoke(page,prompt)
    assert not result.get('error'),(result,page.evaluate("""()=>({
      text:document.querySelector('[role="textbox"]').innerText,
      blocks:[...document.querySelector('[role="textbox"]').children].map(p=>({tag:p.tagName,text:p.textContent,inner:p.innerText,html:p.innerHTML}))
    })"""))
    assert page.evaluate('window.sendClicks')==1
    assert json.loads(result['text'])['kind']=='reply'


def test_quill_preserves_blank_lines_and_indented_text(page):
    install_quill_fixture(page)
    page.evaluate('''()=>{
      const button=document.querySelector('[data-test-id="send-button-container"] button');
      const original=button.onclick;
      button.onclick=()=>{
        window.sentLines=[...document.querySelector('.ql-editor').children].map(p=>p.textContent.replaceAll('\\u00a0',' '));
        original();
      };
    }''')
    result=invoke(page,'First line\n\n  Indented text ✓\nLast line'.replace('\\n','\n'))
    assert not result.get('error'),result
    assert page.evaluate('window.sentLines')==['First line','','  Indented text ✓','Last line']


def test_quill_incomplete_input_still_prevents_send(page):
    install_quill_fixture(page)
    page.evaluate('''()=>{
      const input=document.querySelector('.ql-editor');
      input.addEventListener('input',()=>setTimeout(()=>{
        const last=input.lastElementChild;
        last.textContent=last.textContent.slice(0,-1);
      },0),{once:true});
    }''')
    result=invoke(page,'First line\nSecond line'.replace('\\n','\n'))
    assert 'complete message' in result.get('error','')
    assert page.evaluate('window.sendClicks||0')==0


def test_quill_clipboard_is_never_used_as_the_composer(page):
    install_quill_fixture(page)
    page.locator('.ql-editor').evaluate("el=>el.style.display='none'")
    result=invoke(page,'Do not put this in the clipboard')
    assert 'signing in' in result.get('error','')
    assert page.locator('.ql-clipboard').inner_text()==''


def test_full_lyric_protocol_sends_through_actual_quill_markup(page,monkeypatch,tmp_path):
    import lyric
    from unittest.mock import Mock
    monkeypatch.setattr(lyric,'computer_context',lambda path:{'os':'Windows 11','working_directory':'C:\\Example Folder','powershell_version':'5.1'})
    ai=lyric.BrowserAI({},tmp_path)
    ai.browser=Mock()
    ai.browser.exchange.return_value=json.dumps({'kind':'reply','message':'Hello','description':'','shell':'powershell','command':''})
    ai.ask('hi')
    prompt=ai.browser.exchange.call_args.args[0]
    assert len(prompt)>4000
    install_quill_fixture(page)
    result=invoke(page,prompt)
    assert not result.get('error'),result
    assert page.evaluate('window.sendClicks')==1
    assert json.loads(result['text'])['kind']=='reply'
