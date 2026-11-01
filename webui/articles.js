'use strict';
window.createArticlesWorkspace = ({api, available}) => {
  const $ = selector => document.querySelector(selector);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  let target = '', account = '', accountLabel = '', generation = 0, selection = 0;
  let connection = null, lastCheck = 0, loaded = false, rows = [], selected = null, article = null;
  let selectedPublisher = '';
  let connectionRequest = null, listRequest = null, articleRequest = null, exportRequest = null;
  let followedPublishers = [], publishersLoaded = false, publisherRequest = null, publisherOpenRequest = null;
  let publisherMessage = '', publisherError = false;
  let savedListFailed = false;
  const media = new Map(), mediaViews = new Map();
  const imageConcurrency = 2, maxSyncPages = 50, maxSyncArticles = 500;
  let imageQueue = [], activeImages = 0;
  const publisherKey = value => typeof value === 'string' ? value : value?.username || value?.alias || value?.nickname || '';
  const publisherLabel = value => typeof value === 'string' ? value : value?.nickname || value?.alias || value?.username || '';
  const publishedAt = value => {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', {hour12: false});
  };
  const capture = () => ({robot: target, account, generation, selection});
  const current = (request, detail = false) => request.robot === target && request.account === account &&
    request.generation === generation && (!detail || request.selection === selection);
  const query = () => 'account=' + encodeURIComponent(account);
  const ready = () => !!target && !!account && available() && !!connection?.connected && !!connection?.supported;
  function status(message, error = false, detail = false) {
    const element = $(detail ? '#article-status' : '#articles-status');
    element.textContent = message; element.classList.toggle('account-error', error);
  }
  function checkAccount(result, request) {
    if (result.account !== request.account) throw new Error('账号已变化，请重新选择当前账号。');
  }
  function mergeRows(additions, replace = false) {
    const entries = new Map((replace ? [] : rows).map(row => [row.id, row]));
    for (const row of additions || []) if (typeof row.id === 'string' && row.id) entries.set(row.id, row);
    rows = [...entries.values()];
    if (selectedPublisher && !publisherGroups().some(group => group.key === selectedPublisher)) {
      selectedPublisher = ''; clearArticle();
    }
    drawList(); drawPublishers();
  }
  function publisherGroups() {
    const groups = new Map();
    for (const owner of followedPublishers) {
      const key = publisherKey(owner);
      groups.set(key, {key, publisher:owner, followed:true, count:0, saved:0, latest:0});
    }
    for (const row of rows) {
      const key = publisherKey(row.publisher);
      if (!key) continue;
      if (!groups.has(key)) groups.set(key, {key, publisher:row.publisher, followed:false, count:0, saved:0, latest:0});
      const group = groups.get(key);
      group.count++; if (row.has_content) group.saved++;
      const timestamp = Date.parse(row.published_at);
      if (Number.isFinite(timestamp)) group.latest = Math.max(group.latest, timestamp);
    }
    return [...groups.values()].sort((a, b) => Number(b.followed) - Number(a.followed) || b.latest - a.latest ||
      publisherLabel(a.publisher).localeCompare(publisherLabel(b.publisher), 'zh-CN') || a.key.localeCompare(b.key));
  }
  function drawPublishers() {
    const groups = publisherGroups(), search = $('#articles-publisher-search').value.trim().toLowerCase();
    $('#articles-publishers').replaceChildren(...groups.map(group => {
      const option = node('option', publisherLabel(group.publisher)); option.value = group.key; return option;
    }));
    $('#articles-publisher-count').textContent = groups.length ? groups.length + ' 个 · ' + rows.length + ' 篇文章' : '';
    const visible = groups.filter(group => [publisherLabel(group.publisher), publisherKey(group.publisher), group.publisher?.alias || '']
      .join(' ').toLowerCase().includes(search));
    const list = $('#articles-publisher-list'); list.replaceChildren();
    for (const group of visible) {
      const item = node('li'), button = node('button', undefined, 'article-publisher-item');
      button.type = 'button'; button.disabled = !!listRequest || !!publisherOpenRequest;
      button.append(node('span', publisherLabel(group.publisher), 'article-publisher-name'));
      if (group.publisher?.alias) button.append(node('span', group.publisher.alias, 'article-list-meta'));
      button.append(node('span', group.followed ? '已关注' : '本地存档', 'article-publisher-kind'));
      button.append(node('span', group.count ? group.count + ' 篇文章 · 正文已保存 ' + group.saved + ' 篇' : '暂无本地文章 · 待同步', 'article-publisher-meta'));
      button.append(node('span', group.followed ? '同步并查看 →' : '查看存档 →', 'article-publisher-open'));
      button.onclick = () => openPublisher(group.key);
      item.append(button); list.append(item);
    }
    if (!visible.length) list.append(node('li', search ? '没有匹配的公众号。' : publisherRequest ? '正在获取已关注公众号…' : '暂无可显示的公众号，请保持微信登录后刷新。', 'articles-empty'));
  }
  function choosePublisher(key) {
    const group = publisherGroups().find(group => group.key === key);
    if (!group || key === selectedPublisher) return;
    clearArticle(); selectedPublisher = key; $('#articles-search').value = '';
    $('#articles-publisher').value = key; drawList(); render(false);
  }
  async function openPublisher(key) {
    if (listRequest || publisherOpenRequest) return;
    const group = publisherGroups().find(group => group.key === key);
    if (!group) return;
    choosePublisher(key);
    const request = capture(); publisherOpenRequest = request; render(false);
    try {
      if (publisherRequest && !publishersLoaded) await publisherRequest.promise;
      if (!current(request, true) || selectedPublisher !== key) return;
      if (!publisherGroups().some(owner => owner.key === key && owner.followed)) return;
      if (!available()) { status('管理服务连接中断，可继续查看已显示内容。'); return; }
      if (savedListFailed) {
        const result = await api('articles?' + query() + '&publisher=' + encodeURIComponent(key), undefined, request.robot);
        if (!current(request, true) || selectedPublisher !== key) return;
        checkAccount(result, request);
        if (!Array.isArray(result.articles) || result.articles.some(row => !row || publisherKey(row.publisher) !== key))
          throw new Error('公众号本地文章结果不一致，请刷新重试。');
        mergeRows(result.articles);
      }
      if (!ready()) await refreshConnection(true);
      if (!current(request, true) || selectedPublisher !== key) return;
      if (ready()) await syncArticles(null, key);
      else status('微信暂未连接，可先查看本地文章；连接恢复后可点击“同步文章”。');
    } catch (error) {
      if (current(request, true)) status(error.message, true);
    } finally {
      if (publisherOpenRequest === request) { publisherOpenRequest = null; render(false); }
    }
  }
  function showPublishers() {
    if (listRequest) return;
    clearArticle(); selectedPublisher = ''; $('#articles-search').value = '';
    $('#articles-publisher-search').value = '';
    drawList(); drawPublishers(); render(false);
  }
  function drawList() {
    const list = $('#articles-list'), search = $('#articles-search').value.trim().toLowerCase();
    list.replaceChildren();
    const scoped = rows.filter(row => selectedPublisher && publisherKey(row.publisher) === selectedPublisher);
    const visible = scoped.filter(row => String(row.title || '').toLowerCase().includes(search));
    const savedBodies = scoped.filter(row => row.has_content).length;
    $('#articles-count').textContent = scoped.length ? scoped.length + ' 篇 · 正文已保存 ' + savedBodies + ' 篇' : '';
    for (const row of visible) {
      const item = node('li'), button = node('button', undefined, 'article-list-item');
      button.type = 'button'; button.setAttribute('aria-pressed', String(row.id === selected?.id));
      button.disabled = !!listRequest || !available();
      button.append(node('span', row.title || '未命名文章', 'article-list-title'));
      const meta = [publisherLabel(row.publisher), publishedAt(row.published_at)].filter(Boolean).join(' · ');
      button.append(node('span', meta, 'article-list-meta'));
      if (row.has_content) button.append(node('span', '已保存正文', 'article-list-meta'));
      button.onclick = () => openArticle(row); item.append(button); list.append(item);
    }
    if (!visible.length) list.append(node('li', search ? '没有匹配的文章。' : '暂无本地文章。', 'articles-empty'));
  }
  function safeUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && url.hostname === 'mp.weixin.qq.com' && !url.username && !url.password ? url.href : '';
    } catch { return ''; }
  }
  function clearArticle() {
    selection++; selected = article = null; articleRequest = exportRequest = null; clearMedia();
    $('#article-origin').open = false;
    $('#article-markdown').value = ''; status('', false, true); drawPreview();
  }
  function reset() {
    generation++; target = account = accountLabel = selectedPublisher = ''; connection = null; lastCheck = 0; loaded = false; rows = [];
    followedPublishers = []; publishersLoaded = false; publisherRequest = publisherOpenRequest = null;
    publisherMessage = ''; publisherError = false; savedListFailed = false;
    connectionRequest = listRequest = null; $('#articles-publisher').value = ''; $('#articles-search').value = ''; $('#articles-publisher-search').value = '';
    clearArticle(); drawList(); drawPublishers(); status(''); render(false);
  }
  function setTarget(identifier, expectedAccount = '', label = '') {
    if (identifier === target && expectedAccount === account) { accountLabel = label || account; return; }
    reset(); target = identifier; account = expectedAccount; accountLabel = label || account; render(false);
  }
  function refreshConnection(force = false) {
    if (!target || !account || !available()) return Promise.resolve();
    if (connectionRequest) return connectionRequest.promise;
    if (!force && (listRequest || articleRequest || Date.now() - lastCheck < 8000)) return Promise.resolve();
    const request = capture(); connectionRequest = request; lastCheck = Date.now();
    request.promise = (async () => { try {
      const result = await api('articles-connection', undefined, request.robot);
      if (!current(request) || (!force && (listRequest || articleRequest))) return;
      checkAccount(result, request); connection = result;
    } catch (error) {
      if (current(request) && (force || (!listRequest && !articleRequest)))
        connection = {connected: false, supported: false, reason: error.message};
    } finally {
      if (connectionRequest === request) { connectionRequest = null; render(false); }
    } })();
    return request.promise;
  }
  function loadPublishers(force = false) {
    if (!target || !account || !available() || (!force && publishersLoaded)) return Promise.resolve();
    if (publisherRequest) return publisherRequest.promise;
    const request = capture(); publisherRequest = request;
    publisherMessage = '正在获取已关注公众号…'; publisherError = false; drawPublishers(); render(false);
    request.promise = (async () => { try {
      const result = await api('articles-publishers?' + query(), undefined, request.robot);
      if (!current(request)) return;
      checkAccount(result, request);
      if (!Array.isArray(result.publishers) || result.publishers.length > 10000 || typeof result.complete !== 'boolean' ||
          result.source !== 'wechat_contacts' || result.publishers.some(owner => !owner ||
            typeof owner.username !== 'string' || !/^gh_[A-Za-z0-9_]{1,124}$/.test(owner.username) ||
            typeof owner.nickname !== 'string' || owner.nickname.length > 512 ||
            typeof owner.alias !== 'string' || owner.alias.length > 256)) throw new Error('公众号列表结果无效，请刷新重试。');
      const owners = new Map(result.publishers.map(owner => [owner.username,
        {username:owner.username, nickname:owner.nickname, alias:owner.alias}]));
      if (owners.size !== result.publishers.length) throw new Error('公众号列表身份重复，请刷新重试。');
      followedPublishers = [...owners.values()];
      publisherMessage = '已读取当前微信中的 ' + followedPublishers.length + ' 个已关注公众号。' +
        (result.complete ? '' : '列表可能尚未完整加载。');
      if (selectedPublisher && !publisherGroups().some(group => group.key === selectedPublisher)) {
        selectedPublisher = ''; clearArticle();
      }
      drawList(); drawPublishers();
    } catch (error) {
      if (current(request)) { publisherMessage = '关注列表读取失败：' + error.message + ' 已保存的文章仍可查看。'; publisherError = true; }
    } finally {
      if (publisherRequest === request) { publishersLoaded = true; publisherRequest = null; drawPublishers(); render(false); }
    } })();
    return request.promise;
  }
  async function loadSaved() {
    if (!target || !account || !available() || listRequest) return;
    const request = capture(); listRequest = request; status('正在读取本地文章…'); render(false);
    try {
      const result = await api('articles?' + query(), undefined, request.robot);
      if (!current(request)) return;
      checkAccount(result, request); mergeRows(result.articles, true); savedListFailed = false;
      status(rows.length ? '本地已保存 ' + rows.length + ' 篇文章。' : '暂无已保存文章，点击已关注公众号即可同步。');
    } catch (error) { if (current(request)) { savedListFailed = true; status(error.message, true); } }
    finally {
      if (listRequest === request) { loaded = true; listRequest = null; render(false); }
    }
  }
  async function refresh() { await Promise.all([refreshConnection(true), loadSaved(), loadPublishers(true)]); }
  async function syncArticles(event, requestedPublisher) {
    event?.preventDefault();
    const publisher = requestedPublisher || $('#articles-publisher').value.trim();
    if (!ready() || listRequest || articleRequest || !publisher) return;
    const request = capture(), received = new Set(), continuations = new Set();
    let continuation, pages = 0;
    listRequest = request; status('正在同步文章列表…'); render(false);
    try {
      while (current(request)) {
        if (!available()) throw new Error('管理服务连接中断，同步已停止。');
        if (!ready()) throw new Error('微信连接已变化，同步已停止。');
        const body = {account: request.account, publisher};
        if (continuation !== undefined) body.continuation = continuation;
        const result = await api('articles-read', body, request.robot);
        if (!current(request)) return;
        checkAccount(result, request); pages++;
        if (!Array.isArray(result.articles) || result.articles.length > maxSyncArticles ||
            result.articles.some(row => !row || typeof row.id !== 'string' || !row.id || row.id.length > 128) ||
            typeof result.has_more !== 'boolean' || typeof result.complete_remote_history !== 'boolean' ||
            typeof result.limit_reached !== 'boolean' || result.page_count !== pages ||
            !Number.isInteger(result.fetched_count) || result.fetched_count < received.size || result.fetched_count > maxSyncArticles ||
            (result.has_more && (result.complete_remote_history || result.limit_reached)))
          throw new Error('文章同步结果不完整，请重新同步。');
        const incoming = new Set([...received, ...result.articles.map(row => row.id)]);
        if (incoming.size > maxSyncArticles || result.fetched_count !== incoming.size)
          throw new Error('文章同步数量不一致，请重新同步。');
        for (const id of incoming) received.add(id);
        mergeRows(result.articles);
        if (result.publisher) $('#articles-publisher').value = publisherKey(result.publisher);
        if (pages === 1 && result.publisher) choosePublisher(publisherKey(result.publisher));
        if (!result.has_more) {
          if (result.limit_reached) status('已同步 ' + received.size + ' 篇，已达到本次同步上限，部分历史尚未获取。');
          else if (result.complete_remote_history) status(received.size ? '已同步 ' + received.size + ' 篇文章，列表同步完成。' : '该公众号暂无可同步的文章。');
          else status('已同步 ' + received.size + ' 篇文章，本次同步结束，尚未确认完整历史。');
          break;
        }
        if (pages >= maxSyncPages || received.size >= maxSyncArticles) {
          status('已同步 ' + received.size + ' 篇，已达到本次同步上限，部分历史尚未获取。');
          break;
        }
        const next = result.continuation;
        if (typeof next !== 'string' || !next || next.length > 256 || /[^\x21-\x7e]/.test(next) || continuations.has(next))
          throw new Error('文章同步进度未推进，请重新同步。');
        continuations.add(next); continuation = next;
        status('已同步 ' + received.size + ' 篇（第 ' + pages + ' 页），正在获取下一页…');
      }
    } catch (error) {
      if (current(request)) status((received.size ? '同步中断，已保存 ' + received.size + ' 篇。' : '') + error.message + ' 再次同步将从第一页开始。', true);
    }
    finally { if (listRequest === request) { loaded = true; listRequest = null; render(false); } }
  }
  function acceptArticle(value) {
    if (!value || value.id !== selected?.id) throw new Error('文章已变化，请重新选择。');
    article = value; clearMedia();
    $('#article-markdown').value = typeof article.markdown === 'string' ? article.markdown : '';
    drawPreview(); render(false);
  }
  async function fetchArticle(request, chosen) {
    status('正在让微信加载正文…', false, true);
    const result = await api('article-read', {account: request.account, publisher: publisherKey(chosen.publisher), url: chosen.url}, request.robot);
    if (!current(request, true)) return;
    checkAccount(result, request); acceptArticle(result.article);
    mergeRows([{...chosen, title: article.title, url: article.url, published_at: article.published_at,
      publisher: article.publisher, has_content: !!article.markdown}]);
    status(article.markdown ? '正文已保存到本地。' : '暂未取得正文，请点击“读取正文”重试。', false, true);
  }
  async function openArticle(row) {
    if (!available() || !account || listRequest || publisherKey(row.publisher) !== selectedPublisher) return;
    clearArticle(); selected = row; const request = capture(); articleRequest = request;
    drawList(); status('正在打开文章…', false, true); render(false);
    try {
      const result = await api('article?id=' + encodeURIComponent(row.id) + '&' + query(), undefined, request.robot);
      if (!current(request, true)) return;
      checkAccount(result, request); acceptArticle(result.article);
      if (article.markdown) status('', false, true);
      else if (ready()) await fetchArticle(request, row);
      else status('本地尚未保存正文，微信连接恢复后可点击“读取正文”重试。', false, true);
    } catch (error) { if (current(request, true)) status(error.message, true, true); }
    finally { if (articleRequest === request) { articleRequest = null; render(false); } }
  }
  async function readArticle() {
    if (!ready() || !selected?.url || articleRequest || listRequest) return;
    selection++; clearMedia();
    const request = capture(), chosen = selected; articleRequest = request;
    drawPreview();
    status('正在让微信加载正文…', false, true); render(false);
    try {
      await fetchArticle(request, chosen);
    } catch (error) { if (current(request, true)) status(error.message, true, true); }
    finally { if (articleRequest === request) { articleRequest = null; render(false); } }
  }
  function clearMedia() {
    for (const job of imageQueue) job.entry.finish();
    imageQueue = []; media.clear(); mediaViews.clear();
    // In-flight requests retain their slots until they finish. Switching
    // articles cannot multiply the number of concurrent downloads.
  }
  const currentImage = job => current(job.request, true) && article?.id === job.id && media.get(job.index) === job.entry;
  function queueImage(index, retry = false) {
    if (!article || !(article.images || []).some(image => image.index === index)) return Promise.resolve();
    const existing = media.get(index);
    if (existing && (!retry || existing.pending || existing.queued || existing.src)) return existing.promise;
    const entry = {queued: true};
    entry.promise = new Promise(resolve => { entry.finish = resolve; });
    media.set(index, entry);
    imageQueue.push({request: capture(), id: article.id, index, entry});
    paintImage(index); pumpImages();
    return entry.promise;
  }
  function pumpImages() {
    if (!available() || articleRequest) return;
    while (activeImages < imageConcurrency && imageQueue.length) {
      const job = imageQueue.shift();
      if (!currentImage(job)) { job.entry.finish(); continue; }
      job.entry.queued = false; job.entry.pending = true; activeImages++;
      paintImage(job.index);
      void downloadImage(job);
    }
  }
  function queueArticleImages() {
    if (!article?.markdown || articleRequest) return;
    for (const index of mediaViews.keys()) if (!media.has(index)) void queueImage(index);
    pumpImages();
  }
  async function downloadImage(job) {
    const {request, id, index, entry} = job;
    try {
      const result = await api('article-media', {account: request.account, id, index}, request.robot);
      if (!currentImage(job)) return;
      checkAccount(result, request);
      if (!['image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/avif'].includes(result.mime) ||
          typeof result.data_base64 !== 'string' || !result.data_base64 || result.data_base64.length > 3 * 1024 * 1024 ||
          result.data_base64.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(result.data_base64)) throw new Error('这张图片暂时无法预览。');
      entry.src = 'data:' + result.mime + ';base64,' + result.data_base64;
    } catch (error) { if (currentImage(job)) entry.error = error.message; }
    finally {
      entry.pending = false; activeImages--; entry.finish();
      if (currentImage(job)) paintImage(index);
      pumpImages();
    }
  }
  function paintImage(index) {
    const entry = media.get(index);
    for (const view of mediaViews.get(index) || []) {
      const wrapper = view.element; wrapper.replaceChildren();
      if (entry?.src) {
        const img = node('img'); img.src = entry.src; img.alt = view.caption; img.loading = 'lazy';
        const request = capture(), id = article?.id;
        img.onerror = () => {
          if (!current(request, true) || article?.id !== id || media.get(index) !== entry || !entry.src) return;
          delete entry.src; entry.error = '图片无法显示，请重试。'; paintImage(index);
        };
        wrapper.append(img, node('span', view.caption, 'article-image-caption'));
      } else if (entry?.error) {
        const button = node('button', '重试' + view.caption, 'secondary small');
        button.type = 'button'; button.disabled = !available(); button.onclick = () => queueImage(index, true);
        wrapper.append(node('span', entry.error, 'account-error'), button);
      } else {
        wrapper.append(node('span', view.caption + (entry?.pending ? '正在加载…' : '等待加载…'), 'article-image-caption'));
      }
    }
  }
  const plainPreviewText = value => value.replace(/\\([\\[\]()*_`#!])/g, '$1')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>');
  function appendTextAndImages(container, text) {
    const pattern = /!\[([^\]]*)\]\(article-image:(\d+)\)/g;
    let position = 0, match;
    while ((match = pattern.exec(text))) {
      container.append(document.createTextNode(plainPreviewText(text.slice(position, match.index))));
      const index = Number(match[2]), image = (article?.images || []).find(entry => entry.index === index);
      if (!image) container.append(document.createTextNode(plainPreviewText(match[0])));
      else {
        const wrapper = node('span', undefined, 'article-media');
        const caption = match[1] || '图片 ' + index;
        if (!mediaViews.has(index)) mediaViews.set(index, []);
        mediaViews.get(index).push({element: wrapper, caption});
        paintImage(index);
        container.append(wrapper);
      }
      position = pattern.lastIndex;
    }
    container.append(document.createTextNode(plainPreviewText(text.slice(position))));
  }
  function drawPreview() {
    mediaViews.clear();
    const preview = $('#article-preview'); preview.replaceChildren();
    const markdown = typeof article?.markdown === 'string' ? article.markdown : '';
    if (!markdown) { preview.append(node('p', '文章正文将在这里显示。', 'articles-empty')); return; }
    let paragraph = [];
    const flush = () => {
      if (!paragraph.length) return;
      const p = node('p'); appendTextAndImages(p, paragraph.join('\n')); preview.append(p); paragraph = [];
    };
    for (const line of markdown.split(/\r?\n/)) {
      const heading = /^(#{1,6})\s+(.+)$/.exec(line);
      if (/^#{1,6}\s*$/.test(line)) flush();
      else if (heading) { flush(); const h = node('h' + heading[1].length); appendTextAndImages(h, heading[2]); preview.append(h); }
      else if (!line.trim()) flush();
      else paragraph.push(line);
    }
    flush();
  }
  async function exportMarkdown() {
    if (!article?.markdown || !available() || exportRequest || articleRequest) return;
    const request = capture(), id = article.id; exportRequest = request; render(false);
    try {
      const result = await api('article-export', {account: request.account, id}, request.robot);
      if (!current(request, true) || article?.id !== id) return;
      checkAccount(result, request);
      if (typeof result.markdown !== 'string' || typeof result.filename !== 'string') throw new Error('导出内容不完整，请重试。');
      const url = URL.createObjectURL(new Blob([result.markdown], {type: 'text/markdown;charset=utf-8'}));
      const link = node('a'); link.href = url;
      link.download = result.filename.replace(/[\\/\x00-\x1f\x7f]/g, '_') || 'article.md';
      document.body.append(link);
      try { link.click(); } finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
      status('Markdown 已导出。', false, true);
    } catch (error) { if (current(request, true)) status(error.message, true, true); }
    finally { if (exportRequest === request) { exportRequest = null; render(false); } }
  }
  function render(check = true) {
    $('#articles-library').classList.toggle('hidden', !!selectedPublisher);
    $('#articles-breadcrumb').classList.toggle('hidden', !selectedPublisher);
    $('#articles-content').classList.toggle('hidden', !selectedPublisher);
    const owner = publisherGroups().find(group => group.key === selectedPublisher);
    $('#articles-current-publisher').textContent = owner ? publisherLabel(owner.publisher) : '';
    $('#articles-back').disabled = !!listRequest;
    $('#articles-publisher-search').disabled = !account;
    for (const button of $('#articles-publisher-list').querySelectorAll('button')) button.disabled = !!listRequest || !!publisherOpenRequest;
    $('#articles-publishers-status').textContent = publisherMessage;
    $('#articles-publishers-status').classList.toggle('account-error', publisherError);
    $('#articles-sync-current').disabled = !ready() || !selectedPublisher || !!listRequest || !!articleRequest || !!publisherOpenRequest;
    $('#articles-sync-current').textContent = listRequest ? '同步中…' : '同步文章';
    $('#articles-identity').textContent = account ? '当前账号：' + accountLabel : '请先选择微信账号。';
    $('#articles-availability').textContent = !available() ? '管理服务连接中断，可继续查看当前内容。' : !account ? '' : !connection ? '正在检查微信连接…' :
      ready() ? '微信已连接，可同步文章列表和加载正文。' : connection.reason || '微信未连接，可查看本地文章。';
    $('#articles-refresh').disabled = !account || !available() || !!listRequest;
    $('#articles-publisher').disabled = !account || !!listRequest;
    $('#articles-read').disabled = !ready() || !!listRequest || !!articleRequest || !!publisherOpenRequest || !$('#articles-publisher').value.trim();
    $('#articles-read').textContent = listRequest ? '同步中…' : '同步文章列表';
    for (const button of $('#articles-list').querySelectorAll('button')) button.disabled = !!listRequest || !available();
    $('#articles-search').disabled = !account;
    $('#article-empty').classList.toggle('hidden', !!selected);
    $('#article-detail').classList.toggle('hidden', !selected);
    $('#article-title').textContent = article?.title || selected?.title || '未命名文章';
    $('#article-meta').textContent = [publisherLabel(article?.publisher || selected?.publisher), publishedAt(article?.published_at || selected?.published_at)].filter(Boolean).join(' · ');
    const url = safeUrl(article?.url || selected?.url);
    const original = $('#article-original'); original.classList.toggle('hidden', !url);
    $('#article-origin').classList.toggle('hidden', !url);
    if (url) original.href = url; else original.removeAttribute('href');
    $('#article-read').disabled = !ready() || !selected?.url || !!articleRequest || !!listRequest;
    $('#article-read').textContent = articleRequest ? '加载中…' : article?.markdown ? '刷新正文' : '读取正文';
    $('#article-export').disabled = !article?.markdown || !available() || !!exportRequest || !!articleRequest;
    $('#article-export').textContent = exportRequest ? '导出中…' : '导出 Markdown';
    $('#article-markdown').readOnly = true;
    for (const button of $('#article-preview').querySelectorAll('button')) button.disabled = !available();
    queueArticleImages();
    if (check && !$('#page-articles').classList.contains('hidden') && account && available()) {
      void refreshConnection(); if (!loaded) void loadSaved(); if (!publishersLoaded) void loadPublishers();
    }
  }
  $('#articles-form').onsubmit = syncArticles;
  $('#articles-refresh').onclick = refresh;
  $('#articles-publisher').oninput = () => render(false);
  $('#articles-search').oninput = drawList;
  $('#articles-publisher-search').oninput = drawPublishers;
  $('#articles-back').onclick = showPublishers;
  $('#articles-sync-current').onclick = () => syncArticles(null, selectedPublisher);
  $('#article-read').onclick = readArticle;
  $('#article-export').onclick = exportMarkdown;
  return {setTarget, reset, render, refresh};
};
