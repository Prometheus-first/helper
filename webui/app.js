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
let pluginEditor = null, pluginEditorGeneration = 0, pluginFunctionSignature = '';
let robot = sessionStorage.getItem('helper-robot') || 'default', robotRows = [], lastRobotFetch = 0;
let pluginSignature = '', accountSignature = '', robotOptionsSignature = '';
let messages=[], messageCursor=0, messageStream='', messagePaused=false, messageError='', messageGeneration=0, messageSignature='';
let loggedAccounts = [], selectedAccounts = new Set(), accountsError = '', accountsLoading = true, accountsFetching = false, lastAccountFetch = 0, batchStarting = false;
let loginAttempt = '', loginGeneration = 0, loginTimer = null, loginStarting = false;
let sendMembers = [], mentionSelection = new Set(), sendSession = '', sendGeneration = 0, sendLoading = false, pendingSend = null, sendPolling = false;
let sendChatsAttempted = false;
let taskRows=[], taskActions=[], taskActionSignature='', taskGeneration=0, taskLoading=false, taskLastFetch=0, taskSignature='';
let groupNameDraft = new Map(), discoveredGroups = [];
let contactData=null, contactGeneration=0, contactLoading=false, contactError='', contactIdentity='', contactDetail='', contactPage=0, contactSignature='';
const contactPageSize=50;
const accountKey = row => row.pid + ':' + row.wxid;
const activeStates = ['starting','running','stopping'];
const profileLabel = row => loggedAccounts.find(account=>account.wxid===row?.account)?.name || row?.account || (row?.id==='default'?'默认配置':row?.name) || '当前账号';
const pages = {overview:'运行概览', plugins:'插件管理', groups:'群聊与会话', tasks:'定时任务', contacts:'联系人', moments:'朋友圈', models:'模型与语音', logs:'运行日志'};
const pluginNames = {anti_recall:['文字防撤回'], anti_recall_image:['图片防撤回'], anti_recall_file:['文件防撤回'], anti_recall_voice:['语音防撤回'], image_gallery:['图库图片'], picture_idiom:['看图猜成语'], idiom_chain:['成语接龙'], chat_summary:['聊天总结'], mention_chat:['艾特对话'], voice_reply:['语音回复'], group_notice:['进退群提醒'], member_identity:['群内昵称绑定'], personality:['切换人格'], friend_auto_accept:['自动通过好友'], chat_records:['聊天记录管理'], id_responder:['会话 ID 查询'], echo:['文字回声']};
const escape = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, body, identifier = robot) {
  const scope = (path === 'robots' || path === 'accounts' || path.startsWith('accounts/') || path.startsWith('wechat-login/')) ? '' : (path.includes('?') ? '&' : '?') + 'robot=' + encodeURIComponent(identifier);
  const response = await fetch('/api/' + path + scope, {method: body === undefined ? 'GET' : 'POST', cache:'no-store',
    headers:{'X-Helper-Token':token, ...(body === undefined ? {} : {'Content-Type':'application/json'})},
    ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  const data = await response.json();
  if (!response.ok) { const error=new Error(data.error||'操作失败');error.status=response.status;throw error; }
  return data;
}
const moments = window.createMomentsComposer({api, robots:()=>robotRows, label:profileLabel, available:()=>serviceAvailable});
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
  moments.render();
  if(name==='groups' && state?.connected && !sendChatsAttempted && !sendLoading)loadSendChats();
  if(name==='tasks')loadTasks();
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
  document.querySelectorAll('form:not(#send-form):not(#plugin-form):not(#moments-form):not(#task-form) .primary').forEach(node=>node.disabled=unavailable||!state||running||busy);
  $('#task-save').disabled=unavailable||!state||busy;
  renderSendState();
  renderContacts();
  moments.render();
  document.querySelectorAll('.save-hint').forEach(node=>node.textContent=unavailable?'连接中断':!state?'正在读取':running?'停止后可保存':'启动后生效');
  if(state)$('#nav-plugin-count').textContent=state.plugins.length;
  renderPlugins();
  renderPluginEditor();
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
    return `<tr class="plugin-row"><td><span class="plugin-name">${escape(name)}</span><span class="plugin-code">${escape(p.name)}</span></td><td class="plugin-description">${escape(p.description)}${p.error?`<p class="plugin-error">${escape(p.error)}</p>`:''}</td><td><span class="inline-state ${!serviceAvailable?'waiting':p.active?'active':p.error?'failed':''}">${!serviceAvailable?'待更新':p.active?'运行中':p.enabled?'已启用':'已停用'}</span></td><td><div class="plugin-actions"><button class="secondary small" data-plugin="${escape(p.name)}" data-action="${p.enabled?'disable':'enable'}" ${busy||!serviceAvailable?'disabled':''}>${p.enabled?'停用':'启用'}</button><button class="text-button" data-plugin="${escape(p.name)}" data-action="reload" ${!p.active||busy||!serviceAvailable?'disabled':''}>重载</button><button class="text-button" data-plugin="${escape(p.name)}" data-action="configure" ${busy||!serviceAvailable?'disabled':''}>配置</button><button class="text-button" data-plugin="${escape(p.name)}" data-action="reset" ${busy||!serviceAvailable?'disabled':''}>重置</button></div></td></tr>`;
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
async function closeWechatLogin() {
  const attempt=loginAttempt; loginGeneration++; loginAttempt=''; loginStarting=false;
  clearTimeout(loginTimer); $('#wechat-login-qr').removeAttribute('src');
  $('#wechat-login-qr').classList.add('hidden'); $('#wechat-login-panel').classList.add('hidden');
  $('#login-wechat').disabled=false;
  if(attempt){try{await api('wechat-login/cancel',{attempt_id:attempt});}catch{}}
}
async function pollWechatLogin(attempt,generation) {
  if(generation!==loginGeneration || attempt!==loginAttempt)return;
  try {
    const result=await api('wechat-login/status?attempt_id='+encodeURIComponent(attempt));
    if(generation!==loginGeneration || attempt!==loginAttempt)return;
    const image=$('#wechat-login-qr'), status=$('#wechat-login-status');
    if(result.status==='qr_available'){
      image.src='data:image/png;base64,'+result.qrcode_base64; image.classList.remove('hidden');
      status.textContent='请使用手机微信扫码，并在手机确认登录。';
    }else{
      image.removeAttribute('src');image.classList.add('hidden');
      status.textContent=result.status==='logged_in'?'已登录，可在上方选择账号。':
        result.status==='exited'?'微信进程已退出，请在微信中重新打开登录窗口。':
        result.status==='starting'?'正在启动微信…':'等待二维码，请保持微信登录窗口打开且未最小化。';
    }
    if(result.status==='logged_in'){
      loginAttempt='';$('#login-wechat').disabled=false;
      try{await api('wechat-login/cancel',{attempt_id:attempt});await loadRobots();await loadAccounts();}catch{}
      return;
    }
    if(result.status==='exited'){loginAttempt='';$('#login-wechat').disabled=false;try{await api('wechat-login/cancel',{attempt_id:attempt});}catch{}return;}
    loginTimer=setTimeout(()=>pollWechatLogin(attempt,generation),2000);
  } catch(error) {
    if(generation!==loginGeneration)return;
    $('#wechat-login-qr').removeAttribute('src');$('#wechat-login-qr').classList.add('hidden');
    $('#wechat-login-status').textContent=error.message;$('#login-wechat').disabled=false;
    const old=loginAttempt;loginAttempt='';if(old){try{await api('wechat-login/cancel',{attempt_id:old});}catch{}}
  }
}
$('#login-wechat').onclick=async()=>{
  if(loginStarting || loginAttempt)return;
  loginStarting=true;const generation=++loginGeneration;$('#login-wechat').disabled=true;
  $('#wechat-login-panel').classList.remove('hidden');$('#wechat-login-status').textContent='正在启动微信…';
  try{
    const result=await api('wechat-login/start',{});
    if(generation!==loginGeneration){try{await api('wechat-login/cancel',{attempt_id:result.attempt_id});}catch{}return;}
    loginAttempt=result.attempt_id;await pollWechatLogin(loginAttempt,generation);
  }catch(error){if(generation===loginGeneration){$('#wechat-login-status').textContent=error.message;$('#login-wechat').disabled=false;}}
  finally{loginStarting=false;}
};
$('#close-wechat-login').onclick=closeWechatLogin;
function fill(form, values) {
  if (!values) return;
  for (const input of form.elements) {
    if (!(input.name in values)) continue;
    if (input.type === 'checkbox') input.checked = values[input.name] === true;
    else input.value = input.name === 'extra_body' ? JSON.stringify(values[input.name] ?? {}, null, 2) : values[input.name] ?? '';
  }
}
async function loadConfig() {
  const requested = robot, loaded = await api('config');
  if (requested !== robot) return;
  config = loaded;
  fill($('#groups-form'), {...config, groups:config.groups?.join('\n')||''});
  groupNameDraft = new Map(Object.entries(config.group_names || {}));
  renderGroupNames(); renderDiscoveredGroups();
  fill($('#scope-form'), {chat_allowlist:config.chat_allowlist.join('\n')});
  for (const name of ['llm','tts']) {
    const form = $('#'+name+'-form');
    fill(form, {...(name === 'llm' ? {json_mode:true, token_limit_parameter:'max_tokens', extra_body:{}} : {}), ...config[name]});
    form.elements.api_key.value = ''; form.elements.clear_api_key.checked = false;
    $('.key-state',form).textContent = config[name]?.api_key_configured ? '已配置' : '尚未配置';
  }
  fill($('#delay-form'), config.text_send_delay);
}
const lines = value => [...new Set(value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean))];
function renderGroupNames() {
  const rooms = lines($('#group-ids').value);
  const supported = !!config && Object.hasOwn(config, 'group_names');
  $('#group-name-list').innerHTML = rooms.length ? '<h3 class="group-name-heading">群聊备注</h3>' +
    (supported ? '' : '<p class="group-name-hint">请重启管理台后编辑群备注。</p>') + rooms.map((room, index) =>
    `<div class="group-name-row"><label for="group-name-${index}">${escape(room)}</label><input id="group-name-${index}" data-group-name="${escape(room)}" value="${escape(groupNameDraft.get(room) || '')}" maxlength="64" placeholder="填写备注" autocomplete="off" ${supported ? '' : 'disabled'}></div>`).join('') : '';
}
function renderDiscoveredGroups() {
  const node = $('#discovered-chats');
  node.className = discoveredGroups.length ? '' : 'empty';
  node.innerHTML = discoveredGroups.map(c => {
    const name = config?.group_names?.[c.chat_id] || c.name || '';
    return `<div class="chat-row"><div>${escape(name || c.chat_id)}${name ? `<small>${escape(c.chat_id)}</small>` : ''}</div><button class="secondary small" data-room="${escape(c.chat_id)}">填入列表</button></div>`;
  }).join('') || '暂无已接收消息的群。';
}
$('#group-ids').addEventListener('input', renderGroupNames);
$('#group-name-list').addEventListener('input', event => {
  const input = event.target.closest('[data-group-name]');
  if (input) groupNameDraft.set(input.dataset.groupName, input.value);
});
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
  if(['configure','reset'].includes(button.dataset.action)) {
    action(()=>openPluginEditor(button.dataset.plugin,button.dataset.action==='reset'));return;
  }
  action(async()=>{await api('plugin',{name:button.dataset.plugin,action:button.dataset.action});await loadConfig();toast('插件操作已完成');});
};
function flushPluginBuffer() {
  if(pluginEditor?.loaded&&pluginEditor.functionId)pluginEditor.functions[pluginEditor.functionId]=$('#plugin-source').value;
}
function pluginDirty() {
  return !!pluginEditor?.loaded&&(Object.keys(pluginEditor.functions).some(name=>pluginEditor.functions[name]!==pluginEditor.originalFunctions[name])||$('#plugin-options').value!==pluginEditor.originalOptions);
}
function renderPluginEditor() {
  if(!$('#plugin-dialog').open)return;
  const loaded=!!pluginEditor?.loaded, unavailable=busy||!serviceAvailable||!loaded;
  const dirty=pluginDirty();
  $('#save-plugin-source').disabled=unavailable||!dirty;
  $('#reset-plugin-source').disabled=unavailable;
  $('#confirm-plugin-reset').disabled=unavailable;
  $('#cancel-plugin-reset').disabled=busy;
  $('#reload-plugin-source').disabled=busy||!serviceAvailable;
  $('#plugin-function').disabled=unavailable;
  $('#plugin-source').readOnly=unavailable||!pluginEditor?.functionId;$('#plugin-options').readOnly=unavailable;
  $('#close-plugin-dialog').disabled=busy;
  $('#plugin-editor-status').textContent=!serviceAvailable?'连接中断，修改尚未丢失':!loaded?'正在读取…':dirty?'有未保存的修改':pluginEditor.feedback||'保存后仅对当前账号生效';
  $('#plugin-editor-status').classList.toggle('unsaved',dirty);
  if(loaded) {
    const ids=Object.keys(pluginEditor.functions), single=ids.length<2;
    $('#plugin-function').classList.toggle('hidden',single);
    $('#plugin-current-function').classList.toggle('hidden',!single);
    $('#plugin-current-function').textContent=pluginEditor.function_labels[pluginEditor.functionId]||'暂无业务函数';
    const signature=JSON.stringify(Object.keys(pluginEditor.functions).map(name=>[name,pluginEditor.function_labels[name],pluginEditor.functions[name]!==pluginEditor.originalFunctions[name]]));
    if(signature!==pluginFunctionSignature){
      pluginFunctionSignature=signature;
      $('#plugin-function').innerHTML=Object.keys(pluginEditor.functions).map(name=>`<option value="${escape(name)}">${escape(pluginEditor.function_labels[name]||name)}${pluginEditor.functions[name]!==pluginEditor.originalFunctions[name]?' *':''}</option>`).join('');
      $('#plugin-function').value=pluginEditor.functionId;
    }
    const lines=$('#plugin-source').value.split('\n').length;
    $('.code-editor').classList.toggle('compact-code',lines<12);
    $('#plugin-line-numbers').textContent=Array.from({length:lines},(_,i)=>i+1).join('\n');
    $('#plugin-line-numbers').scrollTop=$('#plugin-source').scrollTop;
  }
}
function pluginTab(name, focus=false) {
  for(const tab of ['code','options']) {
    const active=tab===name,node=$('#plugin-'+tab+'-tab');
    node.setAttribute('aria-selected',String(active));node.tabIndex=active?0:-1;
    $('#plugin-'+tab+'-panel').classList.toggle('hidden',!active);
    if(active&&focus)node.focus();
  }
}
function acceptPluginBusiness(data, feedback='') {
  const functionId=pluginEditor?.functionId in data.functions?pluginEditor.functionId:Object.keys(data.functions)[0];
  pluginEditor={...pluginEditor,...data,functionId,loaded:true,originalFunctions:{...data.functions},functions:{...data.functions},originalOptions:JSON.stringify(data.options,null,2),feedback};
  $('#plugin-source').value=data.functions[functionId]||'';
  $('#plugin-source').scrollTop=0;$('#plugin-line-numbers').scrollTop=0;
  $('#plugin-options').value=pluginEditor.originalOptions;
  $('#plugin-editor-scope').textContent=profileLabel(robotRows.find(row=>row.id===pluginEditor.robot))+' · '+(data.customized?'自定义代码':'默认代码');
  pluginFunctionSignature='';$('#plugin-reset-confirm').classList.add('hidden');renderPluginEditor();
}
async function openPluginEditor(name, reset=false) {
  const generation=++pluginEditorGeneration,identifier=robot;
  selectedPlugin=name;pluginEditor={name,robot:identifier,loaded:false};pluginFunctionSignature='';
  $('#plugin-dialog-title').textContent=(pluginNames[name]?.[0]||name)+' · 配置';
  $('#plugin-editor-scope').textContent='';$('#plugin-source').value='';$('#plugin-options').value='';$('#plugin-function').replaceChildren();$('#plugin-line-numbers').textContent='';
  $('#plugin-reset-confirm').classList.add('hidden');pluginTab('code');
  if(!$('#plugin-dialog').open)openDialog('#plugin-dialog');
  renderPluginEditor();
  const data=await api('plugin-business?name='+encodeURIComponent(name),undefined,identifier);
  if(generation!==pluginEditorGeneration||identifier!==robot)return;
  acceptPluginBusiness(data);
  if(reset)$('#plugin-reset-confirm').classList.remove('hidden');
}
async function submitPluginSource(reset=false) {
  if(!pluginEditor?.loaded||pluginEditor.robot!==robot)throw new Error('请重新打开当前账号的插件');
  flushPluginBuffer();
  const target=pluginEditor,generation=pluginEditorGeneration;
  let options;
  if(!reset){
    try{options=JSON.parse($('#plugin-options').value);}catch{throw new Error('配置的 JSON 格式有误，请检查逗号和引号');}
    if(!options||Array.isArray(options)||typeof options!=='object')throw new Error('插件配置应为 JSON 对象');
  }
  const data=await api('plugin-business',{name:target.name,revision:target.revision,action:reset?'reset':'save',...(reset?{}:{functions:target.functions,options})},target.robot);
  if(generation!==pluginEditorGeneration||target.robot!==robot)return;
  const feedback=(reset?'已重置':'已保存')+(data.applied?'并重载':data.running?'，插件保持停用':'，下次启动生效');
  acceptPluginBusiness(data,feedback);await loadConfig();
}
function closePluginEditor(){
  if(busy)return;
  flushPluginBuffer();
  if(pluginDirty()&&!confirm('有未保存的修改，确定关闭？'))return;
  $('#plugin-dialog').close();
}
$('#close-plugin-dialog').onclick=closePluginEditor;
$('#plugin-dialog').oncancel=event=>{event.preventDefault();closePluginEditor();};
$('#plugin-dialog').onclose=()=>{pluginEditorGeneration++;pluginEditor=null;selectedPlugin='';};
$('#plugin-function').onchange=()=>{
  flushPluginBuffer();pluginEditor.functionId=$('#plugin-function').value;
  $('#plugin-source').value=pluginEditor.functions[pluginEditor.functionId];$('#plugin-source').scrollTop=0;$('#plugin-line-numbers').scrollTop=0;renderPluginEditor();
};
$('#plugin-source').oninput=()=>{flushPluginBuffer();renderPluginEditor();};
$('#plugin-options').oninput=renderPluginEditor;
$('#plugin-source').onscroll=()=>{$('#plugin-line-numbers').scrollTop=$('#plugin-source').scrollTop;};
$('#plugin-source').onkeydown=event=>{
  if(event.key==='Tab'&&!event.shiftKey&&!event.target.readOnly){event.preventDefault();event.target.setRangeText('    ',event.target.selectionStart,event.target.selectionEnd,'end');event.target.dispatchEvent(new Event('input'));}
};
$('#plugin-dialog').addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s'){event.preventDefault();if(!$('#save-plugin-source').disabled)$('#plugin-form').requestSubmit();}});
for(const name of ['code','options']) {
  const tab=$('#plugin-'+name+'-tab');tab.onclick=()=>pluginTab(name);
  tab.onkeydown=event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();pluginTab(event.key==='Home'?'code':event.key==='End'?'options':name==='code'?'options':'code',true);}};
}
$('#reload-plugin-source').onclick=()=>{flushPluginBuffer();if(pluginDirty()&&!confirm('重新读取会丢弃未保存的修改，继续？'))return;action(()=>openPluginEditor(pluginEditor.name));};
$('#reset-plugin-source').onclick=()=>{$('#plugin-reset-confirm').classList.remove('hidden');$('#confirm-plugin-reset').focus();};
$('#cancel-plugin-reset').onclick=()=>{$('#plugin-reset-confirm').classList.add('hidden');$('#reset-plugin-source').focus();};
$('#confirm-plugin-reset').onclick=()=>action(()=>submitPluginSource(true));
$('#plugin-form').onsubmit=event=>{event.preventDefault();if(!$('#save-plugin-source').disabled)action(()=>submitPluginSource());};
$('#groups-form').onsubmit=e=>{e.preventDefault();const groups=lines(e.target.elements.groups.value);const data={groups,history_enabled:e.target.elements.history_enabled.checked,daily_clear:e.target.elements.daily_clear.checked};if(config&&Object.hasOwn(config,'group_names'))data.group_names=Object.fromEntries(groups.map(room=>[room,groupNameDraft.get(room)||'']));action(()=>save(data));};
$('#scope-form').onsubmit=e=>{e.preventDefault();action(()=>save({chat_allowlist:lines(e.target.elements.chat_allowlist.value)}));};
for (const name of ['llm','tts']) $('#'+name+'-form').onsubmit=e=>{
  e.preventDefault();
  action(()=>{
    const values={};
    for(const input of e.target.elements){if(!input.name)continue;values[input.name]=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value;}
    if(name === 'llm') {
      try { values.extra_body=JSON.parse(values.extra_body.trim() || '{}'); }
      catch { throw new Error('扩展参数的 JSON 格式有误，请检查逗号和引号'); }
      if(values.extra_body === null || Array.isArray(values.extra_body) || typeof values.extra_body !== 'object')throw new Error('扩展参数应为 JSON 对象，例如 {}');
    }
    return save({[name]:values});
  });
};
$('#delay-form').onsubmit=e=>{e.preventDefault();const f=e.target.elements;action(()=>save({text_send_delay:{enabled:f.enabled.checked,characters_per_group:Number(f.characters_per_group.value),min_seconds:Number(f.min_seconds.value),max_seconds:Number(f.max_seconds.value)}}));};
$('#discover-chats').onclick=()=>action(async()=>{
  const {chats}=await api('chats');discoveredGroups=chats.filter(c=>c.is_group||c.chat_id?.endsWith('@chatroom'));
  renderDiscoveredGroups();
});
$('#discovered-chats').onclick=e=>{const button=e.target.closest('[data-room]');if(!button)return;const field=$('#groups-form').elements.groups;field.value=[...new Set([...lines(field.value),button.dataset.room])].join('\n');renderGroupNames();toast('已填入，停止机器人后保存群聊配置');};

const roleLabels = {owner:'群主', admin:'群管理员', member:'普通群友', unknown:'身份未知'};
function sendFeedback(text, error=false) {
  $('#send-status').textContent=text;$('#send-status').classList.toggle('error',error);
}
function renderSendState() {
  const ready=serviceAvailable&&!!state?.connected&&!busy;
  $('#refresh-send-chats').disabled=!ready||sendLoading||!!pendingSend;
  $('#refresh-send-members').disabled=!ready||sendLoading||!!pendingSend||!$('#send-chat').value.endsWith('@chatroom');
  $('#send-chat').disabled=!ready||sendLoading||!!pendingSend;
  $('#send-content').disabled=!ready||!!pendingSend;
  $('#send-text').disabled=!ready||sendLoading||!!pendingSend||!sendSession||!$('#send-chat').value||!$('#send-content').value.trim();
  $('#send-text').textContent=pendingSend?'发送中…':'发送';
  $('#mention-count').textContent=`已选 ${mentionSelection.size} / 8`;
  $('#send-members').querySelectorAll('input').forEach(node=>node.disabled=!ready||!!pendingSend);
}
function renderSendMembers() {
  const query=$('#member-search').value.trim().toLowerCase();
  const rows=sendMembers.filter(row=>[row.nickname,row.wxid,roleLabels[row.role]||roleLabels.unknown].some(value=>String(value||'').toLowerCase().includes(query)));
  $('#send-members').innerHTML=rows.map(row=>`<tr><td><label class="member-choice"><input type="checkbox" data-mention="${escape(row.wxid)}" ${mentionSelection.has(row.wxid)?'checked':''} aria-label="艾特 ${escape(row.nickname||row.wxid)}"></label></td><td><span class="message-sender">${escape(row.nickname||row.wxid)}</span><span class="message-id">${escape(row.wxid)}</span></td><td class="member-role">${escape(roleLabels[row.role]||roleLabels.unknown)}</td></tr>`).join('')||'<tr><td colspan="3" class="table-empty">'+(query?'没有匹配的成员。':'暂无可用的成员，请在微信打开该群后刷新。')+'</td></tr>';
  renderSendState();
}
function resetComposer() {
  sendGeneration++;sendMembers=[];mentionSelection.clear();sendSession='';sendLoading=false;pendingSend=null;
  sendChatsAttempted=false;
  $('#send-chat').innerHTML='<option value="">选择会话</option>';$('#send-content').value='';$('#member-search').value='';
  $('#send-group').classList.add('hidden');sendFeedback('');renderSendMembers();
}
async function loadSendChats() {
  const requested=robot,generation=++sendGeneration;
  let loaded=false;
  sendChatsAttempted=true;
  sendLoading=true;sendSession='';sendFeedback('正在读取会话…');renderSendState();
  try {
    const [{chats},connection,settings]=await Promise.all([api('chats',undefined,requested),api('send-connection',undefined,requested),api('config',undefined,requested)]);
    if(requested!==robot||generation!==sendGeneration)return;
    const previous=$('#send-chat').value, rows=[...chats];
    const known=new Set(rows.map(row=>row.chat_id));
    for(const room of settings.groups||[])if(!known.has(room)){rows.push({chat_id:room,is_group:true});known.add(room);}
    if(!rows.some(row=>row.chat_id==='filehelper'))rows.unshift({chat_id:'filehelper',name:'文件传输助手'});
    $('#send-chat').innerHTML='<option value="">选择会话</option>'+rows.map(row=>{const name=settings.group_names?.[row.chat_id]||row.name||'';return `<option value="${escape(row.chat_id)}">${escape(name||row.chat_id)}${name?' · '+escape(row.chat_id):''}</option>`;}).join('');
    if(rows.some(row=>row.chat_id===previous))$('#send-chat').value=previous;
    sendSession=connection.session_id;sendFeedback('');loaded=true;
  } catch(error) {if(requested===robot&&generation===sendGeneration)sendFeedback(error.message,true);}
  finally {if(requested===robot&&generation===sendGeneration){sendLoading=false;renderSendState();}}
  if(loaded&&requested===robot&&generation===sendGeneration)await selectSendChat();
}
async function selectSendChat() {
  const room=$('#send-chat').value,requested=robot,generation=++sendGeneration;
  sendMembers=[];mentionSelection.clear();$('#member-search').value='';
  $('#send-group').classList.toggle('hidden',!room.endsWith('@chatroom'));renderSendMembers();
  if(!room){renderSendState();return;}
  sendSession='';sendLoading=true;sendFeedback('正在读取…');renderSendState();
  try {
    const connection=await api('send-connection',undefined,requested);
    if(requested!==robot||generation!==sendGeneration)return;
    if(room.endsWith('@chatroom')) {
      try {
        const data=await api('group-members?room='+encodeURIComponent(room),undefined,requested);
        if(requested!==robot||generation!==sendGeneration)return;
        if(data.session_id!==connection.session_id)throw new Error('微信连接已变化，请刷新会话');
        sendMembers=data.members||[];
      } catch(error) {
        if(requested!==robot||generation!==sendGeneration)return;
        if(error.status!==409||error.message!=='群成员缓存尚未就绪，请在微信打开该群后重试')throw error;
        sendSession=connection.session_id;sendFeedback('群成员尚未加载，可发送文字；在微信打开群聊后刷新成员。');renderSendMembers();return;
      }
    }
    sendSession=connection.session_id;sendFeedback('');renderSendMembers();
  } catch(error) {if(requested===robot&&generation===sendGeneration)sendFeedback(error.message,true);}
  finally {if(requested===robot&&generation===sendGeneration){sendLoading=false;renderSendState();}}
}
$('#refresh-send-chats').onclick=loadSendChats;
$('#send-chat').onchange=selectSendChat;$('#refresh-send-members').onclick=selectSendChat;
$('#member-search').oninput=renderSendMembers;$('#send-content').oninput=renderSendState;
$('#send-members').onchange=event=>{
  const node=event.target;if(!node.matches('[data-mention]'))return;
  if(node.checked&&mentionSelection.size>=8){node.checked=false;sendFeedback('每条消息最多艾特 8 人',true);return;}
  if(node.checked)mentionSelection.add(node.dataset.mention);else mentionSelection.delete(node.dataset.mention);
  sendFeedback('');renderSendState();
};
async function pollSendStatus() {
  const job=pendingSend;if(!job||sendPolling)return;
  sendPolling=true;
  try {
    const result=await api('send-status?id='+encodeURIComponent(job.id),undefined,job.robot);
    if(pendingSend!==job||job.robot!==robot)return;
    if(result.status==='sending')return;
    pendingSend=null;
    if(result.status==='sent'){$('#send-content').value='';sendFeedback('已发送');}
    else sendFeedback(result.error||'发送未完成，请核对微信后再重发',true);
    renderSendState();
  } catch(error) {if(pendingSend===job)sendFeedback('暂时无法查询发送结果，请先核对微信。不会自动重发。',true);}
  finally {sendPolling=false;}
}
$('#send-form').onsubmit=async event=>{
  event.preventDefault();if($('#send-text').disabled||pendingSend)return;
  const requested=robot,job={id:crypto.randomUUID(),robot:requested};pendingSend=job;
  const body={request_id:job.id,session_id:sendSession,chat_id:$('#send-chat').value,content:$('#send-content').value,at_list:[...mentionSelection]};
  sendFeedback('正在发送…');renderSendState();
  try {
    await api('send-text',body,requested);
    if(pendingSend===job)await pollSendStatus();
  } catch(error) {
    if(pendingSend!==job)return;
    if(error.status){pendingSend=null;sendFeedback(error.message,true);}
    else sendFeedback('发送结果暂不确定，请先核对微信。不会自动重发。',true);
    renderSendState();
  }
};
setInterval(pollSendStatus,1500);
function renderMessages() {
  const query=$('#message-search').value.trim().toLowerCase();
  const rows=messages.filter(row=>!query||[row.chat_id,row.sender_id,row.sender_name,row.content,row.forwarded_by,row.forwarded_by_id,row.source_time,row.message_id,roleLabels[row.sender_role],row.recalled?'已撤回':''].some(value=>String(value||'').toLowerCase().includes(query)));
  $('#message-count').textContent=(query?rows.length+' / '+messages.length:messages.length)+' 条';
  $('#message-error').textContent=messageError;$('#message-error').classList.toggle('hidden',!messageError);
  const signature=JSON.stringify([robot,query,rows,rows.length?null:state?.state]);
  if(signature===messageSignature)return;
  messageSignature=signature;
  const scroll=$('#message-scroll'),previousTop=scroll.scrollTop;
  $('#message-list').innerHTML=rows.map(row=>`<tr><td class="message-time" title="${escape(row.date+' '+row.time)}">${escape(row.time)}</td><td><span class="message-context">${row.is_group?'群聊':'私聊'}</span><span class="message-id">${escape(row.chat_id)}</span></td><td><span class="message-sender">${escape(row.sender_name||row.sender_id||(row.forwarded?'原作者未提供':'未知发送者'))}</span>${row.sender_id?`<span class="message-id">${escape(row.sender_id)}</span>`:''}${row.is_group&&!row.forwarded&&row.sender_role&&row.sender_role!=='unknown'?`<span class="message-context">${escape(roleLabels[row.sender_role]||'')}</span>`:''}${row.forwarded?`<span class="message-context">由 ${escape(row.forwarded_by||'未知用户')} 转发</span>`:''}${row.direction==='outgoing'?'<span class="message-context">发出</span>':''}</td><td class="message-content">${row.forwarded?`<span class="message-context">转发记录 ${escape(row.forwarded_index)}${row.source_time?' · '+escape(row.source_time):''}</span>`:''}${row.recalled?`<span class="message-recalled" title="${escape(row.recall_notice||'')}">已撤回</span>`:''}${escape(row.content)}</td></tr>`).join('')||`<tr><td colspan="4" class="table-empty">${query?'没有匹配的消息。':state&&activeStates.includes(state.state)?'等待新消息…':'暂无消息。'}</td></tr>`;
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
    const updated=new Map(tail.rows.map(row=>[row.id,row]));
    messages=messages.map(row=>{const next=updated.get(row.id);updated.delete(row.id);return next||row;});
    messages=[...messages,...updated.values()].slice(-1000);
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
function contactConnectionIdentity() {
  return JSON.stringify([robot,state?.account,state?.pid,state?.started_at]);
}
function resetContacts(message='') {
  contactGeneration++;contactData=null;contactLoading=false;contactError=message;contactIdentity='';contactDetail='';contactPage=0;contactSignature='';
  $('#contact-search').value='';$('#contact-tag').innerHTML='<option value="">全部标签</option>';
  renderContacts();
}
function contactTagText(row) {
  return row.tags_status==='ready'?(row.tags?.length?row.tags.join('、'):'无标签'):'暂不可读取';
}
function renderContactDetails(row,index) {
  const fields=[['微信 ID',row.wxid,true],['微信号',row.alias,true],['昵称',row.nickname],['备注',row.remark],['描述',row.description],['性别',{male:'男',female:'女'}[row.sex]],['国家或地区',row.country],['省份',row.province],['城市',row.city],['个性签名',row.signature],['好友标签',contactTagText(row)]];
  return `<tr class="contact-detail"><td colspan="4"><dl id="contact-detail-${index}" class="contact-details" aria-label="${escape(row.remark||row.nickname||row.wxid)} 的资料">${fields.filter(([,value])=>value).map(([label,value,mono])=>`<div><dt>${label}</dt><dd${mono?' class="contact-id"':''}>${escape(value)}</dd></div>`).join('')}</dl></td></tr>`;
}
function renderContacts() {
  if(contactIdentity&&(!serviceAvailable||!state?.connected||contactIdentity!==contactConnectionIdentity())) {
    resetContacts(serviceAvailable&&state?.connected?'微信连接已变化，请重新扫描。':'当前账号已断开，连接后请重新扫描。');return;
  }
  const ready=serviceAvailable&&!!state?.connected;
  $('#scan-contacts').disabled=!ready||busy||contactLoading;
  $('#scan-contacts').textContent=contactLoading?'正在扫描…':'扫描联系人';
  $('#contact-search').disabled=!ready||!contactData||contactLoading;
  $('#contact-tag').disabled=!ready||!contactData||contactLoading;
  const query=$('#contact-search').value.trim().toLowerCase(),tag=$('#contact-tag').value;
  const all=contactData?.contacts||[];
  const rows=all.filter(row=>(!query||[row.wxid,row.alias,row.nickname,row.remark,...(row.tags||[])].some(value=>String(value||'').toLowerCase().includes(query)))&&(!tag||(tag==='__untagged'?row.tags_status==='ready'&&!row.tag_ids?.length&&!row.tags?.length:tag==='__unavailable'?row.tags_status!=='ready':row.tag_ids?.includes(tag))));
  const pageCount=Math.max(1,Math.ceil(rows.length/contactPageSize));contactPage=Math.min(contactPage,pageCount-1);
  $('#contact-count').textContent=contactData?((query||tag?rows.length+' / ':'')+all.length+' 位'):'';
  let status=!serviceAvailable?'管理服务连接中断，恢复后请重新扫描。':!state?'正在读取账号状态…':!ready?'当前账号未连接，请先启动机器人。':contactLoading?'正在扫描当前账号的联系人…':contactError;
  if(!status&&contactData) {
    const date=new Date(contactData.scanned_at),time=Number.isNaN(date.getTime())?'本次':date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
    status=time+' 扫描 · 微信本地缓存'+(contactData.complete?'':' · 名单可能尚未同步完整');
  }
  const statusText=status||'点击“扫描联系人”读取当前账号的好友。';
  if($('#contact-status').textContent!==statusText)$('#contact-status').textContent=statusText;
  $('#contact-status').classList.toggle('error',!!contactError&&ready);
  $('#contact-pagination').classList.toggle('hidden',!contactData||rows.length<=contactPageSize);
  $('#contact-page').textContent=`第 ${contactPage+1} / ${pageCount} 页`;
  $('#contact-previous').disabled=contactPage===0||contactLoading;
  $('#contact-next').disabled=contactPage>=pageCount-1||contactLoading;
  const signature=JSON.stringify([contactGeneration,contactLoading,ready,query,tag,contactPage,contactDetail,contactError]);
  if(signature===contactSignature)return;
  contactSignature=signature;
  const focusId=document.activeElement?.dataset.contact;
  const empty=contactLoading?'正在读取联系人…':!ready?'连接后点击“扫描联系人”。':contactError?'未显示联系人，请重新扫描。':!contactData?'尚未扫描联系人。':query||tag?'没有匹配的联系人。':'本地缓存中暂无好友，请在微信打开通讯录后重新扫描。';
  $('#contact-list').innerHTML=rows.slice(contactPage*contactPageSize,(contactPage+1)*contactPageSize).map(row=>{
    const index=all.indexOf(row),expanded=contactDetail===row.wxid;
    return `<tr class="contact-row${expanded?' selected':''}"><td><span class="contact-name">${escape(row.remark||row.nickname||row.wxid)}</span>${row.remark&&row.nickname&&row.remark!==row.nickname?`<span class="contact-nickname">${escape(row.nickname)}</span>`:''}<span class="contact-id">${escape(row.wxid)}</span></td><td class="contact-alias"><span class="sr-only">微信号：</span>${escape(row.alias||'未提供')}</td><td class="contact-tags"><span class="sr-only">好友标签：</span>${escape(contactTagText(row))}</td><td class="contact-action"><button type="button" class="text-button small" data-contact="${escape(row.wxid)}" aria-expanded="${expanded}"${expanded?` aria-controls="contact-detail-${index}"`:''} aria-label="${expanded?'收起':'查看'} ${escape(row.remark||row.nickname||row.wxid)} 的资料">${expanded?'收起':'详情'}</button></td></tr>`+(expanded?renderContactDetails(row,index):'');
  }).join('')||`<tr><td colspan="4" class="table-empty">${empty}</td></tr>`;
  if(focusId)[...$('#contact-list').querySelectorAll('[data-contact]')].find(node=>node.dataset.contact===focusId)?.focus({preventScroll:true});
}
async function scanContacts() {
  if($('#scan-contacts').disabled)return;
  resetContacts();
  const requested=robot,generation=contactGeneration;
  contactIdentity=contactConnectionIdentity();contactLoading=true;renderContacts();
  try {
    const connection=await api('send-connection',undefined,requested);
    if(requested!==robot||generation!==contactGeneration)return;
    const data=await api('contacts',undefined,requested);
    if(requested!==robot||generation!==contactGeneration)return;
    if(!connection.session_id||data.session_id!==connection.session_id||state?.account&&data.account!==state.account)throw new Error('微信连接已变化，请重新扫描');
    if(!Array.isArray(data.contacts)||!Array.isArray(data.tags))throw new Error('联系人返回格式不正确，请更新管理台');
    data.contacts.sort((a,b)=>String(a.remark||a.nickname||a.wxid).localeCompare(String(b.remark||b.nickname||b.wxid),'zh-CN'));
    contactData=data;
    $('#contact-tag').innerHTML='<option value="">全部标签</option>'+data.tags.map(tag=>`<option value="${escape(tag.id)}">${escape(tag.name)}</option>`).join('')+(data.contacts.some(row=>row.tags_status==='ready'&&!row.tag_ids?.length&&!row.tags?.length)?'<option value="__untagged">无标签</option>':'')+(data.contacts.some(row=>row.tags_status!=='ready')?'<option value="__unavailable">标签暂不可读取</option>':'');
  } catch(error) {
    if(requested===robot&&generation===contactGeneration)contactError=error.status===404?'联系人接口尚未加载，请重新打开管理台。':error.message||'扫描失败，请重试。';
  } finally {
    if(requested===robot&&generation===contactGeneration){contactLoading=false;renderContacts();}
  }
}
async function validateContactSession(requested,generation) {
  if(!contactData||contactLoading||!state?.connected)return;
  const session=contactData.session_id,scannedGeneration=contactGeneration;
  try {
    const connection=await api('send-connection',undefined,requested);
    if(requested!==robot||generation!==messageGeneration||scannedGeneration!==contactGeneration)return;
    if(connection.session_id!==session)resetContacts('微信连接已变化，请重新扫描。');
  } catch {
    if(requested===robot&&generation===messageGeneration&&scannedGeneration===contactGeneration)resetContacts('无法确认微信连接，请重新扫描。');
  }
}
$('#scan-contacts').onclick=scanContacts;
$('#contact-search').oninput=()=>{contactPage=0;contactDetail='';renderContacts();};
$('#contact-tag').onchange=()=>{contactPage=0;contactDetail='';renderContacts();};
$('#contact-list').onclick=event=>{const button=event.target.closest('[data-contact]');if(!button)return;contactDetail=contactDetail===button.dataset.contact?'':button.dataset.contact;renderContacts();};
for(const [id,direction] of [['previous',-1],['next',1]])$('#contact-'+id).onclick=()=>{contactPage+=direction;contactDetail='';renderContacts();$('#contact-list').closest('table').scrollIntoView({block:'start'});};
function serviceDisconnected() {
  serviceAvailable=false;
  resetContacts('管理服务连接中断，恢复后请重新扫描。');
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
    if(!next.connected)sendChatsAttempted=false;
    $('#service-banner').classList.add('hidden');renderState();
    if(next.connected && location.hash==='#groups' && !sendChatsAttempted && !sendLoading)loadSendChats();
    if(location.hash==='#tasks' && Date.now()-taskLastFetch>5000)loadTasks();
    if(tail.cursor<logCursor)logs=[];logCursor=tail.cursor;
    if(tail.rows.length){logs=[...logs,...tail.rows].slice(-1000);renderLogs();}
    await pollMessages(requested,generation);
    await validateContactSession(requested,generation);
    if(requested!==robot||generation!==messageGeneration)return;
    if(Date.now()-lastRobotFetch>4000){lastRobotFetch=Date.now();await loadRobots();}
    if(Date.now()-lastAccountFetch>10000)await loadAccounts(true);
  }catch(error){serviceDisconnected();}
  finally{pollBusy=false;}
}
function taskKind() {
  const daily=$('#task-form').elements.kind.value==='daily';
  $('#task-interval-label').classList.toggle('hidden',daily);
  $('#task-time-label').classList.toggle('hidden',!daily);
  $('#task-form').elements.interval_minutes.required=!daily;
  $('#task-form').elements.daily_time.required=daily;
}
function resetTaskForm() {
  const f=$('#task-form');f.reset();f.elements.id.value='';
  if(taskActions.length)f.elements.action_id.value='builtin.send_text';
  $('#task-form-heading').textContent='新建任务';$('#task-cancel').classList.add('hidden');taskKind();renderTaskFields();
}
function renderTaskFields(values={}) {
  const id=$('#task-action').value, action=taskActions.find(item=>item.id===id);
  $('#task-parameters').innerHTML=(action?.fields||[]).map(field=>{
    const name=escape(field.name), label=escape(field.label), required=field.required?'required':'';
    if(field.type==='boolean')return `<label class="inline"><input type="checkbox" data-task-field="${name}" ${values[field.name]?'checked':''}>${label}</label>`;
    if(field.type==='textarea')return `<label>${label}<textarea data-task-field="${name}" rows="3" ${required}>${escape(values[field.name]??'')}</textarea></label>`;
    const kind=field.type==='integer'?'number':'text';
    return `<label>${label}<input type="${kind}" ${field.type==='chat'?'list="task-chat-options" spellcheck="false"':''} data-task-field="${name}" value="${escape(values[field.name]??'')}" ${required}></label>`;
  }).join('');
}
function renderTaskActions() {
  const signature=JSON.stringify(taskActions);if(signature===taskActionSignature)return;
  taskActionSignature=signature;const select=$('#task-action'),previous=select.value;
  const values=Object.fromEntries([...$('#task-parameters').querySelectorAll('[data-task-field]')].map(node=>[node.dataset.taskField,node.type==='checkbox'?node.checked:node.value]));
  select.innerHTML=taskActions.map(item=>`<option value="${escape(item.id)}">${escape(item.label)}${item.plugin?' · '+escape(pluginNames[item.plugin]?.[0]||item.plugin):''}</option>`).join('');
  if(previous&&$('#task-form').elements.id.value&&!taskActions.some(item=>item.id===previous))select.insertAdjacentHTML('beforeend',`<option value="${escape(previous)}" disabled>原动作已不可用</option>`);
  select.value=[...select.options].some(item=>item.value===previous)?previous:'builtin.send_text';
  renderTaskFields(select.value===previous?values:{});
}
function taskTime(timestamp) { return timestamp ? new Date(timestamp*1000).toLocaleString('zh-CN',{hour12:false}) : '—'; }
function renderTasks() {
  const signature=JSON.stringify([taskRows,taskActions,serviceAvailable]);if(signature===taskSignature)return;taskSignature=signature;
  $('#task-list').innerHTML=taskRows.map(row=>{const id=row.action_id||'builtin.send_text', action=taskActions.find(item=>item.id===id), target=row.params?.chat_id||row.chat_id||'';return `<tr><td><span class="robot-name">${escape(row.name)}</span><span class="account-id">${escape(action?.label||'动作已不可用')}${target?' · '+escape(target):''}</span></td><td>${row.kind==='daily'?'每天 '+escape(row.daily_time):'每 '+escape(row.interval_seconds/60)+' 分钟'}<span class="account-id">${row.enabled?'下次 '+escape(taskTime(row.next_run)):'已暂停'}</span></td><td>${row.enabled?'已启用':'已停用'}<span class="account-id">${escape(row.last_status||'尚未执行')}${row.last_time?' · '+escape(taskTime(row.last_time)):''}</span></td><td><div class="plugin-actions"><button class="secondary small" type="button" data-task="${escape(row.id)}" data-task-action="edit">编辑</button><button class="text-button small" type="button" data-task="${escape(row.id)}" data-task-action="toggle">${row.enabled?'停用':'启用'}</button><button class="text-button small danger-text" type="button" data-task="${escape(row.id)}" data-task-action="delete">删除</button></div></td></tr>`;}).join('')||'<tr><td colspan="4" class="table-empty">暂无任务。</td></tr>';
}
async function loadTasks() {
  if(taskLoading||!serviceAvailable)return;
  taskLoading=true;taskLastFetch=Date.now();const requested=robot, generation=taskGeneration;
  try {
    const [data,types]=await Promise.all([api('scheduled-tasks',undefined,requested),api('scheduled-actions',undefined,requested)]);
    if(requested!==robot||generation!==taskGeneration)return;
    taskRows=data.tasks;taskActions=types.actions;$('#task-status').textContent='';renderTaskActions();renderTasks();
    const chats=await api('chats',undefined,requested).catch(()=>({chats:[]}));
    if(requested!==robot||generation!==taskGeneration)return;
    const options=[{chat_id:'filehelper',name:'文件传输助手'},...(chats.chats||[]),...(config?.groups||[]).map(chat_id=>({chat_id}))];
    const unique=[...new Map(options.map(row=>[row.chat_id,row])).values()];
    $('#task-chat-options').innerHTML=unique.map(row=>`<option value="${escape(row.chat_id)}">${escape(config?.group_names?.[row.chat_id]||row.name||row.chat_id)}</option>`).join('');
  } catch(error) {if(requested===robot&&generation===taskGeneration)$('#task-status').textContent=error.message;}
  finally {taskLoading=false;}
}
$('#task-form').elements.kind.onchange=taskKind;
$('#task-action').onchange=()=>renderTaskFields();
$('#task-cancel').onclick=resetTaskForm;
$('#task-refresh').onclick=loadTasks;
$('#task-form').onsubmit=event=>{
  event.preventDefault();const f=event.target.elements;
  const selected=taskActions.find(item=>item.id===f.action_id.value);if(!selected){toast('请选择有效的任务类型',true);return;}
  const params={};for(const field of selected.fields){const node=$(`[data-task-field="${field.name}"]`);params[field.name]=field.type==='boolean'?node.checked:field.type==='integer'&&node.value!==''?Number(node.value):node.value;}
  const payload={id:f.id.value||undefined,name:f.name.value,action_id:f.action_id.value,params,kind:f.kind.value,interval_seconds:Number(f.interval_minutes.value)*60,daily_time:f.daily_time.value,enabled:f.enabled.checked};
  action(async()=>{await api('scheduled-tasks',payload);resetTaskForm();await loadTasks();toast('任务已保存');});
};
$('#task-list').onclick=event=>{
  const button=event.target.closest('[data-task]');if(!button)return;
  const row=taskRows.find(item=>item.id===button.dataset.task);if(!row)return;
  if(button.dataset.taskAction==='edit'){
    const id=row.action_id||'builtin.send_text';if(!taskActions.some(item=>item.id===id)){toast('这个动作已不可用，可先停用或删除任务',true);return;}
    const f=$('#task-form').elements;for(const key of ['id','name','kind','daily_time'])f[key].value=row[key]||'';
    f.action_id.value=id;renderTaskFields(row.params||{chat_id:row.chat_id,content:row.content});
    f.interval_minutes.value=row.interval_seconds?row.interval_seconds/60:5;f.enabled.checked=row.enabled;
    $('#task-form-heading').textContent='编辑任务';$('#task-cancel').classList.remove('hidden');taskKind();f.name.focus();return;
  }
  if(button.dataset.taskAction==='delete'&&!confirm('删除这个定时任务？'))return;
  action(async()=>{
    if(button.dataset.taskAction==='delete')await api('scheduled-tasks/delete',{id:row.id});
    else await api('scheduled-tasks/toggle',{id:row.id,enabled:!row.enabled});
    if($('#task-form').elements.id.value===row.id)resetTaskForm();
    await loadTasks();toast('任务已更新');
  });
};
taskKind();
document.querySelectorAll('[data-page]').forEach(link=>link.addEventListener('click',()=>{if(link.hash===location.hash)navigate();}));
window.addEventListener('hashchange',navigate);navigate();
async function loadRobots(){
  const data=await api('robots');robotRows=data.robots;
  if(!robotRows.some(row=>row.id===robot))robot='default';
  renderState();
}
async function switchRobot(identifier){
  if($('#plugin-dialog').open)$('#plugin-dialog').close();
  resetContacts();
  resetComposer();
  taskRows=[];taskActions=[];taskActionSignature='';taskGeneration++;resetTaskForm();renderTasks();
  messageGeneration++;messages=[];messageCursor=0;messageStream='';messageError='';$('#message-search').value='';messageSignature='';renderMessages();
  robot=identifier;sessionStorage.setItem('helper-robot',robot);
  $('#robot-select').value=robot;
  state=null;config=null;logs=[];logCursor=0;selectedPlugin='';renderLogs();
  renderState();renderAccounts();
  groupNameDraft=new Map(); discoveredGroups=[]; $('#group-ids').value=''; renderGroupNames(); renderDiscoveredGroups();
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
