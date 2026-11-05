(() => {
'use strict';
const INPUT_XPATH='/html/body/chat-app-orchestrator/chat-app/main/side-navigation-v2/bard-sidenav-container/bard-sidenav-content/div[2]/div/div/chat-window/div[2]/input-container/div/input-area-v2/div/fieldset/div/div[2]/div/div/div/rich-textarea/div[1]/p';
const SEND_XPATH='/html/body/chat-app-orchestrator/chat-app/main/side-navigation-v2/bard-sidenav-container/bard-sidenav-content/div[2]/div/div/chat-window/div[2]/input-container/div/input-area-v2/div/fieldset/div/div[3]/div/div[2]/gem-icon-button/button';
const STOP='button[data-test-id="stop-button"],button[aria-label*="Stop response"],button[aria-label*="Stop generating"],input-area-v2 button[aria-label="Stop"]';
const visible=el=>!!el && el.getClientRects().length>0 && getComputedStyle(el).visibility!=='hidden';
const xpath=value=>document.evaluate(value,document,null,XPathResult.FIRST_ORDERED_NODE_TYPE,null).singleNodeValue;
const app=()=>location.origin==='https://gemini.google.com' && (location.pathname==='/app'||location.pathname.startsWith('/app/'));
let requestId=null;
// A minimized tab's chained timers can run only once a minute. The extension
// worker owns the clock, so sending and completion checks don't inherit that delay.
async function sleep(ms){
  const result=await chrome.runtime.sendMessage({type:'lyric-wait',id:requestId,ms});
  if(result?.ok!==true)throw new Error(result?.error||'The Lyric connection changed. Refresh Gemini and reconnect the extension.');
}
const editor=()=>[...document.querySelectorAll('rich-textarea .ql-editor[contenteditable="true"][role="textbox"]'),
  ...document.querySelectorAll('[role="textbox"][contenteditable="true"],rich-textarea [contenteditable="true"]:not(.ql-clipboard)'),
  xpath(INPUT_XPATH)].find(el=>visible(el)&&el.isContentEditable&&!el.classList.contains('ql-clipboard'));
function draftText(input){
  // Quill stores one paragraph per logical line. innerText inserts an extra
  // visual newline between <p> blocks, which previously rejected a full prompt.
  const blocks=[...input.children];
  if(blocks.length&&blocks.every(node=>node.tagName==='P')){
    return blocks.map(block=>{
      const clone=block.cloneNode(true);
      clone.querySelectorAll('.ql-cursor').forEach(node=>node.remove());
      const read=node=>node.nodeType===Node.TEXT_NODE?node.nodeValue:
        node.nodeName==='BR'?'\n':[...node.childNodes].map(read).join('');
      return read(clone).replace(/\r\n/g,'\n').replace(/\n$/,'');
    }).join('\n');
  }
  return input.innerText.replace(/\r\n/g,'\n');
}
const comparableText=text=>text.replace(/\r\n/g,'\n').replace(/\u00a0/g,' ').trim();
const stopping=()=>[...document.querySelectorAll(STOP)].some(visible);
const nodes=new WeakMap();let counter=0,busy=false;
function snapshot(){
  return [...document.querySelectorAll('model-response')].map(node=>{
    const host=node.querySelector('message-content');if(!visible(host))return null;
    const content=host.querySelector('[id^="model-response-message-content"]')||host.querySelector('.markdown')||host;
    if(!nodes.has(node))nodes.set(node,++counter);
    const aria=node.querySelector('[aria-busy="true"]')||content.closest('[aria-busy]')||node.querySelector('[aria-busy]');
    let blocks=[...content.querySelectorAll('pre code')];if(!blocks.length)blocks=[...content.querySelectorAll('pre')];
    return {key:content.id||'node-'+nodes.get(node),text:content.innerText||'',code:blocks.length===1?blocks[0].innerText:null,
      busy:aria?.getAttribute('aria-busy')??null,
      finished:[...node.querySelectorAll('button')].some(b=>/copy|good response|bad response/i.test(b.getAttribute('aria-label')||''))};
  }).filter(Boolean);
}
function responseText(row){
  const text=row.text.trim();
  if(row.code && text.includes(row.code)){
    const wrapper=text.replace(row.code,'').trim();
    if(/^(?:(?:json|copy code|copy|code|content_copy)\s*)*$/i.test(wrapper))return row.code.trim();
  }
  return text;
}
function composerFor(input){
  const named=input.closest('input-area-v2,input-area,input-container,fieldset,form');
  let node=input.parentElement;
  for(let i=0;node&&i<12&&node!==document.body&&node.tagName!=='CHAT-WINDOW';i++,node=node.parentElement){
    const buttons=[...node.querySelectorAll('button,[role="button"]')];
    if(buttons.some(button=>sendScore(button,node,null)>=80))return node;
  }
  return named||input.parentElement;
}
function sendScore(button,root,legacy){
  if(!visible(button)||button.disabled||button.matches(':disabled')||button.getAttribute('aria-disabled')==='true'||button.closest('[inert]'))return -1;
  const inComposer=root?.contains(button);
  const parent=button.parentElement;
  const referenced=(button.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean)
    .map(id=>document.getElementById(id)?.textContent||'').join(' ').trim();
  const label=[button.getAttribute('aria-label'),referenced,button.getAttribute('title'),button.getAttribute('mattooltip'),
    parent?.getAttribute('aria-label'),parent?.getAttribute('title'),button.textContent?.trim()].find(value=>value?.trim())?.trim()||'';
  const attrs=[button.getAttribute('data-test-id'),button.getAttribute('data-testid'),button.getAttribute('name'),button.id,String(button.className),
    parent?.getAttribute('data-test-id'),parent?.getAttribute('data-testid'),parent?.className].filter(Boolean).join(' ');
  const icons=[...button.querySelectorAll('mat-icon,svg,span.google-symbols,span.material-symbols-rounded,span.material-icons,[data-mat-icon-name],[data-icon-name]')]
    .map(el=>[el.getAttribute('data-mat-icon-name'),el.getAttribute('data-icon-name'),el.getAttribute('fonticon'),el.getAttribute('svgicon'),el.getAttribute('aria-label'),el.textContent].filter(Boolean).join(' ')).join(' ');
  if(/feedback|share|report|microphone|\bmic\b|voice|dictat|upload|attach|expand|full.?screen|settings|\bmenu\b|\bstop\b|cancel/i.test(label+' '+icons))return -1;
  let score=0;
  if(/^(?:send(?: message| prompt| question)?|submit(?: message| prompt| question| query)?)(?:\s*\([^)]*\))?$/i.test(label))score=130;
  if(/(?:^|[\s_-])(?:send|submit)(?:[\s_-]|$)/i.test(attrs))score=Math.max(score,120);
  if(inComposer && /\b(?:send|arrow_upward|arrow_upward_alt|north)\b/i.test(icons))score=Math.max(score,100);
  if(inComposer && button.getAttribute('type')==='submit')score=Math.max(score,80);
  if(button===legacy && inComposer && !/model|gemini|auto|thinking|\bpro\b/i.test(label))score=Math.max(score,60);
  if(!score)return -1;
  return score+(inComposer?20:0);
}
function sendButton(input){
  const root=composerFor(input),legacy=xpath(SEND_XPATH);
  const candidates=new Set([...(root?.querySelectorAll('button,[role="button"]')||[]),
    ...document.querySelectorAll('[data-test-id="send-button-container"] gem-icon-button button'),
    ...document.querySelectorAll('button[aria-label],button[data-test-id],button[data-testid],button.send-button,button.submit'),legacy]);
  return [...new Set([...candidates].filter(Boolean).map(node=>node.matches('button')?node:node.querySelector('button')||node))]
    .map(button=>({button,score:sendScore(button,root,legacy)}))
    .filter(item=>item.score>=0).sort((a,b)=>b.score-a.score)[0]?.button;
}
const userTurns=()=>document.querySelectorAll('user-query,user-message,[data-test-id="user-query"]').length;
function submitted(input,old,previousUsers){
  return stopping()||userTurns()>previousUsers||snapshot().some(row=>!old.has(row.key));
}
async function activateSend(button,input,old,previousUsers){
  button.scrollIntoView({block:'nearest',inline:'nearest'});
  button.focus({preventScroll:true});
  await sleep(0); // Allow the editor's blur/input bindings to commit before activation.
  const rect=button.getBoundingClientRect(),x=rect.left+rect.width/2,y=rect.top+rect.height/2;
  const hit=document.elementFromPoint(x,y);
  if(hit&&!button.contains(hit)&&hit!==button)throw new Error('A menu or overlay is covering Send. Close it in Gemini, then try again.');
  for(const type of ['pointerdown','mousedown','pointerup','mouseup']){
    if(!button.isConnected||button.disabled||submitted(input,old,previousUsers)||!draftText(input).trim())return;
    const EventType=type.startsWith('pointer')&&window.PointerEvent?PointerEvent:MouseEvent;
    button.dispatchEvent(new EventType(type,{bubbles:true,cancelable:true,composed:true,view:window,
      clientX:x,clientY:y,button:0,buttons:type.endsWith('down')?1:0,pointerId:1,pointerType:'mouse',isPrimary:true}));
    await sleep(0);
  }
  // Exactly one click. If a pointer handler already submitted, do not click again.
  if(button.isConnected&&!button.disabled&&!submitted(input,old,previousUsers)&&draftText(input).trim())button.click();
}
async function waitForSubmission(input,old,previousUsers){
  const deadline=Date.now()+12000;
  while(Date.now()<deadline){
    if(!app())throw new Error('Gemini left its chat page before confirming the send. Check the tab.');
    if(submitted(input,old,previousUsers))return;
    await sleep(100);
  }
  throw new Error('Gemini did not confirm that Send worked. Your draft was not clicked again. Check the tab and clear the stuck draft before retrying.');
}
async function exchange(text){
  if(!app()||!editor())throw new Error('Finish signing in on Gemini’s /app page, then reconnect the extension.');
  if(stopping())throw new Error('Gemini is still answering. Wait for it to finish.');
  const input=editor();if(draftText(input).trim())throw new Error('The Gemini tab has an unsent draft. Clear it before using Lyric.');
  const old=new Set(snapshot().map(row=>row.key));
  const previousUsers=userTurns();
  input.focus();
  const selection=getSelection(),range=document.createRange();range.selectNodeContents(input);selection.removeAllRanges();selection.addRange(range);
  // Plain-text insertion with a normal input event; never insert page-supplied HTML.
  const inserted=document.execCommand('insertText',false,text);
  if(!inserted){input.textContent=text;input.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:text}));}
  await sleep(0); // Let the rich-text editor commit its input/paragraph changes.
  let complete=false,matches=0;
  for(let i=0;i<20;i++){
    matches=comparableText(draftText(input))===comparableText(text)?matches+1:0;
    if(matches>=2){complete=true;break;}
    await sleep(50);
  }
  if(!complete)throw new Error('Gemini did not accept the complete message. Check and clear the draft; Lyric did not resend it.');
  let send;
  for(let i=0;i<40;i++){
    send=sendButton(input);
    if(send)break;await sleep(250);
  }
  if(!send)throw new Error('The Send button is unavailable. Check the Gemini tab and clear any unsent draft.');
  await activateSend(send,input,old,previousUsers);
  await waitForSubmission(input,old,previousUsers);
  const deadline=Date.now()+240000;let last='',stable=Date.now();
  while(Date.now()<deadline){
    if(!app())throw new Error('The Gemini tab left its chat page. Sign in normally and reconnect.');
    const row=snapshot().filter(row=>!old.has(row.key)).at(-1);
    const mark=row?row.key+'\n'+row.text:'';
    if(mark!==last){last=mark;stable=Date.now();}
    if(row&&row.text.trim()&&row.busy!=='true'&&!stopping()&&(row.busy==='false'||row.finished)&&Date.now()-stable>=3000)return responseText(row);
    await sleep(250);
  }
  throw new Error('Gemini did not finish in time. Check its tab for limits or errors. Lyric did not resend the message.');
}
async function reset(){
  if(!app())throw new Error('Open Gemini’s chat page first.');
  const link=[...document.querySelectorAll('a[href="/app"],button[aria-label="New chat"],a[aria-label="New chat"]')].find(visible);
  if(!link)throw new Error('Click New chat in Gemini, then try again.');
  link.click();
  for(let i=0;i<40;i++){if(location.pathname==='/app'&&editor()&&snapshot().length===0)return 'New conversation ready.';await sleep(250);}
  throw new Error('Gemini did not open a fresh conversation. Click New chat manually and try again.');
}
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(sender.id!==chrome.runtime.id)return;
  if(message.type==='lyric-status'){reply({ready:app()&&!!editor(),url:location.href});return;}
  if(message.type!=='lyric-command')return;
  if(busy){reply({error:'Gemini is already processing another Lyric request.'});return;}
  const command=message.command;
  if(!command||!['request','reset'].includes(command.type)||typeof command.id!=='string')return;
  busy=true;requestId=command.id;
  (command.type==='reset'?reset():exchange(String(command.text||'')))
    .then(text=>reply({text})).catch(error=>reply({error:error.message})).finally(()=>{busy=false;requestId=null;});
  return true;
});
chrome.runtime.sendMessage({type:'content-ready'}).catch(()=>{});
})();
