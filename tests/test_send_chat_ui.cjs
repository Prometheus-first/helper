// 使用模拟账号验证发送会话选择，不连接微信或启动机器人。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../webui/app.js'), 'utf8');
const code = source.slice(source.indexOf('const roleLabels ='), source.indexOf('function renderMessages()'));

function fixture(handler, linux = false) {
  const nodes = new Map();
  const $ = key => {
    if (!nodes.has(key)) {
      const classes = new Set();
      nodes.set(key, {value:'', textContent:'', innerHTML:'', disabled:false,
        classList:{toggle(name, value){if(value)classes.add(name);else classes.delete(name);},
          add(name){classes.add(name);}, remove(name){classes.delete(name);}, contains(name){return classes.has(name);}},
        querySelectorAll(){return [];}});
    }
    return nodes.get(key);
  };
  let request = 0;
  const scope = { $, api:handler, isLinux:linux, state:{connected:true}, serviceAvailable:true, busy:false,
    robot:'a', sendMembers:[], mentionSelection:new Set(), sendSession:'', sendGeneration:0,
    sendLoading:false, pendingSend:null, sendChatsAttempted:false, sendPolling:false,
    pendingEmoticonExport:null,emoticonExportGeneration:0,emoticonExportPolling:false,emoticonExportIdentity:null,emoticonExportFeedback:false,
    setInterval(){}, crypto:{randomUUID:()=> 'fixture-request-' + (++request)},
    escape:s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])) };
  vm.createContext(scope);
  vm.runInContext(code, scope);
  return {scope, $};
}

test('已配置的群和已发现的会话合并去重，优先显示本账号备注', async () => {
  const {scope, $} = fixture(async (endpoint) => {
    if (endpoint === 'chats') return {chats:[{chat_id:'123@chatroom', name:'微信名称'}, {chat_id:'wxid_peer'}]};
    if (endpoint === 'config') return {groups:['123@chatroom','456@chatroom'], group_names:{'123@chatroom':'测试备注'}};
    if (endpoint === 'send-connection') return {session_id:'connection-a'};
    throw Error(endpoint);
  });
  await scope.loadSendChats();
  const html = $('#send-chat').innerHTML;
  assert.match(html, /文件传输助手/);
  assert.match(html, /测试备注 · 123@chatroom/);
  assert.equal((html.match(/value="123@chatroom"/g) || []).length, 1);
  assert.match(html, /456@chatroom/);
  assert.match(html, /wxid_peer/);
});

test('群成员未缓存时仍可发送文字；连接失效时不允许发送', async () => {
  let broken = false;
  const {scope, $} = fixture(async endpoint => {
    if (endpoint === 'send-connection') return {session_id:'connection-a'};
    if (endpoint.startsWith('group-members')) {
      const error = new Error(broken ? '微信尚未连接' : '群成员缓存尚未就绪，请在微信打开该群后重试');
      error.status = 409; throw error;
    }
    throw Error(endpoint);
  });
  $('#send-chat').value = '123@chatroom'; $('#send-content').value = '你好';
  await scope.selectSendChat();
  assert.equal(scope.sendSession, 'connection-a');
  assert.equal($('#send-text').disabled, false);
  assert.match($('#send-status').textContent, /可发送文字/);
  broken = true;
  await scope.selectSendChat();
  assert.equal(scope.sendSession, '');
  assert.equal($('#send-text').disabled, true);
});

test('切换账号后忽略旧账号的会话结果', async () => {
  let release;
  const wait = new Promise(resolve => { release = resolve; });
  const {scope, $} = fixture(async (endpoint, _body, robot) => {
    if (robot === 'a') await wait;
    if (endpoint === 'chats') return {chats:[]};
    if (endpoint === 'config') return {groups:[robot+'@chatroom'], group_names:{}};
    if (endpoint === 'send-connection') return {session_id:'session-'+robot};
    throw Error(endpoint);
  });
  const old = scope.loadSendChats();
  scope.resetComposer(); scope.robot = 'b';
  await scope.loadSendChats();
  release(); await old;
  assert.match($('#send-chat').innerHTML, /b@chatroom/);
  assert.doesNotMatch($('#send-chat').innerHTML, /a@chatroom/);
});


test('Linux 群会话加载成员、搜索群昵称和微信名，并显示未知角色', async () => {
  const calls = [];
  const {scope, $} = fixture(async endpoint => {
    calls.push(endpoint);
    if (endpoint === 'send-connection') return {session_id:'linux-session'};
    if (endpoint.startsWith('group-members?')) return {session_id:'linux-session', members:[
      {wxid:'wxid_peer', nickname:'群里名字', group_nickname:'群里名字', wechat_name:'微信名字', role:'unknown'},
      {wxid:'wxid_owner', nickname:'群主名字', role:'owner'}]};
    throw Error('unexpected ' + endpoint);
  }, true);
  $('#send-chat').value = '123@chatroom';
  $('#send-content').value = 'hello';
  $('#linux-send-kind').value = 'text';
  await scope.selectSendChat();
  assert.deepEqual(calls, ['send-connection','group-members?room=123%40chatroom']);
  assert.equal($('#send-group').classList.contains('hidden'), false);
  assert.match($('#send-members').innerHTML, /群里名字/);
  assert.match($('#send-members').innerHTML, /身份未知/);
  assert.doesNotMatch($('#send-members').innerHTML, /普通群友/);
  $('#member-search').value='微信名字'; scope.renderSendMembers();
  assert.match($('#send-members').innerHTML, /wxid_peer/);
  assert.doesNotMatch($('#send-members').innerHTML, /wxid_owner/);
  assert.equal($('#send-text').disabled, false);
  $('#linux-send-kind').value = 'file';
  $('#linux-send-path').value = '';
  scope.renderSendState();
  assert.equal($('#send-text').disabled, true);
  assert.equal($('#send-group').classList.contains('hidden'), true);
  $('#linux-send-path').value = '/tmp/fixture.txt';
  scope.renderSendState();
  assert.equal($('#send-text').disabled, false);
});

test('Linux 群名单未就绪保留手工艾特，连接失效或名单来自另一会话时拒绝发送', async () => {
  let mode = 'uncached';
  const {scope, $} = fixture(async endpoint => {
    if (endpoint === 'send-connection') return {session_id:'linux-session'};
    if (mode === 'changed') return {session_id:'stale-session',members:[]};
    const error = new Error(mode === 'uncached' ? '群成员缓存尚未就绪，请在微信打开该群后重试' : '微信尚未连接');
    error.status=409;throw error;
  }, true);
  $('#send-chat').value='123@chatroom';$('#linux-send-kind').value='text';$('#send-content').value='hello';
  await scope.selectSendChat();
  $('#linux-mentions').value='wxid_peer';scope.renderSendState();
  assert.equal($('#send-text').disabled,false);
  assert.match($('#send-status').textContent,/填写成员 ID/);
  for (mode of ['disconnected','changed']) {
    await scope.selectSendChat();
    assert.equal(scope.sendSession,'');
    assert.equal($('#send-text').disabled,true);
  }
});

test('Linux 勾选和手工名单合并去重后最多八人，发送请求携带真实 ID', async () => {
  const sent=[];
  const {scope,$}=fixture(async (endpoint,body) => {
    if (endpoint==='send-text'||endpoint==='send-media') {sent.push([endpoint,body]);return {};}
    if (endpoint.startsWith('send-status')) return {status:'sent'};
    throw Error(endpoint);
  },true);
  $('#send-chat').value='123@chatroom';$('#linux-send-kind').value='text';$('#send-content').value='hello';
  scope.sendSession='linux-session';
  for(let i=0;i<7;i++)scope.mentionSelection.add('wxid_'+i);
  $('#linux-mentions').value='wxid_0,wxid_7';
  scope.renderSendState();
  assert.equal(scope.linuxMentionTargets().length,8);
  assert.equal($('#mention-count').textContent,'艾特 8 / 8');
  const ninth={checked:true,dataset:{mention:'wxid_8'},matches:()=>true};
  $('#send-members').onchange({target:ninth});
  assert.equal(ninth.checked,false);
  assert.equal(scope.mentionSelection.has('wxid_8'),false);
  assert.equal($('#send-text').disabled,false);
  await $('#send-form').onsubmit({preventDefault(){}});
  assert.equal(sent[0][0],'send-text');
  assert.deepEqual(Array.from(sent[0][1].at_list),Array.from({length:8},(_,i)=>'wxid_'+i));
  assert.equal(sent[0][1].session_id,'linux-session');
  $('#linux-send-kind').value='file';$('#linux-send-path').value='/tmp/file.txt';scope.renderSendState();
  await $('#send-form').onsubmit({preventDefault(){}});
  assert.equal(sent[1][0],'send-media');
  assert.equal(Object.hasOwn(sent[1][1],'at_list'),false);
});

test('Linux 切换账号后忽略迟到的成员结果并清空两种艾特选择', async () => {
  let release;
  const wait=new Promise(resolve=>{release=resolve;});
  const {scope,$}=fixture(async endpoint=>{
    if(endpoint==='send-connection')return {session_id:'session-a'};
    await wait;return {session_id:'session-a',members:[{wxid:'wxid_old',nickname:'旧账号成员'}]};
  },true);
  $('#send-chat').value='123@chatroom';$('#linux-send-kind').value='text';
  const old=scope.selectSendChat();
  await Promise.resolve();
  scope.mentionSelection.add('wxid_old');$('#linux-mentions').value='wxid_old';
  scope.resetComposer();scope.robot='b';release();await old;
  assert.equal(scope.sendSession,'');assert.equal(scope.sendMembers.length,0);
  assert.equal(scope.mentionSelection.size,0);assert.equal($('#linux-mentions').value,'');
  assert.doesNotMatch($('#send-members').innerHTML,/旧账号成员/);
});

test('Linux 艾特输入仅用于群文字，校验人数和 ID 并在切换时清空', async () => {
  const {scope, $} = fixture(async () => ({session_id:'linux-session'}), true);
  $('#send-chat').value='123@chatroom'; $('#linux-send-kind').value='text';
  $('#send-content').value='hello'; scope.sendSession='linux-session';
  $('#linux-mentions').value='wxid_one， wxid_two';
  assert.deepEqual(Array.from(scope.linuxMentionTargets()), ['wxid_one', 'wxid_two']);
  for (const value of ['wxid_one,wxid_one', 'filehelper', 'gh_account', 'notify@all', '昵称',
      Array.from({length:9}, (_,i) => 'wxid_'+i).join(',')]) {
    $('#linux-mentions').value=value;
    assert.throws(() => scope.linuxMentionTargets());
    scope.renderSendState(); assert.equal($('#send-text').disabled,true);
  }
  $('#linux-mentions').value='wxid_one'; $('#linux-send-kind').value='file';
  assert.deepEqual(Array.from(scope.linuxMentionTargets()), []);
  $('#linux-send-kind').value='text'; $('#send-chat').value='filehelper';
  assert.deepEqual(Array.from(scope.linuxMentionTargets()), []);
  await scope.selectSendChat(); assert.equal($('#linux-mentions').value,'');
  $('#linux-mentions').value='wxid_one'; scope.resetComposer();
  assert.equal($('#linux-mentions').value,'');
});

test('Linux 语音沿用发送编号和连接，路径提示明确且不会带艾特', async () => {
  const sent=[];
  const {scope,$}=fixture(async (endpoint,body)=>{
    if(endpoint==='send-media'){sent.push(body);return {};}
    if(endpoint.startsWith('send-status'))return {status:'sent'};
    throw Error(endpoint);
  },true);
  scope.sendSession='voice-session';
  $('#send-chat').value='123@chatroom';$('#linux-send-kind').value='voice';
  $('#send-content').value='old text';$('#linux-mentions').value='wxid_peer';scope.mentionSelection.add('wxid_peer');
  scope.renderLinuxSendKind();
  assert.equal($('#send-content').required,false);
  assert.equal($('#linux-send-path').required,true);
  assert.equal($('#send-content').classList.contains('hidden'),true);
  assert.equal($('#send-group').classList.contains('hidden'),true);
  assert.equal($('#send-text').disabled,true);
  assert.match($('#linux-send-path').placeholder,/voice\.wav$/);
  assert.match($('#linux-path-hint').textContent,/SILK.*WAV.*60/);
  $('#linux-send-path').value='/tmp/voice.silk';scope.renderSendState();
  assert.equal($('#send-text').disabled,false);
  await $('#send-form').onsubmit({preventDefault(){}});
  assert.equal(sent.length,1);
  assert.equal(sent[0].kind,'voice');assert.equal(sent[0].path,'/tmp/voice.silk');
  assert.equal(sent[0].session_id,'voice-session');assert.equal(sent[0].chat_id,'123@chatroom');
  assert.match(sent[0].request_id,/^fixture-request-/);
  assert.equal(Object.hasOwn(sent[0],'at_list'),false);
  assert.equal(Object.hasOwn(sent[0],'content'),false);
  $('#linux-send-kind').value='text';scope.renderLinuxSendKind();
  assert.equal($('#send-content').required,true);assert.equal($('#linux-send-path').required,false);
  assert.equal($('#linux-path-field').classList.contains('hidden'),true);
});

test('Linux 初始化保留已有 Fish TTS 设置表单和语音类型', () => {
  const {scope,$}=fixture(async()=>({}),true);
  scope.document={body:{dataset:{}}};
  $('#accounts-heading').firstChild={textContent:''};
  $('[data-page="models"]').lastChild={textContent:''};
  $('#tts-form').classList.add('hidden');
  const start=source.indexOf("if(isLinux){\n  document.body.dataset.platform='linux';");
  assert.ok(start>=0);
  vm.runInContext(source.slice(start,source.indexOf('(async()=>{renderState();',start)),scope);
  assert.equal($('#tts-form').classList.contains('hidden'),false);
  assert.equal($('#linux-send-kind').onchange,scope.renderLinuxSendKind);
  const html=fs.readFileSync(path.join(__dirname,'../webui/index.html'),'utf8');
  assert.match(html,/<option value="voice">语音（WAV \/ SILK）<\/option>/);
});

test('Linux 表情导出使用独立后台任务并显示本机路径，错误内容转义', async () => {
  const calls=[];
  const {scope,$}=fixture(async(endpoint,body)=>{
    calls.push([endpoint,body]);
    if(endpoint==='send-connection')return {session_id:'export-session'};
    if(endpoint==='emoticons-export')return {status:'exporting'};
    if(endpoint.startsWith('emoticons-export-status'))return {status:'done',result:{count:2,sendable_count:1,
      output_directories:['/tmp/export<fixture>'],items:[{number:1,format:'GIF',gif_path:'/tmp/test.gif',error:''},
        {number:2,error:'<script>fixture</script>'}]}};
    throw Error(endpoint);
  },true);
  scope.state={connected:true,account:'wxid_fixture',capabilities:['emoticon.extract']};
  $('#emoticon-export-convert').checked=false;
  scope.renderEmoticonExport();
  assert.equal($('#export-emoticons').disabled,false);
  await scope.startEmoticonExport();
  assert.equal(calls.find(row=>row[0]==='emoticons-export')[1].convert,false);
  assert.equal(calls.find(row=>row[0]==='emoticons-export')[1].session_id,'export-session');
  assert.match($('#emoticon-export-status').textContent,/导出 2 项.*1 项/);
  assert.match($('#emoticon-export-results').innerHTML,/export&lt;fixture&gt;/);
  assert.doesNotMatch($('#emoticon-export-results').innerHTML,/<script>/);
  assert.equal(calls.some(row=>row[0].startsWith('send-media')),false);
});

test('Linux 表情导出切换账号后忽略旧任务结果，未开放能力时禁用', async () => {
  let resolveStatus;
  const {scope,$}=fixture(async(endpoint)=>{
    if(endpoint==='send-connection')return {session_id:'export-session'};
    if(endpoint==='emoticons-export')return {status:'exporting'};
    if(endpoint.startsWith('emoticons-export-status'))return new Promise(resolve=>{resolveStatus=resolve;});
    throw Error(endpoint);
  },true);
  scope.renderEmoticonExport();assert.equal($('#export-emoticons').disabled,true);
  scope.state={connected:true,account:'wxid_fixture',capabilities:['emoticon.extract']};
  scope.renderEmoticonExport();
  const sending=scope.startEmoticonExport();
  while(!resolveStatus)await Promise.resolve();
  scope.resetComposer();scope.robot='b';
  resolveStatus({status:'done',result:{count:1,sendable_count:1,items:[{number:1,gif_path:'private-old-account-path'}]}});
  await sending;
  assert.equal($('#emoticon-export-results').innerHTML,'');
  assert.doesNotMatch($('#emoticon-export-status').textContent,/导出 1 项/);
});

test('Linux 表情导出失败与空缓存结果在刷新按钮状态后仍可见', async () => {
  let fail=true;
  const {scope,$}=fixture(async(endpoint)=>{
    if(endpoint==='send-connection')return {session_id:'export-session'};
    if(endpoint==='emoticons-export') {
      if(fail)throw Object.assign(Error('任务接口不存在'),{status:404});
      return {status:'exporting'};
    }
    if(endpoint.startsWith('emoticons-export-status'))return {status:'done',result:{count:0,sendable_count:0,items:[],output_directories:[]}};
    throw Error(endpoint);
  },true);
  scope.state={connected:true,account:'wxid_fixture',capabilities:['emoticon.extract']};
  scope.renderEmoticonExport();
  await scope.startEmoticonExport();
  scope.renderEmoticonExport();
  assert.equal($('#emoticon-export-status').textContent,'任务接口不存在');
  fail=false;
  await scope.startEmoticonExport();
  scope.renderEmoticonExport();
  assert.match($('#emoticon-export-status').textContent,/导出 0 项/);
  scope.resetEmoticonExport();scope.renderEmoticonExport();
  assert.match($('#emoticon-export-status').textContent,/仅导出已缓存/);
});
