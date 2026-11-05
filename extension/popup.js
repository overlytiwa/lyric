const label=document.getElementById('status');
async function status(){
  try{const value=await chrome.runtime.sendMessage({type:'popup-status'});label.textContent=value.error||(value.ready?'Gemini tab ready. Keep Lyric running.':value.connected?'Connected to Chrome. Open Gemini’s /app chat page.':'Not connected yet.');}
  catch(error){label.textContent=error.message;}
}
document.getElementById('connect').onclick=async()=>{
  try{
    const [active]=await chrome.tabs.query({active:true,currentWindow:true});
    let tab=active;
    if(!tab?.url?.startsWith('https://gemini.google.com/app')){
      const tabs=await chrome.tabs.query({url:'https://gemini.google.com/*'});tab=tabs.find(t=>t.url?.startsWith('https://gemini.google.com/app'));
    }
    if(!tab)throw new Error('Open https://gemini.google.com/app and sign in first.');
    const result=await chrome.runtime.sendMessage({type:'popup-connect',tabId:tab.id});
    if(result.error)throw new Error(result.error);await status();
  }catch(error){label.textContent=error.message;}
};
document.getElementById('disconnect').onclick=async()=>{const result=await chrome.runtime.sendMessage({type:'popup-disconnect'});label.textContent=result.error||'Disconnected.';};
status();
