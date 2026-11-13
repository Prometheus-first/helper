// 目录重启恢复、断线保留、扫描失败与账号切换；不连接真实微信。
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../webui/app.js'), 'utf8');
const code = source.slice(source.indexOf('function contactConnectionIdentity()'), source.indexOf('async function poll(){'));

function fixture(handler) {
  const nodes = new Map();
  const $ = key => {
    if (!nodes.has(key)) nodes.set(key, {value:'',textContent:'',innerHTML:'',disabled:false,
      classList:{toggle(){},add(){},remove(){}},querySelectorAll(){return [];}});
    return nodes.get(key);
  };
  const scope = {$, api:handler, robot:'a',state:{account:'wxid_demo_a',connected:false,pid:1,started_at:1},
    config:{account:'wxid_demo_a'},robotRows:[],busy:false,serviceAvailable:true,
    contactData:null,contactGeneration:0,contactLoading:false,contactError:'',contactIdentity:'',
    contactDetail:'',contactPage:0,contactSignature:'',contactPageSize:50,messageGeneration:0,
    directoryLoadedKey:'',directoryPendingKey:'',directoryGroupsKey:'',directoryGeneration:0,directoryRetryAt:0,
    discoveredGroups:[],document:{activeElement:null},escape:s=>String(s??'').replaceAll('<','&lt;'),
    renderDiscoveredGroups(){},renderState(){}};
  vm.createContext(scope);vm.runInContext(code,scope);
  return {scope,$};
}

function directory(account='wxid_demo_a', name='演示好友') {
  return {account,groups:[{chat_id:'101@chatroom',name:'演示群'}],contacts:{account,
    cached:true,session_id:'',scanned_at:'2026-01-01T12:00:00+08:00',complete:false,
    contacts:[{wxid:'wxid_demo_peer',nickname:name,tag_ids:['7'],tags:['演示'],tags_status:'ready'}],
    tags:[{id:'7',name:'演示'}]}};
}

test('未启动机器人即可恢复联系人和群，搜索可用，扫描仍禁用，读取会去重', async () => {
  const calls=[];
  const {scope,$}=fixture(async route=>{calls.push(route);return directory();});
  await scope.restoreDirectoryCache();await scope.restoreDirectoryCache();
  assert.deepEqual(calls,['directory-cache']);
  assert.equal(scope.discoveredGroups.length,1);
  assert.match($('#contact-list').innerHTML,/演示好友/);
  assert.match($('#contact-status').textContent,/本地缓存/);
  assert.equal($('#contact-search').disabled,false);
  assert.equal($('#scan-contacts').disabled,true);
  $('#contact-search').value='不存在';scope.renderContacts();
  assert.match($('#contact-list').innerHTML,/没有匹配/);
});

test('断线和连接变化保留资料，旧session不用于下次连接', async () => {
  const {scope,$}=fixture(async()=>directory());
  await scope.restoreDirectoryCache();
  scope.state.connected=true;scope.contactIdentity=scope.contactConnectionIdentity();
  scope.contactData.cached=false;scope.contactData.session_id='old-connection';
  scope.state.pid=2;scope.renderContacts();
  assert.equal(scope.contactData.cached,true);
  assert.equal(scope.contactData.session_id,'');
  scope.serviceDisconnected();
  assert.match($('#contact-list').innerHTML,/演示好友/);
  assert.equal($('#contact-search').disabled,false);
});

test('扫描失败保留旧资料，成功扫描整体更新并存储当前session', async () => {
  let failed=true;
  const {scope,$}=fixture(async route=>{
    if(route==='directory-cache')return directory();
    if(route==='send-connection')return {session_id:'session-a'};
    if(failed)throw Error('合成扫描失败');
    return {...directory().contacts,cached:false,session_id:'session-a',contacts:[{wxid:'wxid_updated',nickname:'新名单'}]};
  });
  await scope.restoreDirectoryCache();scope.state.connected=true;scope.renderContacts();
  await scope.scanContacts();
  assert.match($('#contact-list').innerHTML,/演示好友/);
  assert.match($('#contact-status').textContent,/合成扫描失败/);
  failed=false;await scope.scanContacts();
  assert.match($('#contact-list').innerHTML,/新名单/);
  assert.doesNotMatch($('#contact-list').innerHTML,/演示好友/);
  assert.equal(scope.contactData.session_id,'session-a');
  assert.equal(scope.contactData.cached,false);
});

test('切换账号后丢弃前一个账号未完成的缓存响应', async () => {
  let release;const wait=new Promise(resolve=>release=resolve);
  const {scope,$}=fixture(async(_route,_body,robot)=>{
    if(robot==='a'){await wait;return directory();}
    return {...directory('wxid_demo_b','另一个账号的好友'),groups:[{chat_id:'202@chatroom',name:'另一个群'}]};
  });
  const first=scope.restoreDirectoryCache();
  scope.robot='b';scope.state.account='wxid_demo_b';scope.resetContacts();scope.resetDirectoryCache();
  await scope.restoreDirectoryCache();release();await first;
  assert.match($('#contact-list').innerHTML,/另一个账号的好友/);
  assert.doesNotMatch($('#contact-list').innerHTML,/演示好友/);
  assert.equal(scope.discoveredGroups[0].chat_id,'202@chatroom');
});

test('缓存读取比手动扫描慢时，不覆盖刚扫描的新结果', async () => {
  let release;const wait=new Promise(resolve=>release=resolve);
  const {scope,$}=fixture(async route=>{
    if(route==='directory-cache'){await wait;return directory();}
    if(route==='send-connection')return {session_id:'session-a'};
    return {...directory().contacts,session_id:'session-a',contacts:[{wxid:'wxid_new',nickname:'刚扫描的好友'}]};
  });
  const cached=scope.restoreDirectoryCache();scope.state.connected=true;scope.renderContacts();
  await scope.scanContacts();release();await cached;
  assert.match($('#contact-list').innerHTML,/刚扫描的好友/);
  assert.doesNotMatch($('#contact-list').innerHTML,/演示好友/);
});

test('首次缓存恢复和失败扫描交错时，仍能恢复原有文件', async () => {
  let release;const wait=new Promise(resolve=>release=resolve);
  const {scope,$}=fixture(async route=>{
    if(route==='directory-cache'){await wait;return directory();}
    if(route==='send-connection')return {session_id:'session-a'};
    throw Error('合成扫描失败');
  });
  const cached=scope.restoreDirectoryCache();scope.state.connected=true;scope.renderContacts();
  await scope.scanContacts();release();await cached;
  await scope.restoreDirectoryCache();
  assert.match($('#contact-list').innerHTML,/演示好友/);
});
