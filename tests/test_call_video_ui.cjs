const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function element(){
  const classes=new Set();
  return {value:'',textContent:'',disabled:false,children:[],
    classList:{add:value=>classes.add(value),remove:value=>classes.delete(value),
      toggle(value,on){if(on)classes.add(value);else classes.delete(value);},contains:value=>classes.has(value)},
    append(...children){this.children.push(...children);},replaceChildren(...children){this.children=children;},
    setAttribute(name,value){this[name]=value;},removeAttribute(name){delete this[name];}};
}
function state(account, enabled=false){
  return {account,session_id:'session-'+account,connected:true,supported:true,enabled,
    phase:enabled?'streaming':'stopped',photo_id:'image-'+account,
    photos:[{id:'image-'+account,name:'Synthetic image',kind:'image',thumbnail:'data:image/jpeg;base64,AA=='}],
    settings:{python_executable:'python-'+account,repository:'repo-'+account,mode:'fast'}};
}
function fixture(handler,hash='#call-video'){
  const nodes=new Map(),calls=[],timers=[];
  const $=key=>{if(!nodes.has(key))nodes.set(key,element());return nodes.get(key);};
  const scope={window:{},document:{hidden:false,querySelector:$,createElement:element},
    location:{hash},setInterval:callback=>{timers.push(callback);return timers.length;},Date};
  vm.createContext(scope);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../webui/call-video.js'),'utf8'),scope);
  const panel=scope.window.createCallVideoWorkspace({available:()=>true,api:async(...args)=>{
    calls.push(args);return handler(...args);}});
  return {$,calls,panel,timers};
}

test('视频页面只加载当前账号设置，点击开始才提交绑定连接的请求',async()=>{
  const f=fixture(async(route,body)=>route==='call-video'?state('account_a'):{ok:true});
  f.panel.setTarget('robot_a','account_a');await tick();
  assert.ok(f.calls.every(call=>call[0]==='call-video'));
  await f.$('#video-start').onclick();
  const request=f.calls.find(call=>call[0]==='call-video/start');
  assert.equal(request[2],'robot_a');
  assert.equal(request[1].session_id,'session-account_a');
  assert.equal(request[1].photo_id,'image-account_a');
});

test('切换账号丢弃旧账号迟到的素材及环境设置',async()=>{
  let release;
  const f=fixture((route,body,robot)=>robot==='robot_a'?new Promise(resolve=>{release=resolve;}):state('account_b'));
  f.panel.setTarget('robot_a','account_a');
  f.panel.setTarget('robot_b','account_b');await tick();
  release(state('account_a'));await tick();
  assert.equal(f.$('#video-python').value,'python-account_b');
  assert.equal(f.$('#video-start').disabled,false);
});

test('旧账号启动尚未返回时，新账号仍能操作且不继承旧任务',async()=>{
  let release;
  const f=fixture((route,body,robot)=>route==='call-video/start'?new Promise(resolve=>{release=resolve;}):
    state(robot==='robot_a'?'account_a':'account_b'));
  f.panel.setTarget('robot_a','account_a');await tick();
  const pending=f.$('#video-start').onclick();
  assert.equal(f.$('#video-start').disabled,true);
  f.panel.setTarget('robot_b','account_b');await tick();
  assert.equal(f.$('#video-start').disabled,false);
  release({ok:true});await pending;
  assert.equal(f.calls.filter(call=>call[0]==='call-video/start').length,1);
  assert.equal(f.$('#video-python').value,'python-account_b');
});

test('旧连接的迟到预览不能显示到新账号',async()=>{
  let release;
  const f=fixture((route,body,robot)=>route==='call-video/preview'?new Promise(resolve=>{release=resolve;}):
    state(robot==='robot_a'?'account_a':'account_b',robot==='robot_a'));
  f.panel.setTarget('robot_a','account_a');await tick();
  const preview=f.timers[1]();
  f.panel.setTarget('robot_b','account_b');await tick();
  release({image:'data:image/jpeg;base64,OLD',sequence:1});await preview;
  assert.equal(f.$('#video-preview').src,undefined);
});

test('未进入视频页面时不读取素材、摄像头或预览',async()=>{
  const f=fixture(async()=>state('account_a'),'#overview');
  f.panel.setTarget('robot_a','account_a');
  for(const timer of f.timers)await timer();
  assert.equal(f.calls.length,0);
});
