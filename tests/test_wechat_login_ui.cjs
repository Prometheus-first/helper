// 使用虚拟接口验证登录入口；不启动微信。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../webui/app.js'), 'utf8');
const code = source.slice(source.indexOf('async function closeWechatLogin()'), source.indexOf('function fill(form, values)'));

function fixture(api) {
  const nodes = new Map();
  const $ = key => {
    if (!nodes.has(key)) {
      const classes = new Set(['hidden']);
      nodes.set(key, {textContent:'', disabled:false, src:'',
        classList:{add:x=>classes.add(x), remove:x=>classes.delete(x), contains:x=>classes.has(x)},
        removeAttribute(name){if(name==='src')this.src='';}});
    }
    return nodes.get(key);
  };
  const scope = {$, api, loginAttempt:'', loginGeneration:0, loginTimer:null, loginStarting:false,
    setTimeout:()=>1, clearTimeout:()=>{}, loadRobots:async()=>{}, loadAccounts:async()=>{}};
  vm.createContext(scope); vm.runInContext(code, scope);
  return {scope, $};
}

test('启动后显示接口返回的二维码，关闭只取消读取', async () => {
  const calls=[];
  const {scope,$}=fixture(async (route,body)=>{
    calls.push([route,body]);
    if(route==='wechat-login/start')return {attempt_id:'a'.repeat(32),pid:7};
    if(route.startsWith('wechat-login/status'))return {status:'qr_available',qrcode_base64:'cG5n'};
    if(route==='wechat-login/cancel')return {ok:true};
    throw Error(route);
  });
  await $('#login-wechat').onclick();
  assert.equal($('#wechat-login-qr').src,'data:image/png;base64,cG5n');
  assert.equal($('#wechat-login-qr').classList.contains('hidden'),false);
  await scope.closeWechatLogin();
  assert.equal($('#wechat-login-qr').src,'');
  assert.equal($('#wechat-login-panel').classList.contains('hidden'),true);
  assert.equal(calls.at(-1)[0],'wechat-login/cancel');
});

test('页面关闭后返回的旧启动结果不会显示二维码', async () => {
  let resolve;const calls=[];
  const {scope,$}=fixture(async(route,body)=>{
    calls.push([route,body]);
    if(route==='wechat-login/start')return await new Promise(done=>{resolve=done;});
    if(route==='wechat-login/cancel')return {ok:true};
    throw Error(route);
  });
  const pending=$('#login-wechat').onclick();
  await scope.closeWechatLogin();resolve({attempt_id:'b'.repeat(32)});await pending;
  assert.equal($('#wechat-login-qr').src,'');
  assert.equal(calls.at(-1)[0],'wechat-login/cancel');
});
