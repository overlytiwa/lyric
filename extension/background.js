const HOST = 'com.lyric.gemini_bridge';
let port = null;
let selectedTab = null;
let enabled = false;
let lastError = '';
let inFlight = false;
let requestId = null;
let protection = null;
const valid = url => { try { const u=new URL(url); return u.origin==='https://gemini.google.com' && (u.pathname==='/app'||u.pathname.startsWith('/app/')); } catch { return false; } };
const init = Promise.all([chrome.storage.local.get(['enabled']),chrome.storage.session.get(['protection'])]).then(async ([s,session]) => {
  enabled=s.enabled===true;protection=session.protection||null;
  if(enabled&&protection)selectedTab=protection.id;
  else await releaseProtection();
});

async function releaseProtection(){
  const previous=protection;protection=null;
  await chrome.storage.session.remove('protection');
  if(previous)try{await chrome.tabs.update(previous.id,{autoDiscardable:previous.autoDiscardable});}catch{/* The tab may have closed. */}
}
async function protectTab(id){
  const tab=await chrome.tabs.get(id);
  if(!valid(tab.url))throw new Error('Open Gemini and reconnect the Lyric extension.');
  if(protection?.id!==id){
    await releaseProtection();
    protection={id,autoDiscardable:tab.autoDiscardable!==false};
    await chrome.storage.session.set({protection});
  }
  // Only the user's connected Gemini tab is protected; never activate a tab or window.
  await chrome.tabs.update(id,{autoDiscardable:false});
  return tab;
}

function nativeSend(value) { try {port?.postMessage(value);} catch {lastError='Local connection closed. Click Connect again.';} }
async function status() {
  if (selectedTab===null) return {type:'status',ready:false,url:''};
  try {
    const tab=await chrome.tabs.get(selectedTab);
    if (!valid(tab.url)||tab.frozen||tab.discarded) return {type:'status',ready:false,url:''};
    const result=await chrome.tabs.sendMessage(selectedTab,{type:'lyric-status'});
    return {type:'status',ready:result?.ready===true,url:tab.url};
  } catch {return {type:'status',ready:false,url:''};}
}
async function publishStatus() {nativeSend(await status());}
function ensureNative() {
  if (port) return;
  try {
    port=chrome.runtime.connectNative(HOST);
    port.onMessage.addListener(async command => {
      if (command.type==='host-connected') {lastError=''; await publishStatus(); return;}
      if (command.type==='host-offline') {lastError='Start Lyric in the terminal. Chrome is connected.'; return;}
      if (command.type==='host-error') {lastError=command.error; return;}
      if (!['request','reset'].includes(command.type) || typeof command.id!=='string') return;
      if (inFlight) {nativeSend({type:'result',id:command.id,error:'Gemini is already processing another request.'});return;}
      inFlight=true;requestId=command.id;
      try {
        if (selectedTab===null || !valid((await chrome.tabs.get(selectedTab)).url)) throw new Error('Open Gemini and reconnect the Lyric extension.');
        const tab=await protectTab(selectedTab);
        if(tab.frozen||tab.discarded)throw new Error('Chrome has paused the Gemini tab. Open it once, then retry.');
        const result=await chrome.tabs.sendMessage(selectedTab,{type:'lyric-command',command});
        nativeSend({type:'result',id:command.id,text:result?.text||'',error:result?.error||''});
      } catch(error) {nativeSend({type:'result',id:command.id,error:error.message||'The Gemini tab could not complete the request.'});}
      finally {inFlight=false;requestId=null; await publishStatus();}
    });
    port.onDisconnect.addListener(()=>{lastError=chrome.runtime.lastError?.message||'Local connection closed. Click Connect again.';port=null;});
    publishStatus();
  } catch(error) {lastError=error.message;port=null;}
}
async function connect(tabId) {
  if (inFlight) throw new Error('Wait for the current request to finish before changing tabs.');
  const tab=await chrome.tabs.get(tabId);
  if (!valid(tab.url)) throw new Error('Open Gemini’s /app chat page, then click Connect this tab.');
  await protectTab(tab.id);
  selectedTab=tab.id;enabled=true;lastError='';
  await chrome.storage.local.set({enabled:true});
  ensureNative();await publishStatus();
  return {ok:true,status:await status()};
}
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if (sender.id!==chrome.runtime.id) return;
  (async()=>{
    await init;
    if(message.type==='lyric-wait'){
      if(!sender.tab||sender.frameId!==0||sender.tab.id!==selectedTab||!valid(sender.tab.url)||!inFlight||message.id!==requestId)
        return {error:'The Lyric request is no longer active. Check its terminal before retrying.'};
      if(!Number.isInteger(message.ms)||message.ms<0||message.ms>1000)return {error:'Invalid wait interval.'};
      await new Promise(resolve=>setTimeout(resolve,message.ms));
      return inFlight&&message.id===requestId?{ok:true}:{error:'The Lyric request is no longer active.'};
    }
    if (message.type==='content-ready' && sender.tab && valid(sender.tab.url)) {
      if (enabled) {if(selectedTab===null){await protectTab(sender.tab.id);selectedTab=sender.tab.id;}ensureNative();}
      if (sender.tab.id===selectedTab) await publishStatus();
      return {ok:true};
    }
    // Only extension pages (the popup), never a website content script, may pin a tab.
    if (sender.tab) return {error:'Unsupported page message.'};
    if (message.type==='popup-connect') return await connect(message.tabId);
    if (message.type==='popup-status') return {connected:!!port,selectedTab,error:lastError,ready:(await status()).ready};
    if (message.type==='popup-disconnect') {
      if(inFlight) throw new Error('Wait for the current request to finish.');
      nativeSend({type:'status',ready:false,url:''});
      port?.disconnect();port=null;selectedTab=null;enabled=false;
      await chrome.storage.local.set({enabled:false});await releaseProtection();return {ok:true};
    }
    return {error:'Unknown request.'};
  })().then(reply).catch(error=>reply({error:error.message}));
  return true;
});
chrome.tabs.onRemoved.addListener(async id=>{await init;if(id===selectedTab){selectedTab=null;await releaseProtection();await publishStatus();}});
chrome.tabs.onUpdated.addListener(async(id,change)=>{
  await init;
  if(id!==selectedTab)return;
  if(change.url&&!valid(change.url)){selectedTab=null;await releaseProtection();}
  if(change.status==='complete'||change.url||change.frozen!==undefined||change.discarded!==undefined)await publishStatus();
});
chrome.runtime.onStartup.addListener(async()=>{
  await init;if(!enabled)return;
  const tabs=await chrome.tabs.query({url:'https://gemini.google.com/*'});
  const tab=tabs.find(t=>valid(t.url));
  if(tab){await protectTab(tab.id);selectedTab=tab.id;ensureNative();}
});

