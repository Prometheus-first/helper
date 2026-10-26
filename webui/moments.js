'use strict';
window.createMomentsComposer = ({api, robots, label, available}) => {
  const $ = selector => document.querySelector(selector);
  const form = $('#moments-form'), dialog = $('#moments-confirm');
  let target = '', mode = 0, generation = 0, connection = null, lastCheck = 0;
  let fetching = false, reading = false, submitting = false, images = [], preview = null, pending = null, timer = null;
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
    target = identifier; generation++; connection = null; lastCheck = 0; preview = null;
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
      if (connection && connection.session_id !== next.session_id) clearDraft('微信连接已变化，草稿已清空。');
      connection = next;
    } catch (error) {
      if (requested === target && version === generation) connection = {supported: false, reason: error.message};
    } finally { fetching = false; render(false); }
  }
  function render(check = true) {
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
    $('#moments-single-account').textContent = mode === 1 && row ? '发布账号：' + label(row) : '';
    const ready = !!row && available() && !!connection?.supported && !!connection?.session_id;
    $('#moments-availability').textContent = !available() ? '管理服务连接中断。' : !rows.length ? '请先启动并连接机器人。' : !target ? '请选择发布账号。' : !connection ? '正在检查朋友圈接口…' : ready ? '' : connection.reason || '当前微信尚不支持朋友圈发布。';
    $('#moments-counter').textContent = count() + ' / 1000 字 · ' + images.length + ' / 9 张';
    $('#moments-preview').disabled = !ready || !!pending || reading || submitting || count() > 1000 || (!$('#moments-content').value.trim() && !images.length);
    $('#moments-clear').disabled = !!pending || reading || submitting;
    $('#moments-images').disabled = !!pending || reading || submitting || images.length >= 9;
    $('#moments-content').disabled = !!pending || submitting;
    $('#moments-visibility').disabled = !!pending || submitting;
    $('#moments-publish').disabled = !!pending || submitting || !ready;
    for (const option of $('#moments-visibility').options) option.disabled = !!connection?.visibility && !connection.visibility.includes(option.value);
    if (check && !$('#page-moments').classList.contains('hidden')) void refresh();
  }
  $('#moments-account').onchange = event => { if (!pending && !submitting) choose(event.target.value); render(); };
  $('#moments-content').oninput = () => { preview = null; render(false); };
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
  window.addEventListener('pagehide', () => { if (timer) clearTimeout(timer); });
  return {render};
};
