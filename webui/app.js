(() => {
  const root = document.documentElement;
  const systemTheme = matchMedia('(prefers-color-scheme: dark)');
  let preference = null;
  try { const saved = localStorage.getItem('helper-theme'); if (saved === 'light' || saved === 'dark') preference = saved; } catch {}
  function applyTheme(theme) {
    root.dataset.theme = theme;
    const button = document.querySelector('#theme-toggle');
    if (!button) return;
    const target = theme === 'dark' ? '亮色' : '暗色';
    button.querySelector('span').textContent = target;
    button.setAttribute('aria-label', '切换到' + target); button.title = '切换到' + target;
  }
  const preferredTheme = () => preference || (systemTheme.matches ? 'dark' : 'light');
  applyTheme(preferredTheme());
  systemTheme.addEventListener('change', () => { if (!preference) applyTheme(preferredTheme()); });
  window.addEventListener('storage', event => {
    if (event.key !== 'helper-theme' && event.key !== null) return;
    preference = event.newValue === 'light' || event.newValue === 'dark' ? event.newValue : null;
    applyTheme(preferredTheme());
  });
  document.addEventListener('DOMContentLoaded', () => {
    applyTheme(preferredTheme());
    document.querySelector('#theme-toggle').onclick = () => {
      preference = root.dataset.theme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem('helper-theme', preference); } catch {}
      applyTheme(preference);
    };
const $ = (q, root = document) => root.querySelector(q);
const token = $('meta[name="helper-token"]').content;
let state = null, config = null, logCursor = 0, logs = [], paused = false, busy = false;
let selectedPlugin = '', toastTimer, pollBusy = false, serviceAvailable = true;
let robot = sessionStorage.getItem('helper-robot') || 'default', robotRows = [], lastRobotFetch = 0;
let pluginSignature = '', accountSignature = '', robotOptionsSignature = '';
let messages=[], messageCursor=0, messageStream='', messagePaused=false, messageError='', messageGeneration=0, messageSignature='';
let loggedAccounts = [], selectedAccounts = new Set(), accountsError = '', accountsLoading = true, accountsFetching = false, lastAccountFetch = 0, batchStarting = false;
const accountKey = row => row.pid + ':' + row.wxid;
const activeStates = ['starting','running','stopping'];
const profileLabel = row => loggedAccounts.find(account=>account.wxid===row?.account)?.name || row?.account || (row?.id==='default'?'默认配置':row?.name) || '当前账号';
const pages = {overview:'运行概览', plugins:'插件管理', groups:'群聊与会话', models:'模型与语音', logs:'运行日志'};
const pluginNames = {picture_idiom:['看图猜成语'], idiom_chain:['成语接龙'], chat_summary:['聊天总结'], mention_chat:['艾特对话'], voice_reply:['语音回复'], group_notice:['进退群提醒'], member_identity:['群内昵称绑定'], personality:['切换人格'], friend_auto_accept:['自动通过好友'], chat_records:['聊天记录管理'], id_responder:['会话 ID 查询'], echo:['文字回声']};
const escape = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, body, identifier = robot) {
  const scope = (path === 'robots' || path === 'accounts' || path.startsWith('accounts/')) ? '' : (path.includes('?') ? '&' : '?') + 'robot=' + encodeURIComponent(identifier);
  const response = await fetch('/api/' + path + scope, {method: body === undefined ? 'GET' : 'POST', cache:'no-store',
    headers:{'X-Helper-Token':token, ...(body === undefined ? {} : {'Content-Type':'application/json'})},
    ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  const data = await response.json();
  if (!response.ok) { const error=new Error(data.error||'操作失败');error.status=response.status;throw error; }
  return data;
}
function toast(message, error = false) {
  const dialogError = $('dialog[open] .dialog-error');
  if (error && dialogError) { dialogError.textContent = message; dialogError.classList.remove('hidden'); return; }
  const node = $('#toast'); node.textContent = message; node.className = 'toast' + (error ? ' error' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => node.classList.add('hidden'), error ? 7000 : 3500);
}
function openDialog(selector) {
  const dialog = $(selector); $('.dialog-error', dialog).classList.add('hidden');
  $('#toast').classList.add('hidden'); dialog.showModal();
}
async function action(callback) {
  if (!serviceAvailable) { toast('管理服务尚未恢复连接，请稍候或刷新页面', true); return; }
  if (busy) return; busy = true; renderState();
  document.querySelectorAll('.dialog-error').forEach(node=>node.classList.add('hidden'));
  try { await callback(); await poll(); } catch (error) { toast(error.message, true); }
  finally { busy = false; renderState(); }
}
function navigate() {
  const name = location.hash.slice(1) in pages ? location.hash.slice(1) : 'overview';
  document.querySelectorAll('.page').forEach(node => node.classList.toggle('hidden', node.id !== 'page-' + name));
  document.querySelectorAll('[data-page]').forEach(node => node.classList.toggle('active', node.dataset.page === name));
  document.querySelectorAll('[data-page]').forEach(node => node.dataset.page === name ? node.setAttribute('aria-current','page') : node.removeAttribute('aria-current'));
  $('#page-label').textContent = pages[name]; document.title = pages[name] + ' · helper';
  if (document.body.classList.contains('nav-open')) { setNavigation(false); $('#workspace').focus({preventScroll:true}); }
  window.scrollTo(0, 0);
}
function renderRobotOptions() {
  const names={stopped:'已停止',starting:'启动中',running:'等待连接',stopping:'停止中',error:'异常'};
  const options=robotRows.map(row=>{
    const current=row.id===robot&&state?state:row;
    return {value:row.id,label:profileLabel(row)+' · '+(!serviceAvailable?'状态待更新':current.connected?'已连接':names[current.state]||'已停止')};
  });
  for(const account of loggedAccounts) {
    if(robotRows.some(row=>row.account===account.wxid))continue;
    options.push({value:'account:'+accountKey(account),label:(account.name||account.wxid)+' · 未启动',disabled:!account.selectable});
  }
  const signature=JSON.stringify(options);
  if(signature!==robotOptionsSignature) {
    robotOptionsSignature=signature;
    $('#robot-select').innerHTML=options.map(row=>`<option value="${escape(row.value)}" ${row.disabled?'disabled':''}>${escape(row.label)}</option>`).join('');
  }
  $('#robot-select').value=robot;
}
function renderState() {
  const unavailable=!serviceAvailable;
  $('#robot-select').disabled=busy||unavailable;
  $('#discover-chats').disabled=busy||unavailable||!state||!activeStates.includes(state.state);
  renderRobotOptions();renderAccounts();renderMessages();
  const running=!!state&&activeStates.includes(state.state);
  document.querySelectorAll('form .primary').forEach(node=>node.disabled=unavailable||!state||running||busy);
  document.querySelectorAll('.save-hint').forEach(node=>node.textContent=unavailable?'连接中断':!state?'正在读取':running?'停止后可保存':'启动后生效');
  if(state)$('#nav-plugin-count').textContent=state.plugins.length;
  renderPlugins();
}
function renderPlugins() {
  if (!state) { pluginSignature=''; $('#plugin-list').innerHTML='<tr><td colspan="4" class="table-empty">正在读取插件列表…</td></tr>'; return; }
  const query = $('#plugin-search').value.trim().toLowerCase();
  const cards = state.plugins.filter(p => (p.name + (pluginNames[p.name]?.[0]||'') + p.description).toLowerCase().includes(query));
  const signature = JSON.stringify([robot,query,busy,serviceAvailable,cards]);
  if (signature === pluginSignature) return;
  pluginSignature = signature;
  const focused = document.activeElement?.closest('[data-plugin]');
  const focusKey = focused && [focused.dataset.plugin,focused.dataset.action];
  $('#plugin-list').innerHTML = cards.map(p => {
    const name = pluginNames[p.name]?.[0] || p.name;
    return `<tr class="plugin-row"><td><span class="plugin-name">${escape(name)}</span><span class="plugin-code">${escape(p.name)}</span></td><td class="plugin-description">${escape(p.description)}${p.error?`<p class="plugin-error">${escape(p.error)}</p>`:''}</td><td><span class="inline-state ${!serviceAvailable?'waiting':p.active?'active':p.error?'failed':''}">${!serviceAvailable?'待更新':p.active?'运行中':p.enabled?'已启用':'已停用'}</span></td><td><div class="plugin-actions"><button class="secondary small" data-plugin="${escape(p.name)}" data-action="${p.enabled?'disable':'enable'}" ${busy||!serviceAvailable?'disabled':''}>${p.enabled?'停用':'启用'}</button><button class="text-button" data-plugin="${escape(p.name)}" data-action="reload" ${!p.active||busy||!serviceAvailable?'disabled':''}>重载</button><button class="text-button" data-plugin="${escape(p.name)}" data-action="configure" ${busy||!serviceAvailable?'disabled':''}>配置</button></div></td></tr>`;
  }).join('') || '<tr><td colspan="4" class="table-empty">没有匹配的插件。</td></tr>';
  if (focusKey) {
    const candidates = [...$('#plugin-list').querySelectorAll('[data-plugin]')];
    const target = candidates.find(n=>n.dataset.plugin===focusKey[0]&&n.dataset.action===focusKey[1]&&!n.disabled) || candidates.find(n=>n.dataset.plugin===focusKey[0]&&!n.disabled);
    target?.focus({preventScroll:true});
  }
}
function renderAccounts() {
  const rows=loggedAccounts.map(row=>{
    const profile=robotRows.find(profile=>profile.id===row.robot_id);
    return row.robot_id===robot&&state ? {...row,state:state.state,connected:state.connected,error:state.error||state.connection_error} : profile ? {...row,state:profile.state,connected:profile.connected} : row;
  });
  const chosen=rows.filter(row=>selectedAccounts.has(accountKey(row))&&row.selectable&&!activeStates.includes(row.state));
  $('#account-total').textContent=rows.length;
  $('#account-selection-count').textContent='已选 '+chosen.length;
  $('#refresh-accounts').disabled=busy||!serviceAvailable||accountsFetching;
  $('#start-selected').disabled=busy||!serviceAvailable||accountsLoading||!chosen.length;
  $('#start-selected').textContent=batchStarting?'正在启动…':'启动所选';
  $('#account-error').textContent=accountsError;$('#account-error').classList.toggle('hidden',!accountsError);
  const signature=JSON.stringify([rows,[...selectedAccounts],busy,serviceAvailable,accountsLoading]);
  if(signature===accountSignature)return;
  accountSignature=signature;
  const focused=document.activeElement;
  const focusKey=focused?.dataset.selectAccount||focused?.dataset.accountAction;
  const focusSelector=focused?.dataset.selectAccount?'[data-select-account]':'[data-account-action]';
  const statuses={stopped:'未启动',starting:'启动中',running:'等待连接',stopping:'正在停止',error:'异常'};
  $('#account-list').innerHTML=rows.map(row=>{
    const key=accountKey(row),running=activeStates.includes(row.state),disabled=busy||!serviceAvailable||accountsLoading||!row.selectable||running;
    const label=row.name||row.wxid;
    const stopping=row.state==='stopping',canStop=running&&!!row.robot_id;
    const buttonDisabled=busy||!serviceAvailable||stopping||(canStop?false:accountsLoading||!row.selectable);
    const buttonLabel=stopping?'停止中…':canStop?'停止':'启动';
    return `<tr class="${row.robot_id===robot?'selected':''}"><td class="account-choice"><label><input type="checkbox" data-select-account="${escape(key)}" aria-label="选择 ${escape(label)} ${escape(row.wxid)}" ${selectedAccounts.has(key)?'checked':''} ${disabled?'disabled':''}></label></td><td><span class="robot-name">${escape(label)}</span><span class="account-id">${escape(row.wxid)}</span></td><td class="account-pid">${escape(row.pid)}</td><td><span class="inline-state ${!serviceAvailable?'waiting':row.reason?'failed':row.connected?'active':row.state==='error'?'failed':running?'waiting':''}">${escape(!serviceAvailable?'待更新':row.reason|| (row.connected?'已连接':statuses[row.state]||'未启动'))}</span>${row.error?`<p class="account-error">${escape(row.error)}</p>`:''}</td><td class="account-action"><button class="secondary small" data-account-action="${escape(key)}" data-action="${canStop?'stop':'start'}" aria-label="${buttonLabel} ${escape(label)} ${escape(row.wxid)}" ${buttonDisabled?'disabled':''}>${buttonLabel}</button></td></tr>`;
  }).join('')||`<tr><td colspan="5" class="table-empty">${accountsLoading?'正在识别账号…':accountsError?'账号识别失败，请刷新重试。':'未发现已登录微信。'}</td></tr>`;
  if(focusKey){const target=[...$('#account-list').querySelectorAll(focusSelector)].find(node=>(node.dataset.selectAccount||node.dataset.accountAction)===focusKey);if(target&&!target.disabled)target.focus({preventScroll:true});}
}
async function loadAccounts(quiet=false) {
  if(accountsFetching)return;
  accountsFetching=true;accountsLoading=!quiet||!loggedAccounts.length;renderAccounts();
  try{
    const data=await api('accounts');loggedAccounts=data.accounts;accountsError='';
    const available=new Set(loggedAccounts.filter(row=>row.selectable&&!activeStates.includes(row.state)).map(accountKey));
    selectedAccounts=new Set([...selectedAccounts].filter(key=>available.has(key)));
  }catch(error){accountsError=error.message;loggedAccounts=[];selectedAccounts.clear();}
  finally{accountsFetching=false;accountsLoading=false;lastAccountFetch=Date.now();renderState();}
}
async function launchAccounts(rows) {
  if(!rows.length){toast('请先勾选账号',true);return;}
  batchStarting=true;renderAccounts();
  try{
    const result=await api('accounts/start',{accounts:rows.map(({wxid,pid})=>({wxid,pid}))});
    const summary=`已提交 ${result.started} 项启动请求`+(result.already_running?`，${result.already_running} 项已在运行`:'')+(result.failed?`，${result.failed} 项失败`:'');
    $('#batch-result').innerHTML=`<p>${escape(summary)}</p>`+result.results.filter(row=>row.status==='error').map(row=>`<p class="account-error">${escape(rows.find(account=>account.wxid===row.wxid)?.name||row.wxid)}：${escape(row.error)}</p>`).join('');
    $('#batch-result').classList.remove('hidden');
    await loadRobots();await loadAccounts();
    await loadConfig();
  }finally{batchStarting=false;renderAccounts();}
}
function fill(form, values) {
  if (!values) return;
  for (const input of form.elements) {
    if (!(input.name in values)) continue;
    if (input.type === 'checkbox') input.checked = values[input.name] === true;
    else input.value = values[input.name] ?? '';
  }
}
async function loadConfig() {
  const requested = robot, loaded = await api('config');
  if (requested !== robot) return;
  config = loaded;
  fill($('#groups-form'), {...config, groups:config.groups?.join('\n')||''});
  fill($('#scope-form'), {chat_allowlist:config.chat_allowlist.join('\n')});
  for (const name of ['llm','tts']) {
    const form = $('#'+name+'-form'); fill(form, config[name]);
    form.elements.api_key.value = ''; form.elements.clear_api_key.checked = false;
    $('.key-state',form).textContent = config[name]?.api_key_configured ? '已配置' : '尚未配置';
  }
  fill($('#delay-form'), config.text_send_delay);
}
const lines = value => [...new Set(value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean))];
async function save(data, message) { await api('config',data); await loadConfig(); toast(message||'配置已保存，下次启动生效'); }
$('#start-selected').onclick = () => action(()=>launchAccounts(loggedAccounts.filter(row=>selectedAccounts.has(accountKey(row))&&row.selectable&&!activeStates.includes(row.state))));
$('#refresh-accounts').onclick=()=>loadAccounts();
$('#account-list').onchange=event=>{
  const input=event.target.closest('[data-select-account]');if(!input)return;
  if(input.checked)selectedAccounts.add(input.dataset.selectAccount);else selectedAccounts.delete(input.dataset.selectAccount);
  renderAccounts();
};
$('#account-list').onclick=event=>{
  const button=event.target.closest('[data-account-action]');if(!button)return;
  const account=loggedAccounts.find(row=>accountKey(row)===button.dataset.accountAction);if(!account)return;
  if(button.dataset.action==='stop') {
    if(!account.robot_id)return;
    action(async()=>{await api('stop',{},account.robot_id);await loadRobots();await loadAccounts();toast('正在停止，等待连接清理完成');});
  } else action(()=>launchAccounts([account]));
};
$('#plugin-search').oninput = renderPlugins;
$('#plugin-list').onclick = e => {
  const button=e.target.closest('[data-plugin]');if(!button)return;
  if(button.dataset.action==='configure') {
    if(state && ['starting','running','stopping'].includes(state.state)) {toast('请先停止机器人再编辑插件选项',true);return;}
    selectedPlugin=button.dataset.plugin;$('#plugin-dialog-title').textContent=(pluginNames[selectedPlugin]?.[0]||selectedPlugin)+' · 配置';
    $('#plugin-form').elements.options.value=JSON.stringify(config?.plugins[selectedPlugin]||{},null,2);openDialog('#plugin-dialog');return;
  }
  action(async()=>{await api('plugin',{name:button.dataset.plugin,action:button.dataset.action});await loadConfig();toast('插件操作已完成');});
};
$('#close-plugin-dialog').onclick=()=>$('#plugin-dialog').close();
$('#plugin-form').onsubmit=e=>{e.preventDefault();action(async()=>{let options;try{options=JSON.parse(e.target.elements.options.value);}catch{throw new Error('JSON 格式有误，请检查逗号和引号');}await api('plugin',{name:selectedPlugin,action:'configure',options});await loadConfig();$('#plugin-dialog').close();toast('插件配置已保存');});};
$('#groups-form').onsubmit=e=>{e.preventDefault();action(()=>save({groups:lines(e.target.elements.groups.value),history_enabled:e.target.elements.history_enabled.checked,daily_clear:e.target.elements.daily_clear.checked}));};
$('#scope-form').onsubmit=e=>{e.preventDefault();action(()=>save({chat_allowlist:lines(e.target.elements.chat_allowlist.value)}));};
for (const name of ['llm','tts']) $('#'+name+'-form').onsubmit=e=>{
  e.preventDefault();const values={};for(const input of e.target.elements){if(!input.name)continue;values[input.name]=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value;}
  action(()=>save({[name]:values}));
};
$('#delay-form').onsubmit=e=>{e.preventDefault();const f=e.target.elements;action(()=>save({text_send_delay:{enabled:f.enabled.checked,characters_per_group:Number(f.characters_per_group.value),min_seconds:Number(f.min_seconds.value),max_seconds:Number(f.max_seconds.value)}}));};
$('#discover-chats').onclick=()=>action(async()=>{
  const {chats}=await api('chats');const rooms=chats.filter(c=>c.is_group||c.chat_id?.endsWith('@chatroom'));
  $('#discovered-chats').className=rooms.length?'':'empty';$('#discovered-chats').innerHTML=rooms.map(c=>`<div class="chat-row"><div>${escape(c.name||c.chat_id)}<small>${escape(c.chat_id)}</small></div><button class="secondary small" data-room="${escape(c.chat_id)}">填入列表</button></div>`).join('')||'暂无已接收消息的群。';
});
$('#discovered-chats').onclick=e=>{const button=e.target.closest('[data-room]');if(!button)return;const field=$('#groups-form').elements.groups;field.value=[...new Set([...lines(field.value),button.dataset.room])].join('\n');toast('已填入，停止机器人后保存群聊配置');};
function renderMessages() {
  const query=$('#message-search').value.trim().toLowerCase();
  const rows=messages.filter(row=>!query||[row.chat_id,row.sender_id,row.sender_name,row.content].some(value=>String(value||'').toLowerCase().includes(query)));
  $('#message-count').textContent=(query?rows.length+' / '+messages.length:messages.length)+' 条';
  $('#message-error').textContent=messageError;$('#message-error').classList.toggle('hidden',!messageError);
  const signature=JSON.stringify([robot,query,rows,rows.length?null:state?.state]);
  if(signature===messageSignature)return;
  messageSignature=signature;
  const scroll=$('#message-scroll'),previousTop=scroll.scrollTop;
  $('#message-list').innerHTML=rows.map(row=>`<tr><td class="message-time" title="${escape(row.date+' '+row.time)}">${escape(row.time)}</td><td><span class="message-context">${row.is_group?'群聊':'私聊'}</span><span class="message-id">${escape(row.chat_id)}</span></td><td><span class="message-sender">${escape(row.sender_name||row.sender_id||'未知发送者')}</span>${row.sender_id?`<span class="message-id">${escape(row.sender_id)}</span>`:''}${row.direction==='outgoing'?'<span class="message-context">发出</span>':''}</td><td class="message-content">${escape(row.content)}</td></tr>`).join('')||`<tr><td colspan="4" class="table-empty">${query?'没有匹配的消息。':state&&activeStates.includes(state.state)?'等待新消息…':'暂无消息。'}</td></tr>`;
  scroll.scrollTop=messagePaused?previousTop:scroll.scrollHeight;
}
$('#message-search').oninput=renderMessages;
$('#pause-messages').onclick=()=>{messagePaused=!messagePaused;$('#pause-messages').textContent=messagePaused?'继续滚动':'暂停滚动';if(!messagePaused)$('#message-scroll').scrollTop=$('#message-scroll').scrollHeight;};
$('#clear-messages').onclick=()=>{messages=[];renderMessages();};
async function pollMessages(requested,generation) {
  try {
    let tail=await api('messages?after='+messageCursor,undefined,requested);
    if(requested!==robot||generation!==messageGeneration)return;
    if(messageStream&&messageStream!==tail.stream_id&&!tail.reset) {
      tail=await api('messages?after=0',undefined,requested);
      if(requested!==robot||generation!==messageGeneration)return;
    }
    if((messageStream&&messageStream!==tail.stream_id)||tail.reset)messages=[];
    messageStream=tail.stream_id;messageCursor=tail.cursor;messageError='';
    const existing=new Set(messages.map(row=>row.id));
    messages=[...messages,...tail.rows.filter(row=>!existing.has(row.id))].slice(-1000);
    renderMessages();
  } catch(error) {
    if(requested!==robot||generation!==messageGeneration)return;
    messageError=error.status===404?'消息监控尚未加载，请重启管理台后刷新。':'消息读取失败，请稍后重试。';
    renderMessages();
  }
}
function renderLogs(){
  const level=$('#log-level').value;const allowed=level==='WARNING'?['WARNING','ERROR','CRITICAL']:['ERROR','CRITICAL'];
  const rows=logs.filter(r=>level==='ALL'||allowed.includes(r.level));const view=$('#log-list');const previousTop=view.scrollTop;
  view.innerHTML=rows.map(r=>`<div class="log-row" data-level="${escape(r.level)}"><span class="log-time">${escape(r.time)}</span><span class="log-level">${escape(r.level)}</span><span class="log-message">[${escape(r.logger)}] ${escape(r.message)}</span></div>`).join('')||'<div class="log-empty">暂无符合条件的日志。</div>';
  $('#log-count').textContent=rows.length+' 条';view.scrollTop=paused?previousTop:view.scrollHeight;
}
$('#pause-logs').onclick=()=>{paused=!paused;$('#pause-logs').textContent=paused?'继续滚动':'暂停滚动';};
$('#clear-logs').onclick=()=>{logs=[];renderLogs();};$('#log-level').onchange=renderLogs;
function serviceDisconnected() {
  serviceAvailable=false;
  $('#service-banner').textContent='连接中断，状态待更新。服务重启后请刷新页面。';
  $('#service-banner').classList.remove('hidden'); renderState();
}
async function poll(){
  if(pollBusy)return;pollBusy=true;
  const requested=robot,generation=messageGeneration;
  try{
    const [next,tail]=await Promise.all([api('state'),api('logs?after='+logCursor)]);
    if(requested!==robot||generation!==messageGeneration)return;
    state=next;serviceAvailable=true;
    $('#service-banner').classList.add('hidden');renderState();
    if(tail.cursor<logCursor)logs=[];logCursor=tail.cursor;
    if(tail.rows.length){logs=[...logs,...tail.rows].slice(-1000);renderLogs();}
    await pollMessages(requested,generation);
    if(requested!==robot||generation!==messageGeneration)return;
    if(Date.now()-lastRobotFetch>4000){lastRobotFetch=Date.now();await loadRobots();}
    if(Date.now()-lastAccountFetch>10000)await loadAccounts(true);
  }catch(error){serviceDisconnected();}
  finally{pollBusy=false;}
}
document.querySelectorAll('[data-page]').forEach(link=>link.addEventListener('click',()=>{if(link.hash===location.hash)navigate();}));
window.addEventListener('hashchange',navigate);navigate();
async function loadRobots(){
  const data=await api('robots');robotRows=data.robots;
  if(!robotRows.some(row=>row.id===robot))robot='default';
  renderState();
}
async function switchRobot(identifier){
  messageGeneration++;messages=[];messageCursor=0;messageStream='';messageError='';$('#message-search').value='';messageSignature='';renderMessages();
  robot=identifier;sessionStorage.setItem('helper-robot',robot);
  $('#robot-select').value=robot;
  state=null;config=null;logs=[];logCursor=0;selectedPlugin='';renderLogs();
  renderState();renderAccounts();
  $('#discovered-chats').className='empty';$('#discovered-chats').textContent='暂无已接收消息的群。';
  await loadConfig();await poll();navigate();
}
$('#robot-select').onchange=event=>{
  const value=event.target.value;
  action(async()=>{
    if(value.startsWith('account:')) {
      const account=loggedAccounts.find(row=>'account:'+accountKey(row)===value);
      if(!account)throw new Error('账号已退出，请刷新账号');
      const entry=await api('accounts/prepare',{wxid:account.wxid,pid:account.pid});
      await loadRobots();await loadAccounts();await switchRobot(entry.id);
    } else await switchRobot(value);
  });
};
function setNavigation(open, restoreFocus=false) {
  const mobile=matchMedia('(max-width: 800px)').matches;
  const show=mobile&&open;
  document.body.classList.toggle('nav-open',show);
  $('#primary-sidebar').inert=mobile&&!show;
  $('#nav-backdrop').classList.toggle('hidden',!show);
  $('#nav-toggle').setAttribute('aria-expanded',String(show));
  $('#nav-toggle').setAttribute('aria-label',show?'关闭导航':'展开导航');
  if(show)$('#primary-sidebar [aria-current="page"]')?.focus();
  else if(restoreFocus)$('#nav-toggle').focus();
}
$('#nav-toggle').onclick=()=>setNavigation(!document.body.classList.contains('nav-open'));
$('#nav-backdrop').onclick=()=>setNavigation(false,true);
document.addEventListener('keydown',e=>{
  if(!document.body.classList.contains('nav-open'))return;
  if(e.key==='Escape'){setNavigation(false,true);return;}
  if(e.key==='Tab'){
    const links=[...$('#primary-sidebar').querySelectorAll('a')];
    if(e.shiftKey&&document.activeElement===links[0]){e.preventDefault();links.at(-1).focus();}
    else if(!e.shiftKey&&document.activeElement===links.at(-1)){e.preventDefault();links[0].focus();}
  }
});
matchMedia('(max-width: 800px)').addEventListener('change',()=>setNavigation(false));setNavigation(false);
document.addEventListener('invalid',e=>{const group=e.target.closest('details');if(group)group.open=true;},true);
(async()=>{renderState();try{await loadRobots();await loadConfig();await loadAccounts();await poll();}catch(error){serviceDisconnected();toast(error.message,true);}setInterval(poll,2000);})();

  }, {once:true});
})();
