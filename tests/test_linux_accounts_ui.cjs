// Linux selector displays only sessions returned by live-account discovery.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../webui/app.js'), 'utf8');
const code = source.slice(source.indexOf('function renderRobotOptions()'), source.indexOf('function renderState()'));

test('隐藏未登录账号，等待登录的会话仍显示', () => {
  const node = {innerHTML:'', value:''};
  const scope = {isLinux:true, robot:'live', robotRows:[
    {id:'old',account:'wxid_old_fixture',name:'old'},
    {id:'live',account:'wxid_live_fixture',name:'live'},
    {id:'pending',account:'',name:'pending'}],
    loggedAccounts:[{robot_id:'live',wxid:'wxid_live_fixture'},{robot_id:'pending',wxid:''}],
    state:null,serviceAvailable:true,robotOptionsSignature:'',
    profileLabel:row=>row.name, accountKey:row=>row.robot_id,
    escape:s=>String(s ?? ''), $:()=>node};
  vm.createContext(scope);vm.runInContext(code,scope);
  scope.renderRobotOptions();
  assert.match(node.innerHTML,/live/);
  assert.match(node.innerHTML,/pending/);
  assert.doesNotMatch(node.innerHTML,/old/);
  scope.loggedAccounts=[];scope.robotOptionsSignature='';scope.renderRobotOptions();
  assert.match(node.innerHTML,/未选择账号/);
  assert.doesNotMatch(node.innerHTML,/>old|>live|>pending/);
});
