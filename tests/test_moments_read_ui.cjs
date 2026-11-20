const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function node() {
  return {value:'', textContent:'', dataset:{}, options:[], children:[], disabled:false,
    classList:{toggle(){}, add(){}, remove(){}, contains(){return false;}},
    replaceChildren(...children){this.children=children;},
    append(...children){this.children.push(...children);},
    setAttribute(){}, close(){this.open=false;}, showModal(){this.open=true;}};
}

async function read(result) {
  const nodes = new Map(), calls = [];
  const $ = selector => {
    if (!nodes.has(selector)) nodes.set(selector,node());
    return nodes.get(selector);
  };
  const scope = {window:{addEventListener(){}},document:{querySelector:$,
    createElement:node,createTextNode:text=>({textContent:text})},TextEncoder,
    setTimeout,clearTimeout};
  vm.createContext(scope);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../webui/moments.js'),'utf8'),scope);
  const composer = scope.window.createMomentsComposer({
    api:async route=>{
      calls.push(route);
      if(route==='moments-connection')return {supported:true,session_id:'fixture'};
      return {session_id:'fixture',...result};
    },robots:()=>[{id:'example',state:'running',connected:true}],
    label:()=> 'Fixture',available:()=>true});
  composer.render();
  await new Promise(resolve=>setImmediate(resolve));
  await $('#moments-read').onclick();
  assert.equal(calls.filter(route=>route==='moments-read').length,1);
  return $('#moments-read-status').textContent;
}

const row = {sns_id:'123',author:'wxid_fixture',nickname:'Fixture',timestamp:1700000000,
  time:'2023-11-15T06:13:20+08:00',content:'Synthetic',media_count:0};

test('服务端首屏标明本次来源，读取按钮只请求一次',async()=>{
  const status = await read({source:'native_sns_first_page',rows:[row],loaded_count:10,truncated:true});
  assert.match(status,/已从微信服务器读取 1 条/);
  assert.match(status,/本次首页共 10 条/);
  assert.doesNotMatch(status,/缓存/);
});

test('旧版本缓存标明缓存，空服务端结果给出本次返回状态',async()=>{
  const cached = await read({source:'loaded_pc_moments_cache',rows:[row],loaded_count:1,truncated:false});
  assert.equal(cached,'已读取缓存 1 条。');
  const empty = await read({source:'native_sns_first_page',rows:[],loaded_count:0,truncated:false});
  assert.equal(empty,'微信服务器本次未返回记录。');
});
