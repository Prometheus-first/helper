// 使用合成文章和虚拟 DOM 验证只读工作区；不连接微信或请求真实图片。
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../webui/articles.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../webui/index.html'), 'utf8');
const app = fs.readFileSync(path.join(__dirname, '../webui/app.js'), 'utf8');
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };
const settle = () => new Promise(setImmediate);
const publisher = {username:'gh_sample', nickname:'示例刊物', alias:'sample_publication'};
const record = (id = 'one', extra = {}) => ({id, title:'合成文章 ' + id, publisher, published_at:'2026-01-02',
  url:'https://mp.weixin.qq.com/s/synthetic-' + id, has_content:true, ...extra});
const content = (id = 'one', extra = {}) => ({...record(id), markdown:'# 标题\n\n合成正文。', images:[], ...extra});
const syncPage = (account, articles, extra = {}) => ({account, publisher, articles, has_more:false, continuation:null,
  complete_remote_history:true, limit_reached:false, page_count:1, fetched_count:new Set(articles.map(row=>row.id)).size, ...extra});

function fixture(handler) {
  const nodes = new Map(), calls = [], downloads = [], blobs = [], revoked = [];
  class Element {
    constructor(tag = 'div') {
      this.tagName = tag.toUpperCase(); this.children = []; this.attributes = new Map(); this._text = '';
      this.value = ''; this.disabled = false; this.readOnly = false; this.parentNode = null; this.className = '';
      const classes = new Set();
      this.classList = {add:name=>classes.add(name), remove:name=>classes.delete(name), contains:name=>classes.has(name),
        toggle(name, force) { const set = force === undefined ? !classes.has(name) : !!force; set ? classes.add(name) : classes.delete(name); return set; }};
    }
    set textContent(value) { this._text = String(value ?? ''); this.children = []; }
    get textContent() { return this._text + this.children.map(child=>child.textContent).join(''); }
    set innerHTML(_value) { throw Error('正文禁止使用 innerHTML'); }
    append(...children) {
      for (let child of children) {
        if (typeof child === 'string') child = new Text(child);
        child.parentNode = this; this.children.push(child);
      }
    }
    replaceChildren(...children) { this.children = []; this._text = ''; this.append(...children); }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name); }
    removeAttribute(name) { this.attributes.delete(name); if (name === 'href') delete this.href; }
    querySelectorAll(tag) { return this.children.flatMap(child=>[...(child.tagName === tag.toUpperCase() ? [child] : []), ...child.querySelectorAll(tag)]); }
    click() { if (this.tagName === 'A') downloads.push({href:this.href, filename:this.download}); else return this.onclick?.(); }
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child=>child !== this); }
  }
  class Text extends Element { constructor(text) { super('#text'); this.textContent = text; } }
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) nodes.set('#' + match[1], new Element());
  const $ = selector => { assert.ok(nodes.has(selector), '节点必须存在于 HTML: ' + selector); return nodes.get(selector); };
  $('#page-articles').classList.add('hidden');
  class BrowserURL extends URL {}
  BrowserURL.createObjectURL = blob => { blobs.push(blob); return 'blob:synthetic-' + blobs.length; };
  BrowserURL.revokeObjectURL = url => revoked.push(url);
  let online = true, now = Date.now();
  class Clock extends Date { static now() { return now; } }
  const scope = {window:{}, document:{querySelector:$, createElement:tag=>new Element(tag), createTextNode:text=>new Text(text), body:new Element('body')},
    URL:BrowserURL, Blob, Date:Clock, setTimeout:fn=>{ fn(); return 1; }};
  vm.createContext(scope); vm.runInContext(source, scope);
  const workspace = scope.window.createArticlesWorkspace({available:()=>online, api:async(route, body, robot)=>{
    const call = {route, body:body === undefined ? undefined : JSON.parse(JSON.stringify(body)), robot}; calls.push(call);
    return handler(call);
  }});
  return {$, workspace, calls, downloads, blobs, revoked, setOnline:value=>{online = value;}, advance:ms=>{now += ms;}};
}

function basicHandler({route, body, robot}) {
  const account = 'wxid_' + robot;
  if (route === 'articles-connection') return {account, connected:true, supported:true};
  if (route.startsWith('articles-publishers?')) return {account, publishers:[], source:'wechat_contacts', complete:false};
  if (route.startsWith('articles?')) return {account, articles:[record()]};
  if (route.startsWith('article?')) return {account, article:content(new URL('http://local/?' + route.split('?')[1]).searchParams.get('id'))};
  if (route === 'articles-read') return syncPage(account, [record('two')]);
  if (route === 'article-read') return {account, article:content('one', {markdown:'# 从微信读取\n\n合成正文。'})};
  if (route === 'article-export') return {account, filename:'合成文章.md', markdown:content(body.id).markdown};
  if (route === 'article-media') return {account, mime:'image/png', data_base64:'cG5n'};
  throw Error('意外接口: ' + route);
}
async function start(f, robot = 'a', account = 'wxid_' + robot, enter = true) {
  f.workspace.setTarget(robot, account, '合成账号 ' + robot); await f.workspace.refresh();
  if (enter) f.$('#articles-publisher-list').querySelectorAll('button')[0]?.onclick();
}
const listButton = (f, index = 0) => f.$('#articles-list').children[index].children[0];
const publisherButtons = f => f.$('#articles-publisher-list').querySelectorAll('button');
const secondPublisher = {username:'gh_second', nickname:'第二刊物', alias:'second_publication'};
const groupedHandler = call => call.route.startsWith('articles?') ? {account:'wxid_a', articles:[
  record('first', {has_content:false}), record('second'), record('other', {publisher:secondPublisher})]} : basicHandler(call);
const followedResult = (account, publishers) => ({account, publishers, source:'wechat_contacts', complete:false});

test('自动读取已关注公众号，零篇文章的公众号也显示待同步，不预取正文或其他文章列表', async () => {
  const f = fixture(call=>call.route.startsWith('articles-publishers?') ? followedResult('wxid_a', [publisher, secondPublisher]) :
    call.route.startsWith('articles?') ? {account:'wxid_a', articles:[]} : basicHandler(call));
  await start(f, 'a', 'wxid_a', false);
  assert.equal(publisherButtons(f).length, 2);
  assert.match(f.$('#articles-publisher-list').textContent, /已关注/);
  assert.match(f.$('#articles-publisher-list').textContent, /待同步/);
  assert.match(f.$('#articles-publishers-status').textContent, /2 个已关注公众号/);
  assert.match(f.$('#articles-publishers-status').textContent, /可能尚未完整加载/);
  assert.equal(f.calls.filter(call=>call.route.startsWith('articles-publishers?')).length, 1);
  assert.equal(f.calls.some(call=>call.route === 'articles-read' || call.route === 'article-read'), false);
  f.$('#page-articles').classList.remove('hidden'); f.advance(9000); f.workspace.render(); await settle();
  assert.equal(f.calls.filter(call=>call.route.startsWith('articles-publishers?')).length, 1);
});

test('同名的已关注公众号保持独立，关注列表账号不匹配时不接纳数据', async () => {
  const f = fixture(call=>call.route.startsWith('articles-publishers?') ? followedResult('wxid_a', [publisher,
    {...secondPublisher, nickname:publisher.nickname}]) : call.route.startsWith('articles?') ? {account:'wxid_a', articles:[]} : basicHandler(call));
  await start(f, 'a', 'wxid_a', false);
  assert.equal(publisherButtons(f).length, 2);
  assert.equal(f.$('#articles-publishers').children.length, 2);
  const mismatch = fixture(call=>call.route.startsWith('articles-publishers?') ? followedResult('wxid_other', [secondPublisher]) : basicHandler(call));
  await start(mismatch, 'a', 'wxid_a', false);
  assert.doesNotMatch(mismatch.$('#articles-publisher-list').textContent, /第二刊物/);
  assert.match(mismatch.$('#articles-publishers-status').textContent, /账号已变化/);
});

test('点击已关注公众号按它的 gh 标识自动分页同步，不获取其他公众号', async () => {
  const f = fixture(call=> {
    if (call.route.startsWith('articles-publishers?')) return followedResult('wxid_a', [publisher, secondPublisher]);
    if (call.route.startsWith('articles?')) return {account:'wxid_a', articles:[]};
    if (call.route === 'articles-read') {
      assert.equal(call.body.publisher, secondPublisher.username);
      return syncPage('wxid_a', [record(call.body.continuation ? 'next' : 'first', {publisher:secondPublisher})], {
        publisher:secondPublisher, page_count:call.body.continuation ? 2 : 1,
        fetched_count:call.body.continuation ? 2 : 1, has_more:!call.body.continuation,
        complete_remote_history:!!call.body.continuation, continuation:call.body.continuation ? null : 'only-second-page'});
    }
    return basicHandler(call);
  });
  await start(f, 'a', 'wxid_a', false);
  await publisherButtons(f).find(button=>button.children[0].textContent === secondPublisher.nickname).onclick();
  assert.equal(f.$('#articles-current-publisher').textContent, secondPublisher.nickname);
  assert.equal(f.calls.filter(call=>call.route === 'articles-read').length, 2);
  assert.match(f.$('#articles-list').textContent, /first/); assert.match(f.$('#articles-list').textContent, /next/);
  assert.match(f.$('#articles-status').textContent, /列表同步完成/);
  assert.equal(f.calls.some(call=>call.route === 'article-read'), false);
});

test('首次连接检查未完成时点击公众号会等待检查，重复点击不重复同步', async () => {
  const probe = deferred(), page = deferred(); let pages = 0;
  const f = fixture(call=>call.route === 'articles-connection' ? probe.promise :
    call.route.startsWith('articles-publishers?') ? followedResult('wxid_a', [publisher]) :
    call.route.startsWith('articles?') ? {account:'wxid_a', articles:[]} :
    call.route === 'articles-read' ? (++pages, page.promise) : basicHandler(call));
  f.workspace.setTarget('a', 'wxid_a', '合成账号 a'); const refresh = f.workspace.refresh(); await settle();
  const button = publisherButtons(f)[0]; const pending = button.onclick(); await button.onclick();
  assert.equal(pages, 0);
  probe.resolve({account:'wxid_a', connected:true, supported:true}); await settle();
  assert.equal(pages, 1);
  page.resolve(syncPage('wxid_a', [record()])); await pending; await refresh;
  assert.equal(pages, 1);
});

test('本地公众号先到达时点击会等关注列表，确认已关注后自动同步', async () => {
  const follows = deferred();
  const f = fixture(call=>call.route.startsWith('articles-publishers?') ? follows.promise : basicHandler(call));
  f.workspace.setTarget('a', 'wxid_a', '合成账号 a'); const refresh = f.workspace.refresh(); await settle();
  const pending = publisherButtons(f)[0].onclick();
  assert.equal(f.calls.some(call=>call.route === 'articles-read'), false);
  follows.resolve(followedResult('wxid_a', [publisher])); await pending; await refresh;
  assert.equal(f.calls.filter(call=>call.route === 'articles-read').length, 1);
});

test('迟到的已关注公众号列表不能进入新账号，切换后重新读取自己的关注列表', async () => {
  const old = deferred();
  const f = fixture(call=>call.route.startsWith('articles-publishers?') ?
    call.robot === 'a' ? old.promise : followedResult('wxid_b', [secondPublisher]) : basicHandler(call));
  f.workspace.setTarget('a', 'wxid_a', '合成账号 a'); const pending = f.workspace.refresh();
  await start(f, 'b', 'wxid_b', false);
  old.resolve(followedResult('wxid_a', [{...publisher, nickname:'旧账号的关注'}])); await pending;
  assert.doesNotMatch(f.$('#articles-publisher-list').textContent, /旧账号/);
  assert.match(f.$('#articles-publisher-list').textContent, /第二刊物/);
  assert.equal(f.calls.some(call=>call.route === 'articles-read'), false);
});

test('关注列表失败保留本地存档，不自动重试；已关注公众号离线时只查看本地内容', async () => {
  const f = fixture(call=>call.route.startsWith('articles-publishers?') ? Promise.reject(new Error('合成读取失败')) : basicHandler(call));
  await start(f, 'a', 'wxid_a', false);
  assert.match(f.$('#articles-publishers-status').textContent, /读取失败/);
  assert.match(f.$('#articles-publisher-list').textContent, /本地存档/);
  await publisherButtons(f)[0].onclick(); assert.match(f.$('#articles-list').textContent, /one/);
  f.$('#page-articles').classList.remove('hidden'); f.workspace.render(); await settle();
  assert.equal(f.calls.filter(call=>call.route.startsWith('articles-publishers?')).length, 1);
  const offline = fixture(call=>call.route.startsWith('articles-publishers?') ? followedResult('wxid_a', [publisher]) :
    call.route === 'articles-connection' ? {account:'wxid_a', connected:false, supported:true} : basicHandler(call));
  await start(offline, 'a', 'wxid_a', false); await publisherButtons(offline)[0].onclick();
  assert.match(offline.$('#articles-list').textContent, /one/);
  assert.equal(offline.calls.some(call=>call.route === 'articles-read'), false);
});

test('自动同步失败保留公众号和已取得页面，仅显式点击同步文章才重试', async () => {
  let requests = 0;
  const f = fixture(call=>call.route.startsWith('articles-publishers?') ? followedResult('wxid_a', [publisher]) :
    call.route === 'articles-read' ? (++requests === 1 ? syncPage('wxid_a', [record('partial')], {
      has_more:true, continuation:'later', complete_remote_history:false}) : Promise.reject(new Error('合成同步失败'))) : basicHandler(call));
  await start(f, 'a', 'wxid_a', false); await publisherButtons(f)[0].onclick();
  assert.match(f.$('#articles-list').textContent, /partial/);
  assert.match(f.$('#articles-status').textContent, /同步中断/);
  f.workspace.render(); await settle(); assert.equal(requests, 2);
  await f.$('#articles-sync-current').onclick(); assert.equal(requests, 3);
});

test('汇总本地列表过大时进入公众号只读取其缓存并合并，然后自动同步', async () => {
  const f = fixture(call=> {
    if (call.route.startsWith('articles-publishers?')) return followedResult('wxid_a', [publisher, secondPublisher]);
    if (call.route.startsWith('articles?')) {
      if (!call.route.includes('&publisher=')) throw new Error('合成本地汇总超过响应上限');
      const owner = call.route.includes(secondPublisher.username) ? secondPublisher : publisher;
      return {account:'wxid_a', articles:[record(owner.username, {publisher:owner})]};
    }
    if (call.route === 'articles-read') return syncPage('wxid_a', [], {
      publisher:call.body.publisher === secondPublisher.username ? secondPublisher : publisher});
    return basicHandler(call);
  });
  await start(f, 'a', 'wxid_a', false);
  await publisherButtons(f).find(button=>button.children[0].textContent === publisher.nickname).onclick();
  assert.match(f.$('#articles-list').textContent, /gh_sample/);
  f.$('#articles-back').onclick();
  await publisherButtons(f).find(button=>button.children[0].textContent === secondPublisher.nickname).onclick();
  assert.match(f.$('#articles-list').textContent, /gh_second/);
  f.$('#articles-back').onclick();
  assert.equal(publisherButtons(f).every(button=>/1 篇文章/.test(button.textContent)), true);
  assert.equal(f.calls.filter(call=>call.route.startsWith('articles?') && call.route.includes('&publisher=')).length, 2);
  assert.equal(f.calls.filter(call=>call.route === 'articles-read').length, 2);
});

test('初始页面先展示公众号目录和分组数量，不混排文章', async () => {
  const f = fixture(groupedHandler); await start(f, 'a', 'wxid_a', false);
  assert.equal(publisherButtons(f).length, 2);
  assert.match(f.$('#articles-publisher-count').textContent, /2 个 · 3 篇文章/);
  const group = publisherButtons(f).find(button=>button.children[0].textContent === publisher.nickname);
  assert.match(group.textContent, /2 篇文章 · 正文已保存 1 篇/);
  assert.equal(f.$('#articles-library').classList.contains('hidden'), false);
  assert.equal(f.$('#articles-content').classList.contains('hidden'), true);
  assert.equal(f.calls.some(call=>call.route.startsWith('article?')), false);
});

test('进入公众号后只显示它的文章，搜索不跨公众号，返回清空正文', async () => {
  const f = fixture(groupedHandler); await start(f, 'a', 'wxid_a', false);
  publisherButtons(f).find(button=>button.children[0].textContent === publisher.nickname).onclick();
  assert.equal(f.$('#articles-current-publisher').textContent, publisher.nickname);
  assert.match(f.$('#articles-count').textContent, /^2 篇/);
  assert.doesNotMatch(f.$('#articles-list').textContent, /other/);
  f.$('#articles-search').value = 'other'; f.$('#articles-search').oninput();
  assert.match(f.$('#articles-list').textContent, /没有匹配/);
  f.$('#articles-search').value = ''; f.$('#articles-search').oninput();
  await listButton(f).onclick();
  f.$('#articles-back').onclick();
  assert.equal(f.$('#articles-content').classList.contains('hidden'), true);
  assert.equal(f.$('#article-markdown').value, '');
  publisherButtons(f).find(button=>button.children[0].textContent === secondPublisher.nickname).onclick();
  assert.match(f.$('#articles-list').textContent, /other/);
  assert.doesNotMatch(f.$('#articles-list').textContent, /first|second/);
  assert.match(f.$('#articles-count').textContent, /^1 篇/);
});

test('同名公众号按独立 gh 标识分组，公众号目录支持搜索名称和微信号', async () => {
  const f = fixture(call=>call.route.startsWith('articles?') ? {account:'wxid_a', articles:[
    record('one'), record('other', {publisher:{...secondPublisher, nickname:publisher.nickname}})]} : basicHandler(call));
  await start(f, 'a', 'wxid_a', false);
  assert.equal(publisherButtons(f).length, 2);
  f.$('#articles-publisher-search').value = 'second_publication'; f.$('#articles-publisher-search').oninput();
  assert.equal(publisherButtons(f).length, 1);
  publisherButtons(f)[0].onclick();
  assert.match(f.$('#articles-list').textContent, /other/);
  assert.doesNotMatch(f.$('#articles-list').textContent, /合成文章 one/);
  f.$('#articles-back').onclick();
  assert.equal(f.$('#articles-publisher-search').value, '');
  assert.equal(publisherButtons(f).length, 2);
});

test('返回目录并切换公众号后，迟到正文不能覆盖新的文章', async () => {
  const late = deferred();
  const f = fixture(call=>call.route.startsWith('article?') && call.route.includes('id=first') ? late.promise :
    call.route.startsWith('article?') ? {account:'wxid_a', article:content('other', {publisher:secondPublisher, markdown:'第二刊物正文'})} : groupedHandler(call));
  await start(f, 'a', 'wxid_a', false);
  publisherButtons(f).find(button=>button.children[0].textContent === publisher.nickname).onclick();
  const pending = listButton(f).onclick(); f.$('#articles-back').onclick();
  publisherButtons(f).find(button=>button.children[0].textContent === secondPublisher.nickname).onclick();
  await listButton(f).onclick();
  late.resolve({account:'wxid_a', article:content('first', {markdown:'旧公众号正文'})}); await pending;
  assert.equal(f.$('#articles-current-publisher').textContent, secondPublisher.nickname);
  assert.match(f.$('#article-preview').textContent, /第二刊物正文/);
  assert.doesNotMatch(f.$('#article-preview').textContent, /旧公众号/);
});

test('同步新公众号自动进入其文章层级，原公众号内容仍保留在目录', async () => {
  const f = fixture(call=>call.route === 'articles-read' ? syncPage('wxid_a', [record('other', {publisher:secondPublisher})],
    {publisher:secondPublisher}) : basicHandler(call));
  await start(f); f.$('#articles-publisher').value = secondPublisher.nickname;
  await f.$('#articles-form').onsubmit({preventDefault(){}});
  assert.equal(f.$('#articles-current-publisher').textContent, secondPublisher.nickname);
  assert.match(f.$('#articles-list').textContent, /other/);
  assert.doesNotMatch(f.$('#articles-list').textContent, /合成文章 one/);
  f.$('#articles-back').onclick();
  assert.equal(publisherButtons(f).length, 2);
});

test('沿用当前账号，离线可读本地列表和正文，Markdown始终只读', async () => {
  const f = fixture(call => call.route === 'articles-connection' ? {account:'wxid_a', connected:false, supported:true} : basicHandler(call));
  await start(f); await listButton(f).onclick();
  assert.equal(f.$('#article-title').textContent, '合成文章 one');
  assert.equal(f.$('#article-markdown').value, content().markdown);
  assert.equal(f.$('#article-markdown').readOnly, true);
  assert.equal(f.$('#articles-read').disabled, true);
  assert.equal(f.$('#article-read').disabled, true);
  assert.equal(f.$('#article-export').disabled, false);
  assert.ok(f.calls.some(call=>call.route === 'articles?account=wxid_a' && call.robot === 'a'));
  assert.ok(f.calls.some(call=>call.route === 'article?id=one&account=wxid_a'));
  assert.equal(f.calls.some(call=>call.route === 'article-draft'), false);
});

test('点击未保存正文的文章直接在站内加载，无需再次点击读取或打开来源', async () => {
  const f = fixture(call => call.route.startsWith('article?') ?
    {account:'wxid_a', article:content('one', {markdown:'', has_content:false})} : basicHandler(call));
  await start(f); await listButton(f).onclick();
  assert.equal(f.calls.filter(call=>call.route === 'article-read').length, 1);
  assert.match(f.$('#article-preview').textContent, /从微信读取/);
  assert.equal(f.$('#article-read').textContent, '刷新正文');
  assert.equal(f.$('#article-origin').open, false);
  assert.equal(f.downloads.length, 0);
});

test('已有正文直接站内阅读，不重复请求微信', async () => {
  const f = fixture(basicHandler); await start(f); await listButton(f).onclick();
  assert.equal(f.calls.filter(call=>call.route === 'article-read').length, 0);
  assert.match(f.$('#article-preview').textContent, /合成正文/);
  assert.equal(f.$('#article-origin').open, false);
});

test('自动加载失败后保留站内重试入口，不循环读取或跳转外站', async () => {
  let attempts = 0;
  const f = fixture(call => {
    if (call.route.startsWith('article?')) return {account:'wxid_a', article:content('one', {markdown:'', has_content:false})};
    if (call.route === 'article-read' && ++attempts === 1) throw new Error('微信暂未完成正文加载，请重试');
    return basicHandler(call);
  });
  await start(f); await listButton(f).onclick();
  f.workspace.render(false); f.workspace.render(false);
  assert.equal(attempts, 1);
  assert.match(f.$('#article-status').textContent, /微信暂未完成正文加载/);
  assert.equal(f.$('#article-read').disabled, false);
  assert.equal(f.downloads.length, 0);
  await f.$('#article-read').onclick();
  assert.equal(attempts, 2);
  assert.match(f.$('#article-preview').textContent, /从微信读取/);
});

test('自动加载期间切换账号，迟到正文不能进入新账号页面', async () => {
  const entered = deferred(), response = deferred();
  const f = fixture(call => {
    if (call.robot === 'a' && call.route.startsWith('article?')) return {account:'wxid_a', article:content('one', {markdown:'', has_content:false})};
    if (call.robot === 'a' && call.route === 'article-read') { entered.resolve(); return response.promise; }
    return basicHandler(call);
  });
  await start(f); const pending = listButton(f).onclick(); await entered.promise;
  await start(f, 'b'); await listButton(f).onclick();
  response.resolve({account:'wxid_a', article:content('one', {markdown:'旧账号正文'})}); await pending;
  assert.match(f.$('#articles-identity').textContent, /账号 b/);
  assert.doesNotMatch(f.$('#article-preview').textContent, /旧账号/);
  assert.match(f.$('#article-preview').textContent, /合成正文/);
});

test('公众号同步和正文请求绑定公众号及当前账号', async () => {
  const f = fixture(basicHandler); await start(f);
  f.$('#articles-publisher').value = '示例刊物'; f.$('#articles-publisher').oninput();
  assert.equal(f.$('#articles-read').disabled, false);
  await f.$('#articles-form').onsubmit({preventDefault(){}});
  assert.deepEqual(f.calls.find(call=>call.route === 'articles-read'), {route:'articles-read', robot:'a', body:{account:'wxid_a', publisher:'示例刊物'}});
  assert.equal(f.$('#articles-publishers').children[0].value, publisher.username);
  assert.match(f.$('#articles-status').textContent, /已同步 1 篇文章，列表同步完成/);
  await listButton(f).onclick(); await f.$('#article-read').onclick();
  assert.deepEqual(f.calls.find(call=>call.route === 'article-read').body, {account:'wxid_a', publisher:'gh_sample', url:record().url});
  assert.match(f.$('#article-markdown').value, /从微信读取/);
});

test('列表和详情统一显示本地日期时间，忽略无效时间', async () => {
  const timestamp = '2026-01-02T00:00:00+08:00', expected = new Date(timestamp).toLocaleString('zh-CN', {hour12:false});
  const f = fixture(call => call.route.startsWith('articles?') ? {account:'wxid_a', articles:[record('one', {published_at:timestamp}), record('invalid', {published_at:'invalid-date'})]} :
    call.route.startsWith('article?') ? {account:'wxid_a', article:content('one', {published_at:timestamp})} : basicHandler(call));
  await start(f); await listButton(f).onclick();
  assert.equal(listButton(f).children[1].textContent, publisher.nickname + ' · ' + expected);
  assert.equal(f.$('#article-meta').textContent, publisher.nickname + ' · ' + expected);
  assert.equal(listButton(f, 1).children[1].textContent, publisher.nickname);
});

test('达到同步上限时保留结果并说明未取完历史，不要求手动加载微信', async () => {
  const f = fixture(call => call.route === 'articles-read' ? syncPage('wxid_a', [record('partial')], {
    complete_remote_history:false, limit_reached:true}) : basicHandler(call));
  await start(f); f.$('#articles-publisher').value = '示例刊物';
  await f.$('#articles-form').onsubmit({preventDefault(){}});
  assert.match(f.$('#articles-list').textContent, /partial/);
  assert.match(f.$('#articles-status').textContent, /已同步 1 篇，已达到本次同步上限/);
  assert.match(f.$('#articles-status').textContent, /部分历史尚未获取/);
  assert.doesNotMatch(f.$('#articles-status').textContent, /刷新或下滑|先在电脑微信|手动打开/);
  assert.doesNotMatch(f.$('#articles-status').textContent, /byte|renderer/i);
});

test('目录自动跟随两页，逐页展示进度和保存结果，无需再次点击', async () => {
  const entered = deferred(), second = deferred();
  const f = fixture(call => {
    if (call.route !== 'articles-read') return basicHandler(call);
    if (!call.body.continuation) return syncPage('wxid_a', [record('first')], {
      has_more:true, continuation:'opaque-page-two', complete_remote_history:false});
    assert.equal(call.body.continuation, 'opaque-page-two');
    entered.resolve(); return second.promise;
  });
  await start(f); f.$('#articles-publisher').value = '示例刊物';
  const pending = f.$('#articles-form').onsubmit({preventDefault(){}}); await entered.promise;
  assert.match(f.$('#articles-list').textContent, /first/);
  assert.match(f.$('#articles-status').textContent, /已同步 1 篇（第 1 页）/);
  assert.equal(f.$('#articles-read').textContent, '同步中…');
  assert.equal(listButton(f).disabled, true);
  await listButton(f).onclick(); assert.equal(f.calls.some(call=>call.route.startsWith('article?')), false);
  second.resolve(syncPage('wxid_a', [record('second')], {page_count:2, fetched_count:2})); await pending;
  const requests = f.calls.filter(call=>call.route === 'articles-read');
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[1].body, {account:'wxid_a', publisher:'示例刊物', continuation:'opaque-page-two'});
  assert.match(f.$('#articles-list').textContent, /first/); assert.match(f.$('#articles-list').textContent, /second/);
  assert.match(f.$('#articles-status').textContent, /已同步 2 篇文章，列表同步完成/);
  assert.equal(listButton(f).disabled, false);
  assert.equal(f.$('#articles-read').textContent, '同步文章列表');
});

test('中途失败保留已保存页面，重试从第一页开始，不自动重试失败页面', async () => {
  let requests = 0;
  const f = fixture(call => {
    if (call.route !== 'articles-read') return basicHandler(call);
    requests++;
    if (requests === 1) return syncPage('wxid_a', [record('partial')], {
      has_more:true, continuation:'opaque-later', complete_remote_history:false});
    if (requests === 2) throw Error('同步请求超时');
    assert.equal(call.body.continuation, undefined);
    return syncPage('wxid_a', [record('retry')]);
  });
  await start(f); f.$('#articles-publisher').value = '示例刊物';
  await f.$('#articles-form').onsubmit({preventDefault(){}});
  assert.equal(requests, 2); assert.match(f.$('#articles-list').textContent, /partial/);
  assert.match(f.$('#articles-status').textContent, /同步中断，已保存 1 篇。同步请求超时/);
  assert.match(f.$('#articles-status').textContent, /再次同步将从第一页开始/);
  assert.equal(listButton(f).disabled, false);
  f.workspace.render(false); await settle(); assert.equal(requests, 2);
  await f.$('#articles-form').onsubmit({preventDefault(){}});
  assert.equal(requests, 3); assert.match(f.$('#articles-list').textContent, /retry/);
});

test('切换账号后丢弃迟到同步页，不继续请求旧账号后续页', async () => {
  const page = deferred();
  const f = fixture(call => call.route === 'articles-read' ? page.promise : basicHandler(call));
  await start(f); f.$('#articles-publisher').value = '示例刊物';
  const pending = f.$('#articles-form').onsubmit({preventDefault(){}});
  await start(f, 'b');
  page.resolve(syncPage('wxid_a', [record('old-account')], {
    has_more:true, continuation:'old-token', complete_remote_history:false})); await pending;
  assert.equal(f.calls.filter(call=>call.route === 'articles-read').length, 1);
  assert.doesNotMatch(f.$('#articles-list').textContent, /old-account/);
  assert.doesNotMatch(f.$('#articles-status').textContent, /旧账号|已同步|同步中断/);
  assert.match(f.$('#articles-identity').textContent, /账号 b/);
});

test('主动同步期间暂停连接探测，迟到探测不能误停已核验的分页', async () => {
  const probe = deferred(), page = deferred();
  let probes = 0, pages = 0;
  const f = fixture(call => {
    if (call.route === 'articles-connection' && ++probes > 1) return probe.promise;
    if (call.route === 'articles-read') return ++pages === 1 ? page.promise :
      syncPage('wxid_a', [record('second')], {page_count:2, fetched_count:2});
    return basicHandler(call);
  });
  await start(f); f.$('#page-articles').classList.remove('hidden');
  f.advance(9000); f.workspace.render(); await settle();
  assert.equal(probes, 2);
  f.$('#articles-publisher').value = '示例刊物';
  const pending = f.$('#articles-form').onsubmit({preventDefault(){}});
  probe.resolve({account:'wxid_a', connected:true, supported:false, reason:'合成的瞬时探测变化'});
  await settle(); f.advance(9000); f.workspace.render(); await settle();
  assert.equal(probes, 2);
  page.resolve(syncPage('wxid_a', [record('first')], {
    has_more:true, continuation:'page-two', complete_remote_history:false}));
  await pending;
  assert.equal(pages, 2);
  assert.match(f.$('#articles-status').textContent, /列表同步完成/);
});

test('管理服务断开后停止自动分页，恢复后不会自行重启同步', async () => {
  const f = fixture(call => {
    if (call.route !== 'articles-read') return basicHandler(call);
    f.setOnline(false);
    return syncPage('wxid_a', [record('saved-before-offline')], {
      has_more:true, continuation:'next-token', complete_remote_history:false});
  });
  await start(f); f.$('#articles-publisher').value = '示例刊物';
  await f.$('#articles-form').onsubmit({preventDefault(){}});
  assert.equal(f.calls.filter(call=>call.route === 'articles-read').length, 1);
  assert.match(f.$('#articles-list').textContent, /saved-before-offline/);
  assert.match(f.$('#articles-status').textContent, /连接中断，同步已停止/);
  f.setOnline(true); f.workspace.render(false); await settle();
  assert.equal(f.calls.filter(call=>call.route === 'articles-read').length, 1);
});

test('分页标识重复时停止，不无限循环，已经到达的页面仍可阅读', async () => {
  let page = 0;
  const f = fixture(call => call.route === 'articles-read' ? syncPage('wxid_a', [record('page-' + ++page)], {
    has_more:true, continuation:'same-token', complete_remote_history:false, page_count:page, fetched_count:page}) : basicHandler(call));
  await start(f); f.$('#articles-publisher').value = '示例刊物';
  await f.$('#articles-form').onsubmit({preventDefault(){}});
  assert.equal(page, 2); assert.match(f.$('#articles-list').textContent, /page-2/);
  assert.match(f.$('#articles-status').textContent, /进度未推进/);
  assert.equal(listButton(f).disabled, false);
});

test('不完整分页协议显示错误，不把旧缓存读取当作同步完成', async () => {
  const f = fixture(call => call.route === 'articles-read' ? {account:'wxid_a', publisher, articles:[record('unknown')]} : basicHandler(call));
  await start(f); f.$('#articles-publisher').value = '示例刊物';
  await f.$('#articles-form').onsubmit({preventDefault(){}});
  assert.equal(f.calls.filter(call=>call.route === 'articles-read').length, 1);
  assert.match(f.$('#articles-status').textContent, /同步结果不完整/);
  assert.doesNotMatch(f.$('#articles-list').textContent, /unknown/);
});

test('空页面持续返回后续标识时最多请求50页，达到500篇也停止', async () => {
  for (const fill of [false, true]) {
    let page = 0;
    const f = fixture(call => {
      if (call.route !== 'articles-read') return basicHandler(call);
      page++;
      return syncPage('wxid_a', fill ? Array.from({length:500},(_,index)=>record('bulk-' + index)) : [], {
        has_more:true, continuation:'page-' + page, complete_remote_history:false, page_count:page, fetched_count:fill ? 500 : 0});
    });
    await start(f); f.$('#articles-publisher').value = '示例刊物';
    await f.$('#articles-form').onsubmit({preventDefault(){}});
    assert.equal(page, fill ? 1 : 50);
    assert.match(f.$('#articles-status').textContent, /达到本次同步上限/);
    assert.doesNotMatch(f.$('#articles-status').textContent, /列表同步完成/);
  }
});

test('一次点击未缓存文章即可取得正文并自动显示全部图片', async () => {
  const entered = deferred(), body = deferred();
  const f = fixture(call => {
    if (call.route.startsWith('article?')) return {account:'wxid_a', article:content('one', {markdown:'', has_content:false})};
    if (call.route === 'article-read') { entered.resolve(); return body.promise; }
    return basicHandler(call);
  });
  await start(f); f.$('#articles-publisher').value = '示例刊物';
  const pending = listButton(f).onclick(); await entered.promise;
  assert.match(f.$('#article-status').textContent, /正在让微信加载正文/);
  assert.equal(f.$('#articles-read').disabled, true);
  body.resolve({account:'wxid_a', article:content('one', {
    markdown:'自动载入完整正文\n\n![图片 1](article-image:1)\n\n![图片 2](article-image:2)', images:[{index:1},{index:2}]})});
  await pending; await settle();
  assert.equal(f.calls.filter(call=>call.route === 'article-read').length, 1);
  assert.equal(f.calls.filter(call=>call.route === 'article-media').length, 2);
  assert.match(f.$('#article-preview').textContent, /自动载入完整正文/);
  assert.equal(f.$('#article-preview').querySelectorAll('img').length, 2);
  assert.equal(f.$('#article-origin').open, false);
});

test('切换robot立即清空旧内容，迟到列表和连接结果不能污染新账号', async () => {
  const old = deferred();
  const f = fixture(async call => { if (call.robot === 'a') await old.promise; return basicHandler(call); });
  f.workspace.setTarget('a', 'wxid_a', '合成账号 a'); const pending = f.workspace.refresh();
  f.$('#articles-publisher').value = '旧输入'; f.$('#articles-search').value = '旧搜索';
  f.workspace.setTarget('b', 'wxid_b', '合成账号 b');
  assert.equal(f.$('#articles-publisher').value, ''); assert.equal(f.$('#articles-search').value, '');
  assert.equal(f.$('#article-markdown').value, '');
  await f.workspace.refresh(); publisherButtons(f)[0].onclick(); await listButton(f).onclick();
  old.resolve(); await pending;
  assert.match(f.$('#articles-identity').textContent, /账号 b/);
  assert.equal(f.$('#article-markdown').value, content().markdown);
  assert.equal(f.$('#article-read').disabled, false);
  assert.equal(f.$('#articles-list').children.length, 1);
});

test('同一robot重新绑定账号也清空，并拒绝迟到正文', async () => {
  const detail = deferred();
  const f = fixture(async call => {
    if (call.route.startsWith('article?') && call.route.includes('wxid_a')) return detail.promise;
    if (call.route === 'articles-connection') return {account:'wxid_new', connected:true, supported:true};
    if (call.route.startsWith('articles?') && call.route.includes('wxid_new')) return {account:'wxid_new', articles:[record('new')]};
    return basicHandler(call);
  });
  await start(f); const pending = listButton(f).onclick();
  f.workspace.setTarget('a', 'wxid_new', '新的合成账号'); await f.workspace.refresh();
  detail.resolve({account:'wxid_a', article:content('one', {markdown:'旧账号正文'})}); await pending;
  assert.equal(f.$('#article-markdown').value, '');
  assert.equal(f.$('#article-detail').classList.contains('hidden'), true);
  assert.equal(f.$('#articles-content').classList.contains('hidden'), true);
  publisherButtons(f)[0].onclick();
  assert.match(f.$('#articles-list').textContent, /new/);
});

test('响应account不匹配时不展示内容、不启用微信读取', async () => {
  const f = fixture(call => call.route === 'articles-connection' ? {account:'wxid_other', connected:true, supported:true} :
    call.route.startsWith('articles?') ? {account:'wxid_other', articles:[record('foreign')]} : basicHandler(call));
  await start(f);
  assert.doesNotMatch(f.$('#articles-list').textContent, /foreign/);
  assert.match(f.$('#articles-status').textContent, /账号已变化/);
  assert.equal(f.$('#articles-read').disabled, true);
  assert.match(f.$('#articles-availability').textContent, /账号已变化/);
});

test('切换文章后迟到正文不能覆盖当前选择', async () => {
  const old = deferred();
  const f = fixture(call => call.route.startsWith('articles?') ? {account:'wxid_a', articles:[record('one'), record('two')]} :
    call.route.startsWith('article?id=one') ? old.promise : basicHandler(call));
  await start(f); const pending = listButton(f, 0).onclick(); await listButton(f, 1).onclick();
  old.resolve({account:'wxid_a', article:content('one', {markdown:'旧文章'})}); await pending;
  assert.equal(f.$('#article-title').textContent, '合成文章 two');
  assert.equal(f.$('#article-markdown').value, content('two').markdown);
});

test('原文和Markdown中的HTML仅显示文本，普通链接与图片URL不执行', async () => {
  const markdown = '# 合成标题\n\n<script>alert(1)</script>\n<img src=x onerror=alert(2)>\n[链接](javascript:alert(3))\n![远程](https://example.invalid/image.png)';
  const f = fixture(call => call.route.startsWith('article?') ? {account:'wxid_a', article:content('one', {url:'javascript:alert(1)', markdown})} : basicHandler(call));
  await start(f); await listButton(f).onclick();
  assert.equal(f.$('#article-markdown').value, markdown);
  assert.match(f.$('#article-preview').textContent, /<script>alert\(1\)<\/script>/);
  assert.equal(f.$('#article-preview').querySelectorAll('script').length, 0);
  assert.equal(f.$('#article-preview').querySelectorAll('img').length, 0);
  assert.equal(f.$('#article-preview').querySelectorAll('a').length, 0);
  assert.equal(f.$('#article-original').href, undefined);
  assert.equal(f.$('#article-original').classList.contains('hidden'), true);
});

test('预览还原生成的Markdown转义为普通文本，不重新解析图片样式或HTML', async () => {
  const markdown = String.raw`普通 \[方括号\] \(括号\) \*星号\* \_下划线\_ ` + '\\`代码\\`' +
    String.raw` \#号 \! 反斜杠\\ 保留\q &lt;script&gt;alert\(1\)&lt;/script&gt; &lt;img src=x onerror=alert\(2\)&gt; \[链接\]\(javascript:alert\(3\)\) \!\[图片 7\]\(article-image:7\)`;
  const f = fixture(call => call.route.startsWith('article?') ? {account:'wxid_a', article:content('one', {
    markdown, images:[{index:7, url:'https://example.invalid/image'}]})} : basicHandler(call));
  await start(f); await listButton(f).onclick();
  assert.equal(f.$('#article-markdown').value, markdown);
  assert.equal(f.$('#article-preview').textContent, '普通 [方括号] (括号) *星号* _下划线_ `代码` #号 ! 反斜杠\\ 保留\\q <script>alert(1)</script> <img src=x onerror=alert(2)> [链接](javascript:alert(3)) ![图片 7](article-image:7)');
  for (const tag of ['script', 'img', 'a', 'button']) assert.equal(f.$('#article-preview').querySelectorAll(tag).length, 0);
  assert.equal(f.calls.some(call=>call.route === 'article-media'), false);
});

test('空 HTML 标题生成的 Markdown 标记不作为正文显示', async () => {
  const f = fixture(call => call.route.startsWith('article?') ? {account:'wxid_a', article:content('one', {
    markdown:'###\n\n## 有效标题\n\n正文中的 \\# 保留。'})} : basicHandler(call));
  await start(f); await listButton(f).onclick();
  assert.doesNotMatch(f.$('#article-preview').textContent, /###/);
  assert.match(f.$('#article-preview').textContent, /有效标题/);
  assert.match(f.$('#article-preview').textContent, /正文中的 # 保留/);
});

test('打开正文自动加载已校验的图片占位，未知index保持文本且不请求原始URL', async () => {
  const f = fixture(call => call.route.startsWith('article?') ? {account:'wxid_a', article:content('one', {
    markdown:'正文\n\n![图片 7](article-image:7)\n![图片 1](article-image:1)', images:[{index:7, url:'https://example.invalid/private-image'}]})} : basicHandler(call));
  await start(f); await listButton(f).onclick(); await settle();
  assert.equal(f.calls.filter(call=>call.route === 'article-media').length, 1);
  assert.match(f.$('#article-preview').textContent, /article-image:1/);
  assert.equal(f.$('#article-preview').querySelectorAll('button').length, 0);
  assert.deepEqual(f.calls.find(call=>call.route === 'article-media').body, {account:'wxid_a', id:'one', index:7});
  assert.equal(f.$('#article-preview').querySelectorAll('img')[0].src, 'data:image/png;base64,cG5n');
});

test('切换账号后迟到图片不会写入新账号预览', async () => {
  const image = deferred();
  const f = fixture(call => call.robot === 'a' && call.route.startsWith('article?') ? {account:'wxid_a', article:content('one', {
    markdown:'![图片 1](article-image:1)', images:[{index:1, url:'https://example.invalid/image'}]})} :
    call.route === 'article-media' ? image.promise : basicHandler(call));
  await start(f); await listButton(f).onclick(); await settle();
  await start(f, 'b'); await listButton(f).onclick();
  image.resolve({account:'wxid_a', mime:'image/png', data_base64:'cG5n'}); await settle();
  assert.equal(f.$('#article-preview').querySelectorAll('img').length, 0);
  assert.equal(f.$('#article-preview').querySelectorAll('button').length, 0);
});

test('图片接口返回SVG、非base64或超限数据时只显示错误', async () => {
  for (const response of [{mime:'image/svg+xml', data_base64:'c3Zn'}, {mime:'image/png', data_base64:'" onerror="'},
    {mime:'image/png', data_base64:'A'.repeat(3 * 1024 * 1024 + 4)}]) {
    const f = fixture(call => call.route.startsWith('article?') ? {account:'wxid_a', article:content('one', {
      markdown:'![图片 1](article-image:1)', images:[{index:1, url:'https://example.invalid/image'}]})} :
      call.route === 'article-media' ? {account:'wxid_a', ...response} : basicHandler(call));
    await start(f); await listButton(f).onclick(); await settle();
    assert.equal(f.$('#article-preview').querySelectorAll('img').length, 0);
    assert.match(f.$('#article-preview').textContent, /无法预览/);
    assert.match(f.$('#article-preview').querySelectorAll('button')[0].textContent, /重试/);
  }
});

test('自动下载最多并发两张，重复占位只请求一次，图片到达时不重建正文', async () => {
  const replies = new Map(); let active = 0, peak = 0;
  const images = [1, 2, 3, 4, 5].map(index=>({index}));
  const markdown = '正文保持不变\n\n' + [1, 2, 3, 4, 5, 1].map(index=>`![图片 ${index}](article-image:${index})`).join('\n\n');
  const f = fixture(async call => {
    if (call.route.startsWith('article?')) return {account:'wxid_a', article:content('one', {markdown, images})};
    if (call.route === 'article-media') {
      active++; peak = Math.max(peak, active);
      const reply = deferred(); replies.set(call.body.index, reply);
      try { return await reply.promise; } finally { active--; }
    }
    return basicHandler(call);
  });
  await start(f); await listButton(f).onclick(); await settle();
  const firstParagraph = f.$('#article-preview').children[0];
  assert.deepEqual([...replies.keys()], [1, 2]);
  for (const index of [1, 2, 3, 4, 5]) {
    replies.get(index).resolve({account:'wxid_a', mime:'image/png', data_base64:'cG5n'}); await settle();
    assert.equal(f.$('#article-preview').children[0], firstParagraph);
  }
  assert.equal(peak, 2);
  assert.equal(f.$('#article-preview').querySelectorAll('img').length, 6);
  assert.equal(f.$('#article-preview').querySelectorAll('button').length, 0);
  const firstImage = f.$('#article-preview').querySelectorAll('img')[0];
  f.workspace.render(false); f.workspace.render(false); await settle();
  assert.equal(f.$('#article-preview').querySelectorAll('img')[0], firstImage);
  assert.equal(f.calls.filter(call=>call.route === 'article-media').length, 5);
});

test('一张图片失败不阻塞其他图片，只在用户重试时重新请求', async () => {
  let attempts = 0;
  const f = fixture(call => {
    if (call.route.startsWith('article?')) return {account:'wxid_a', article:content('one', {
      markdown:'正文仍可阅读\n\n![图片 1](article-image:1)\n\n![图片 2](article-image:2)', images:[{index:1},{index:2}]})};
    if (call.route === 'article-media' && call.body.index === 1 && ++attempts === 1) throw new Error('图片连接失败');
    return basicHandler(call);
  });
  await start(f); await listButton(f).onclick(); await settle();
  assert.equal(f.$('#article-preview').querySelectorAll('img').length, 1);
  assert.match(f.$('#article-preview').textContent, /正文仍可阅读/);
  f.workspace.render(false); await settle(); assert.equal(attempts, 1);
  const loaded = f.$('#article-preview').querySelectorAll('img')[0];
  await f.$('#article-preview').querySelectorAll('button')[0].onclick(); await settle();
  assert.equal(attempts, 2);
  assert.equal(f.$('#article-preview').querySelectorAll('img').length, 2);
  assert.equal(f.$('#article-preview').querySelectorAll('img')[1], loaded);
  assert.equal(f.$('#article-preview').querySelectorAll('button').length, 0);
  f.$('#article-preview').querySelectorAll('img')[0].onerror();
  assert.match(f.$('#article-preview').textContent, /图片无法显示/);
  assert.equal(f.$('#article-preview').querySelectorAll('button').length, 1);
});

test('切换账号丢弃旧队列，并发中的旧图片仍占用下载槽直到完成', async () => {
  const replies = new Map(); let active = 0, peak = 0;
  const images = [1, 2, 3, 4].map(index=>({index}));
  const markdown = images.map(image=>`![图片 ${image.index}](article-image:${image.index})`).join('\n\n');
  const f = fixture(async call => {
    if (call.route.startsWith('article?')) return {account:'wxid_' + call.robot, article:content('one', {markdown, images})};
    if (call.route === 'article-media') {
      active++; peak = Math.max(peak, active);
      const reply = deferred(); replies.set(call.robot + ':' + call.body.index, reply);
      try { return await reply.promise; } finally { active--; }
    }
    return basicHandler(call);
  });
  await start(f); await listButton(f).onclick(); await settle();
  await start(f, 'b'); await listButton(f).onclick(); await settle();
  assert.deepEqual([...replies.keys()], ['a:1','a:2']);
  for (const index of [1, 2]) {
    replies.get('a:' + index).resolve({account:'wxid_a', mime:'image/png', data_base64:'T0xE'}); await settle();
  }
  assert.equal(f.$('#article-preview').querySelectorAll('img').length, 0);
  for (const index of [1, 2, 3, 4]) {
    replies.get('b:' + index).resolve({account:'wxid_b', mime:'image/png', data_base64:'TkVX'}); await settle();
  }
  assert.equal(peak, 2);
  assert.equal(replies.has('a:3'), false); assert.equal(replies.has('a:4'), false);
  assert.equal(f.$('#article-preview').querySelectorAll('img').length, 4);
  for (const img of f.$('#article-preview').querySelectorAll('img')) assert.equal(img.src, 'data:image/png;base64,TkVX');
});

test('切换文章后旧图片不能覆盖同序号的新图片', async () => {
  const old = deferred();
  const f = fixture(call => {
    if (call.route.startsWith('articles?')) return {account:'wxid_a', articles:[record('one'),record('two')]};
    if (call.route.startsWith('article?')) {
      const id = new URL('http://local/?' + call.route.split('?')[1]).searchParams.get('id');
      return {account:'wxid_a', article:content(id, {markdown:'![图片 1](article-image:1)', images:[{index:1}]})};
    }
    if (call.route === 'article-media') return call.body.id === 'one' ? old.promise : {account:'wxid_a', mime:'image/png', data_base64:'TkVX'};
    return basicHandler(call);
  });
  await start(f); await listButton(f).onclick(); await listButton(f, 1).onclick(); await settle();
  old.resolve({account:'wxid_a', mime:'image/png', data_base64:'T0xE'}); await settle();
  assert.match(f.$('#article-title').textContent, /two/);
  assert.equal(f.$('#article-preview').querySelectorAll('img')[0].src, 'data:image/png;base64,TkVX');
});

test('管理服务断开时暂停尚未开始的图片，恢复后自动继续且不重复下载', async () => {
  const replies = new Map();
  const images = [1, 2, 3, 4].map(index=>({index}));
  const f = fixture(call => {
    if (call.route.startsWith('article?')) return {account:'wxid_a', article:content('one', {
      markdown:images.map(image=>`![图片 ${image.index}](article-image:${image.index})`).join('\n\n'), images})};
    if (call.route === 'article-media' && call.body.index <= 2) {
      const reply = deferred(); replies.set(call.body.index, reply); return reply.promise;
    }
    return basicHandler(call);
  });
  await start(f); await listButton(f).onclick(); await settle();
  f.setOnline(false); f.workspace.render(false);
  for (const reply of replies.values()) reply.resolve({account:'wxid_a', mime:'image/png', data_base64:'cG5n'});
  await settle(); assert.equal(f.calls.filter(call=>call.route === 'article-media').length, 2);
  f.setOnline(true); f.workspace.render(false); await settle();
  assert.equal(f.calls.filter(call=>call.route === 'article-media').length, 4);
  assert.equal(f.$('#article-preview').querySelectorAll('img').length, 4);
});

test('Markdown通过Blob导出原文并释放URL，不执行其中HTML', async () => {
  const markdown = '# 导出\n\n<script>合成文本</script>\n![图片 7](article-image:7)';
  const f = fixture(call => call.route === 'article-export' ? {account:'wxid_a', filename:'合成文章.md', markdown} : basicHandler(call));
  await start(f); await listButton(f).onclick(); await f.$('#article-export').onclick();
  assert.deepEqual(f.calls.find(call=>call.route === 'article-export'), {route:'article-export', robot:'a', body:{account:'wxid_a', id:'one'}});
  assert.deepEqual(f.downloads, [{href:'blob:synthetic-1', filename:'合成文章.md'}]);
  assert.equal(await f.blobs[0].text(), markdown);
  assert.deepEqual(f.revoked, ['blob:synthetic-1']);
});

test('切换账号后迟到导出不触发下载，错误也不覆盖新账号提示', async () => {
  const output = deferred();
  const f = fixture(call => call.route === 'article-export' ? output.promise : basicHandler(call));
  await start(f); await listButton(f).onclick(); const pending = f.$('#article-export').onclick();
  await start(f, 'b'); await listButton(f).onclick();
  output.resolve({account:'wxid_a', filename:'旧文章.md', markdown:'旧内容'}); await pending;
  assert.equal(f.downloads.length, 0);
  assert.equal(f.$('#article-status').textContent, '');
  assert.equal(f.$('#article-export').disabled, false);
});

test('媒体或导出响应账号不匹配时不显示或下载内容', async () => {
  const f = fixture(call => call.route.startsWith('article?') ? {account:'wxid_a', article:content('one', {
    markdown:'![图片 1](article-image:1)', images:[{index:1, url:'https://example.invalid/image'}]})} :
    call.route === 'article-media' ? {account:'wxid_other', mime:'image/png', data_base64:'cG5n'} :
    call.route === 'article-export' ? {account:'wxid_other', filename:'其他账号.md', markdown:'其他内容'} : basicHandler(call));
  await start(f); await listButton(f).onclick(); await settle();
  assert.equal(f.$('#article-preview').querySelectorAll('img').length, 0);
  assert.match(f.$('#article-preview').textContent, /账号已变化/);
  await f.$('#article-export').onclick();
  assert.equal(f.downloads.length, 0); assert.match(f.$('#article-status').textContent, /账号已变化/);
});

test('网页注册独立只读模块，robot切换先reset，无编辑或草稿保存接口', () => {
  assert.match(html, /<script src="\/articles\.js"><\/script>/);
  assert.match(html, /data-page="articles"/);
  assert.match(html, /id="article-markdown"[^>]*readonly/);
  assert.match(app, /articles\.setTarget\(robot,articleAccount/);
  const switchBody = app.slice(app.indexOf('async function switchRobot(identifier)'), app.indexOf("$('#robot-select').onchange"));
  assert.ok(switchBody.indexOf('articles.reset()') < switchBody.indexOf('robot=identifier'));
  assert.doesNotMatch(source, /innerHTML|article-draft|revision/);
});
