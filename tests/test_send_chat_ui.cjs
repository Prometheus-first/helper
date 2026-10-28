// 使用模拟账号验证发送会话选择，不连接微信或启动机器人。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../webui/app.js'), 'utf8');
const code = source.slice(source.indexOf('const roleLabels ='), source.indexOf("$('#refresh-send-chats').onclick=loadSendChats;"));

function fixture(handler) {
  const nodes = new Map();
  const $ = key => {
    if (!nodes.has(key)) nodes.set(key, {value:'', textContent:'', innerHTML:'', disabled:false,
      classList:{toggle(){}, add(){}, remove(){}}, querySelectorAll(){return [];}});
    return nodes.get(key);
  };
  const scope = { $, api:handler, state:{connected:true}, serviceAvailable:true, busy:false,
    robot:'a', sendMembers:[], mentionSelection:new Set(), sendSession:'', sendGeneration:0,
    sendLoading:false, pendingSend:null, sendChatsAttempted:false,
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
