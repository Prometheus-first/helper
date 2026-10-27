'use strict';
window.createMomentsComposer = ({api, robots, label, available}) => {
  const $ = selector => document.querySelector(selector);
  const form = $('#moments-form'), dialog = $('#moments-confirm');
  let target = '', mode = 0, generation = 0, connection = null, lastCheck = 0;
  let fetching = false, reading = false, submitting = false, images = [], preview = null, pending = null, timer = null;
  let feedBusy = false, feedGeneration = 0;
  const interactionJobs = new Map(), likedEntries = new Set(), feedEntries = new Set(), interactionTimers = new Set();
  const interactionKey = (robot, session, id) => JSON.stringify([robot, session, id]);
  const feedStatus = (message, error = false) => {
    const node = $('#moments-read-status'); node.textContent = message; node.classList.toggle('account-error', error);
  };
  function clearFeed() {
    feedGeneration++; feedBusy = false; feedEntries.clear();
    $('#moments-timeline-list').replaceChildren(); feedStatus('尚未读取。');
  }
  const live = () => robots().filter(row => row.state === 'running' && row.connected);
  const count = () => [...$('#moments-content').value].length;
  const feedback = (message, error = false) => {
    const node = $('#moments-feedback'); node.textContent = message; node.classList.toggle('account-error', error);
  };
  function clearDraft(message = '') {
    generation++;
    images = []; preview = null; $('#moments-content').value = ''; $('#moments-images').value = '';
    if (dialog.open) dialog.close(); drawImages(); feedback(message);
  }
  function choose(identifier) {
    if (identifier === target) return;
    const hadAccount = !!target;
    target = identifier; generation++; connection = null; lastCheck = 0; preview = null; clearFeed();
    if (hadAccount) clearDraft('发布账号已变化，草稿已清空。');
  }
  function drawImages(container = $('#moments-image-list'), readonly = false) {
    container.replaceChildren();
    for (const [index, entry] of images.entries()) {
      const figure = document.createElement('figure'), image = document.createElement('img');
      image.src = entry.data_url; image.alt = entry.name; figure.append(image);
      if (!readonly) {
        const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'secondary small';
        remove.textContent = '移除'; remove.setAttribute('aria-label', '移除图片 ' + (index + 1));
        remove.disabled = !!pending || submitting;
        remove.onclick = () => { images.splice(index, 1); drawImages(); render(); };
        figure.append(remove);
      }
      container.append(figure);
    }
  }
  async function refresh(force = false) {
    if (!target || !available() || fetching || (!force && Date.now() - lastCheck < 4000)) return;
    const requested = target, version = generation; fetching = true; lastCheck = Date.now();
    try {
      const next = await api('moments-connection', undefined, requested);
      if (requested !== target || version !== generation) return;
      if (connection && connection.session_id !== next.session_id) { clearDraft('微信连接已变化，草稿已清空。'); clearFeed(); }
      connection = next;
    } catch (error) {
      if (requested === target && version === generation) connection = {supported: false, reason: error.message};
    } finally { fetching = false; render(false); }
  }
  function render(check = true) {
    if (!available() && (feedBusy || $('#moments-timeline-list').childElementCount)) clearFeed();
    const rows = live(), nextMode = rows.length === 1 ? 1 : rows.length > 1 ? 2 : 0;
    if (!pending && !submitting) {
      if (nextMode === 1) choose(rows[0].id);
      else if (nextMode === 0 || nextMode !== mode || !rows.some(row => row.id === target)) choose('');
    }
    mode = nextMode;
    const select = $('#moments-account'), options = [{id: '', name: '选择运行中的机器人'}, ...rows];
    const signature = JSON.stringify(options.map(row => [row.id, row.name, row.account]));
    if (select.dataset.options !== signature) {
      select.dataset.options = signature;
      select.replaceChildren(...options.map(row => { const option = document.createElement('option'); option.value = row.id; option.textContent = row.id ? label(row) : row.name; return option; }));
    }
    select.value = target; select.disabled = !!pending || submitting || !available();
    $('#moments-account-field').classList.toggle('hidden', mode !== 2);
    const row = rows.find(entry => entry.id === target);
    $('#moments-single-account').textContent = mode === 1 && row ? '操作账号：' + label(row) : '';
    const ready = !!row && available() && !!connection?.supported && !!connection?.session_id;
    $('#moments-availability').textContent = !available() ? '管理服务连接中断。' : !rows.length ? '请先启动并连接机器人。' : !target ? '请选择发布账号。' : !connection ? '正在检查朋友圈接口…' : ready ? '' : connection.reason || '当前微信尚不支持朋友圈发布。';
    $('#moments-counter').textContent = count() + ' / 1000 字 · ' + images.length + ' / 9 张';
    $('#moments-preview').disabled = !ready || !!pending || reading || submitting || count() > 1000 || (!$('#moments-content').value.trim() && !images.length);
    $('#moments-clear').disabled = !!pending || reading || submitting;
    $('#moments-images').disabled = !!pending || reading || submitting || images.length >= 9;
    $('#moments-content').disabled = !!pending || submitting;
    $('#moments-visibility').disabled = !!pending || submitting;
    $('#moments-publish').disabled = !!pending || submitting || !ready;
    $('#moments-read').disabled = !ready || feedBusy;
    $('#moments-read').textContent = feedBusy ? '读取中…' : '读取列表';
    for (const entry of feedEntries) updateInteraction(entry);
    for (const option of $('#moments-visibility').options) option.disabled = !!connection?.visibility && !connection.visibility.includes(option.value);
    if (check && !$('#page-moments').classList.contains('hidden')) void refresh();
  }
  $('#moments-account').onchange = event => { if (!pending && !submitting) choose(event.target.value); render(); };
  $('#moments-content').oninput = () => { preview = null; render(false); };
  $('#moments-read').onclick = async () => {
    if (feedBusy || !target || !connection?.session_id || !available()) return;
    const requested = target, session = connection.session_id, version = ++feedGeneration;
    feedBusy = true; feedEntries.clear(); $('#moments-timeline-list').replaceChildren(); feedStatus('正在读取已加载的记录…'); render(false);
    try {
      const result = await api('moments-read', {session_id: session, limit: 20}, requested);
      if (requested !== target || version !== feedGeneration || session !== connection?.session_id) return;
      if (result.session_id !== session) throw new Error('微信连接已变化，请重新读取。');
      const list = $('#moments-timeline-list');
      for (const row of result.rows) {
        const article = document.createElement('article'); article.className = 'moments-feed-row';
        const heading = document.createElement('h3'); heading.textContent = row.nickname || row.author;
        const author = document.createElement('div'); author.className = 'contact-id'; author.textContent = row.author;
        const meta = document.createElement('div'); meta.className = 'moments-feed-meta';
        const time = document.createElement('time'); time.dateTime = row.time; time.textContent = new Date(row.timestamp * 1000).toLocaleString('zh-CN', {hour12:false});
        meta.append(time);
        if (row.is_self && row.private) meta.append(document.createTextNode(' · 仅自己可见'));
        const content = document.createElement('p'); content.className = 'moments-feed-content'; content.textContent = row.content || '（无文字）';
        article.append(heading, author, meta, content);
        if (row.media_count) { const media = document.createElement('div'); media.className = 'editor-status'; media.textContent = '媒体附件：' + row.media_count + ' 项'; article.append(media); }
        if (row.likes?.length) {
          const likes = document.createElement('p'); likes.className = 'moments-feed-interaction';
          likes.textContent = '赞：' + row.likes.map(item => item.nickname || item.username).join('、') + (row.likes_truncated ? '…' : '');
          article.append(likes);
        }
        if (row.comments?.length) {
          const comments = document.createElement('ul'); comments.className = 'moments-feed-comments';
          for (const item of row.comments) {
            const comment = document.createElement('li'), name = document.createElement('strong');
            name.textContent = item.nickname || item.username;
            comment.append(name, document.createTextNode('：' + item.content)); comments.append(comment);
          }
          if (row.comments_truncated) { const more = document.createElement('li'); more.textContent = '仅显示前 100 条已加载的评论'; comments.append(more); }
          article.append(comments);
        }
        const id = document.createElement('div'); id.className = 'contact-id'; id.textContent = '朋友圈 ID：' + row.sns_id; article.append(id);
        addInteraction(article, row, requested, session);
        list.append(article);
      }
      feedStatus(result.rows.length ? '已读取 ' + result.rows.length + ' 条' + (result.truncated ? '，缓存共 ' + result.loaded_count + ' 条。' : '。') : '当前没有已加载的记录，请先在电脑微信打开朋友圈后重试。');
    } catch (error) {
      if (requested === target && version === feedGeneration) feedStatus(error.message, true);
    } finally {
      if (version === feedGeneration) { feedBusy = false; render(false); }
    }
  };
  function updateInteraction(entry) {
    const current = entry.robot === target && entry.session === connection?.session_id &&
      available() && !!connection?.interaction_supported;
    const job = interactionJobs.get(entry.key);
    const locked = !!job && ['pending', 'unknown'].includes(job.status);
    const liked = !!entry.row.raw_like_flag || likedEntries.has(entry.key) || !!job?.liked;
    entry.like.disabled = !current || locked || liked;
    entry.like.textContent = liked ? '已点赞' : '点赞';
    entry.comment.disabled = !current || locked;
    entry.input.disabled = !current || locked;
    const size = new TextEncoder().encode(entry.input.value).length;
    entry.counter.textContent = size + ' / 1023 字节';
    entry.send.disabled = !current || locked || !entry.input.value.trim() || size > 1023;
    entry.status.textContent = job?.message || '';
    entry.status.classList.toggle('account-error', !!job && ['failed', 'unknown'].includes(job.status));
  }
  function updateInteractions() { for (const entry of feedEntries) updateInteraction(entry); }
  function scheduleInteractionPoll(job) {
    const timer = setTimeout(() => { interactionTimers.delete(timer); void pollInteraction(job); }, 2000);
    interactionTimers.add(timer);
  }
  async function pollInteraction(job) {
    if (interactionJobs.get(job.key) !== job || job.status !== 'pending') return;
    if (!available()) { job.message = '连接中断，正在等待恢复。'; updateInteractions(); scheduleInteractionPoll(job); return; }
    try {
      const result = await api('moments-interaction-status?id=' + encodeURIComponent(job.id), undefined, job.robot);
      if (interactionJobs.get(job.key) !== job) return;
      if (result.status === 'pending') job.message = '正在处理…';
      else if (result.status === 'completed') {
        job.status = 'completed'; job.liked = job.action === 'like';
        job.message = job.action === 'like' ? (result.outcome === 'already_liked' ? '已点过赞。' : '已点赞。') : '评论已发送。';
        if (job.action === 'comment') for (const entry of feedEntries) {
          if (entry.key === job.key) { entry.input.value = ''; entry.editor.hidden = true; entry.comment.setAttribute('aria-expanded', 'false'); }
        }
      } else if (result.status === 'failed') {
        job.status = 'failed'; job.message = result.error || '操作失败，请核对后再试。';
      } else {
        job.status = 'unknown'; job.message = '结果未确认，请先在微信核对。';
      }
    } catch (error) {
      if ([400, 404, 409].includes(error.status)) {
        job.status = 'unknown'; job.message = '结果未确认，请先在微信核对。';
      } else job.message = '暂时无法确认结果，正在继续查询。';
    }
    if (job.liked) likedEntries.add(job.key);
    updateInteractions();
    if (job.status === 'pending') scheduleInteractionPoll(job);
  }
  async function submitInteraction(entry, action) {
    const key = entry.key, prior = interactionJobs.get(key);
    if (entry.robot !== target || entry.session !== connection?.session_id || !available() ||
        !connection?.interaction_supported || (prior && ['pending', 'unknown'].includes(prior.status)) ||
        (action === 'like' && (entry.row.raw_like_flag || likedEntries.has(key) || prior?.liked))) return;
    const content = action === 'comment' ? entry.input.value : '';
    if (action === 'comment' && (!content.trim() || new TextEncoder().encode(content).length > 1023)) return;
    const job = {id: crypto.randomUUID(), key, robot: entry.robot, session: entry.session,
      sns_id: String(entry.row.sns_id), action, status: 'pending', message: '正在提交…', liked: false};
    interactionJobs.set(key, job); updateInteractions();
    try {
      await api('moments-interact', {request_id: job.id, session_id: job.session,
        sns_id: job.sns_id, action, content}, job.robot);
    } catch (error) {
      if ([400, 401, 403, 404, 409, 422].includes(error.status)) {
        job.status = 'failed'; job.message = error.message || '请求未通过，请核对后再试。';
        updateInteractions(); return;
      }
      job.message = '请求结果未确认，正在查询。';
    }
    updateInteractions(); void pollInteraction(job);
  }
  function addInteraction(article, row, robot, session) {
    const actions = document.createElement('div'); actions.className = 'moments-feed-actions';
    const like = document.createElement('button'), comment = document.createElement('button');
    like.type = comment.type = 'button'; like.className = comment.className = 'secondary small';
    like.textContent = '点赞'; comment.textContent = '评论'; comment.setAttribute('aria-expanded', 'false'); actions.append(like, comment);
    const editor = document.createElement('div'); editor.className = 'moments-comment-editor'; editor.hidden = true;
    const input = document.createElement('textarea'); input.rows = 3; input.placeholder = '写评论';
    input.setAttribute('aria-label', '评论内容');
    const footer = document.createElement('div'); footer.className = 'moments-comment-footer';
    const counter = document.createElement('span'); counter.className = 'editor-status';
    const send = document.createElement('button'); send.type = 'button'; send.className = 'primary small'; send.textContent = '发送评论';
    footer.append(counter, send); editor.append(input, footer);
    const status = document.createElement('div'); status.className = 'editor-status'; status.setAttribute('role', 'status');
    const entry = {robot, session, row, key: interactionKey(robot, session, String(row.sns_id)),
      like, comment, input, send, editor, counter, status};
    feedEntries.add(entry); article.append(actions, editor, status);
    like.onclick = () => void submitInteraction(entry, 'like');
    comment.onclick = () => { editor.hidden = !editor.hidden; comment.setAttribute('aria-expanded', String(!editor.hidden)); if (!editor.hidden) input.focus(); };
    input.oninput = () => updateInteraction(entry);
    send.onclick = () => void submitInteraction(entry, 'comment');
    updateInteraction(entry);
  }
  $('#moments-visibility').onchange = () => { preview = null; };
  $('#moments-clear').onclick = () => { clearDraft(); render(false); };
  $('#moments-images').onchange = async event => {
    if (reading || pending || submitting) return;
    const version = generation, files = [...event.target.files]; reading = true; render(false);
    try {
      if (images.length + files.length > 9) throw new Error('最多添加 9 张图片。');
      if (images.reduce((sum, entry) => sum + entry.size, 0) + files.reduce((sum, file) => sum + file.size, 0) > 20 * 1024 * 1024) throw new Error('图片合计不能超过 20 MiB。');
      const additions = [];
      for (const file of files) {
        if (!['image/jpeg', 'image/png'].includes(file.type) || file.size > 5 * 1024 * 1024 || !file.size) throw new Error('请选择不超过 5 MiB 的 JPEG 或 PNG 图片。');
        const data_url = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('图片读取失败。')); reader.readAsDataURL(file); });
        additions.push({name: file.name, data_url, size: file.size});
      }
      if (version === generation) { images.push(...additions); preview = null; feedback(''); drawImages(); }
    } catch (error) { feedback(error.message, true); }
    finally { event.target.value = ''; reading = false; render(false); }
  };
  form.onsubmit = async event => {
    event.preventDefault(); if (pending || submitting || reading) return;
    await refresh(true); render(false);
    if ($('#moments-preview').disabled) return;
    preview = {robot: target, session_id: connection.session_id, content: $('#moments-content').value,
      visibility: $('#moments-visibility').value, images: images.map(({name, data_url}) => ({name, data_url}))};
    const row = live().find(entry => entry.id === target);
    $('#moments-preview-account').textContent = '发布账号：' + label(row);
    $('#moments-preview-visibility').textContent = preview.visibility === 'private' ? '仅自己可见' : '公开';
    $('#moments-preview-content').textContent = preview.content; drawImages($('#moments-preview-images'), true);
    $('#moments-confirm-error').classList.add('hidden'); dialog.showModal();
  };
  $('#moments-cancel').onclick = () => dialog.close();
  async function pollJob() {
    if (!pending) return;
    if (!available()) { timer = setTimeout(pollJob, 2000); return; }
    const job = pending;
    try {
      const result = await api('moments-status?id=' + encodeURIComponent(job.id), undefined, job.robot);
      if (pending !== job) return;
      if (result.status === 'pending') feedback('正在发布…');
      else {
        pending = null;
        if (result.status === 'published') { clearDraft(); feedback('已发布。'); }
        else feedback(result.status === 'failed' ? result.error || '发布失败，请核对后再操作。' : '发布结果未确认，请先在微信核对。不会自动重发。', true);
      }
    } catch { feedback('暂时无法确认发布结果，请先在微信核对。不会自动重发。', true); }
    finally { render(false); if (pending) timer = setTimeout(pollJob, 2000); }
  }
  $('#moments-publish').onclick = async () => {
    if (!preview || pending || submitting || !connection?.supported) return;
    const draft = preview;
    if (draft.robot !== target || draft.session_id !== connection.session_id) { dialog.close(); feedback('微信连接已变化，请重新预览。', true); return; }
    submitting = true; render(false);
    const job = {id: crypto.randomUUID(), robot: draft.robot};
    try {
      await api('moments-publish', {...draft, request_id: job.id}, job.robot);
      pending = job; dialog.close(); feedback('正在发布…'); void pollJob();
    } catch (error) {
      if (error.status) { $('#moments-confirm-error').textContent = error.message; $('#moments-confirm-error').classList.remove('hidden'); }
      else { pending = job; dialog.close(); feedback('请求结果未确认，正在查询，请勿重复提交。', true); void pollJob(); }
    } finally { submitting = false; render(false); }
  };
  window.addEventListener('pagehide', () => { if (timer) clearTimeout(timer); for (const item of interactionTimers) clearTimeout(item); });
  return {render};
};
