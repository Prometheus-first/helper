// 使用合成账号验证 Linux 联系人页面，不启动微信。
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../webui/app.js'), 'utf8');
const code = source.slice(source.indexOf('function contactConnectionIdentity()'), source.indexOf("$('#scan-contacts').onclick=scanContacts;"));

function fixture(api) {
  const nodes = new Map();
  const $ = key => {
    if (!nodes.has(key)) nodes.set(key, {value:'', textContent:'', innerHTML:'', disabled:false,
      classList:{toggle(){},add(){},remove(){}}, querySelectorAll(){return []}});
    return nodes.get(key);
  };
  const scope = {$, api, isLinux:true, state:{connected:true,account:'wxid_self_fixture',pid:42,started_at:1},
    serviceAvailable:true,busy:false,robot:'a',contactIdentity:'',contactData:null,contactLoading:false,
    contactError:'',contactGeneration:0,contactDetail:'',contactPage:0,contactPageSize:50,contactSignature:'',
    document:{activeElement:{dataset:{}}},
    escape:s=>String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))};
  vm.createContext(scope);vm.runInContext(code,scope);
  return {scope,$};
}

test('Linux 联系人扫描展示备注、微信 ID 和标签并转义内容', async () => {
  const {scope,$}=fixture(async endpoint => {
    if(endpoint==='send-connection') return {session_id:'s1'};
    if(endpoint==='contacts') return {session_id:'s1',account:'wxid_self_fixture',complete:false,
      scanned_at:'2026-01-01T00:00:00Z',tags:[{id:'7',name:'好友'}],
      contacts:[{wxid:'wxid_friend_fixture',remark:'<备注>',nickname:'昵称',alias:'',tag_ids:['7'],tags:['好友'],tags_status:'ready'}]};
    throw Error(endpoint);
  });
  await scope.scanContacts();
  assert.match($('#contact-list').innerHTML,/&lt;备注&gt;/);
  assert.match($('#contact-list').innerHTML,/wxid_friend_fixture/);
  assert.match($('#contact-list').innerHTML,/好友/);
  assert.match($('#contact-status').textContent,/可能尚未同步完整/);
  assert.match($('#contact-tag').innerHTML,/好友/);
});

test('账号切换时忽略旧账号扫描结果',async()=>{
  let release;const wait=new Promise(resolve=>{release=resolve;});
  const {scope}=fixture(async endpoint=>{
    if(endpoint==='send-connection')return {session_id:'s1'};
    if(endpoint==='contacts'){await wait;return {session_id:'s1',account:'wxid_self_fixture',contacts:[],tags:[]};}
    throw Error(endpoint);
  });
  const pending=scope.scanContacts();await Promise.resolve();
  scope.robot='b';scope.resetContacts();release();await pending;
  assert.equal(scope.contactData,null);
});

test('Linux 导航不再隐藏联系人页面',()=>{
  const navigation=source.slice(source.indexOf('function navigate()'),source.indexOf('function renderRobotOptions()'));
  assert.doesNotMatch(navigation,/\['tasks','contacts','moments','articles'\]/);
  assert.match(source,/for\(const page of \['tasks','moments','articles'\]\)/);
});
