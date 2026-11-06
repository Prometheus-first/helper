// Linux 定时任务入口与写入账号校验；不连接真实微信。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../webui/app.js'), 'utf8');

test('Linux 定时任务页面可打开', () => {
  const code = source.slice(source.indexOf('function navigate()'), source.indexOf('function renderRobotOptions()'));
  let loaded = 0, hidden = null;
  const taskPage = {id:'page-tasks', classList:{toggle:(name, value)=>{hidden=value;}}};
  const document = {querySelectorAll:selector=>selector==='.page'?[taskPage]:[],
    body:{classList:{contains:()=>false}}, title:''};
  const context = {isLinux:true, pages:{overview:'运行概览',tasks:'定时任务'},
    location:{hash:'#tasks'}, document, window:{scrollTo:()=>{}},
    $:()=>({textContent:''}), moments:{render:()=>{}}, articles:{render:()=>{}},
    state:null,sendChatsAttempted:false,sendLoading:false,loadTasks:()=>{loaded++;}};
  vm.createContext(context); vm.runInContext(code, context); context.navigate();
  assert.equal(hidden,false); assert.equal(loaded,1); assert.equal(document.title,'定时任务 · helper');
});

test('Linux 定时任务写入携带当前账号，Windows 不受影响', async () => {
  const code = source.slice(source.indexOf('async function api('), source.indexOf('const moments ='));
  const requests=[];
  const context = {isLinux:true, robot:'default', config:{account:'wxid_fixture'}, token:'fixture',
    fetch:async (url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({ok:true})};}};
  vm.createContext(context); vm.runInContext(code, context);
  for (const route of ['scheduled-tasks','scheduled-tasks/delete','scheduled-tasks/toggle']) {
    await context.api(route,{id:'fixture'});
    assert.equal(JSON.parse(requests.at(-1).options.body).expected_account,'wxid_fixture');
  }
  context.isLinux=false; await context.api('scheduled-tasks',{id:'fixture'});
  assert.equal(JSON.parse(requests.at(-1).options.body).expected_account,undefined);
});
