const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture(handler) {
  const nodes = new Map(), calls = [];
  const $ = key => {
    if (!nodes.has(key)) nodes.set(key, {value:'', checked:false, disabled:false, textContent:'',
      classList:{contains:()=>false, toggle(){}}});
    return nodes.get(key);
  };
  let selected = {robot:'robot_one', session:'session_one', supported:true};
  let now = 20000;
  const scope = {window:{},document:{querySelector:$},Date:{now:()=>now}};
  vm.createContext(scope);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../webui/moments-auto.js'),'utf8'),scope);
  const form = scope.window.createMomentsAutoComment({available:()=>true,current:()=>selected,
    api:async (...args)=>{calls.push(args); return handler(...args);}});
  return {$,calls,form,select:value=>{selected=value;},advance:()=>{now+=10001;}};
}
const settings = {enabled:false,content:'固定评论',interval_seconds:120,authors:[],phase:'disabled',completed:0};

test('加载配置不自动启用或发送评论，固定设置绑定当前账号和连接',async()=>{
  const f=fixture(async(route,body)=>body ? {...body,phase:'waiting'} : settings);
  f.form.render(); await tick();
  assert.equal(f.$('#moments-auto-enabled').checked,false);
  f.$('#moments-auto-enabled').checked=true;
  f.$('#moments-auto-content').value='这条很棒 👍';
  f.$('#moments-auto-interval').value='60';
  f.$('#moments-auto-authors').value='wxid_fixture_one\nwxid_fixture_two';
  await f.$('#moments-auto-form').onsubmit({preventDefault(){}});
  const post=f.calls.find(call=>call[1]);
  assert.equal(post[0],'moments-auto-comment');
  assert.equal(post[2],'robot_one');
  assert.equal(post[1].session_id,'session_one');
  assert.equal(post[1].content,'这条很棒 👍');
  assert.deepEqual(Array.from(post[1].authors),['wxid_fixture_one','wxid_fixture_two']);
  assert.ok(f.calls.every(call=>call[0]==='moments-auto-comment'));
});

test('切换机器人丢弃旧账号迟到的设置',async()=>{
  let release;
  const f=fixture(()=>new Promise(resolve=>{release=resolve;}));
  f.form.render();
  f.select({robot:'robot_two',session:'session_two',supported:true});
  f.form.render();
  release({...settings,enabled:true,content:'旧账号设置'}); await tick();
  assert.equal(f.$('#moments-auto-content').value,'');
  assert.equal(f.$('#moments-auto-enabled').checked,false);
});

test('后台状态刷新保留未保存文案，结果未知时显示暂停原因',async()=>{
  let result=settings;
  const f=fixture(async()=>result);
  f.form.render(); await tick();
  f.$('#moments-auto-content').value='正在编辑'; f.$('#moments-auto-content').oninput();
  result={...settings,phase:'paused',error:'结果未确认，已暂停'};
  f.advance(); f.form.render(); await tick();
  assert.equal(f.$('#moments-auto-content').value,'正在编辑');
  assert.match(f.$('#moments-auto-status').textContent,/结果未确认/);
});

test('不支持评论的连接不能启用，已启用配置仍可关闭',async()=>{
  const f=fixture(async()=>settings);
  f.select({robot:'robot_one',session:'session_one',supported:false});
  f.form.render(); await tick();
  assert.equal(f.$('#moments-auto-enabled').disabled,true);
  f.$('#moments-auto-enabled').checked=true;
  await f.$('#moments-auto-form').onsubmit({preventDefault(){}});
  assert.equal(f.calls.filter(call=>call[1]).length,0);
});
