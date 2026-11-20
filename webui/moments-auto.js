'use strict';
window.createMomentsAutoComment = ({api, current, available}) => {
  const $ = selector => document.querySelector(selector);
  const form = $('#moments-auto-form');
  let identity = '', generation = 0, checkedAt = 0, fetching = false, saving = false, dirty = false;
  const fields = ['enabled', 'content', 'interval', 'authors'].map(name => $('#moments-auto-' + name));
  const status = (message, error = false) => {
    const node = $('#moments-auto-status');
    node.textContent = message; node.classList.toggle('account-error', error);
  };
  const key = () => JSON.stringify([current().robot, current().session]);
  function show(result, fill) {
    if (fill) {
      $('#moments-auto-enabled').checked = result.enabled === true;
      $('#moments-auto-content').value = result.content || '';
      $('#moments-auto-interval').value = String(result.interval_seconds || 120);
      $('#moments-auto-authors').value = (result.authors || []).join('\n');
    }
    const phases = {disabled:'自动评论已关闭。', waiting:'正在准备首次读取…',
      waiting_connection:'等待绑定的微信账号连接。', reading:'正在检查新动态…',
      baseline:'已跳过当前列表，等待新动态。', watching:'正在等待新动态。',
      commenting:'正在发送固定评论…', paused:'自动评论已暂停。', error:'自动评论检查失败。'};
    status((phases[result.phase] || '等待检查。') + ' 本次已评论 ' + (result.completed || 0) + ' 条。' +
      (result.error ? ' ' + result.error : ''), !!result.error);
  }
  async function refresh() {
    const selected = current(), version = generation, requested = key();
    if (!selected.robot || !selected.session || !available() || fetching || saving) return;
    fetching = true; checkedAt = Date.now();
    try {
      const result = await api('moments-auto-comment', undefined, selected.robot);
      if (requested === key() && version === generation) show(result, !dirty);
    } catch (error) {
      if (requested === key() && version === generation) status(error.message, true);
    } finally { fetching = false; }
  }
  function render() {
    const selected = current(), next = key();
    if (identity !== next) {
      identity = next; generation++; dirty = false; checkedAt = 0;
      $('#moments-auto-enabled').checked = false;
      $('#moments-auto-content').value = ''; $('#moments-auto-authors').value = '';
      $('#moments-auto-interval').value = '120';
      status('请选择已连接的机器人。');
    }
    const ready = !!selected.robot && !!selected.session && available() && !saving;
    for (const field of fields) field.disabled = !ready;
    if (!selected.supported && !$('#moments-auto-enabled').checked) $('#moments-auto-enabled').disabled = true;
    $('#moments-auto-save').disabled = !ready;
    if (ready && !selected.supported) status('当前微信连接尚未支持自动评论。', true);
    if (ready && Date.now() - checkedAt >= 10000 && !$('#page-moments').classList.contains('hidden')) void refresh();
  }
  for (const field of fields) field.oninput = () => { dirty = true; };
  form.onsubmit = async event => {
    event.preventDefault();
    const selected = current(), requested = key(), version = generation;
    if (saving || !selected.robot || !selected.session || !available()) return;
    const enabled = $('#moments-auto-enabled').checked;
    if (enabled && !selected.supported) return;
    const body = {session_id:selected.session, enabled, content:$('#moments-auto-content').value,
      interval_seconds:Number($('#moments-auto-interval').value),
      authors:$('#moments-auto-authors').value.split(/[\s,，]+/u).filter(Boolean)};
    saving = true; render(); status('正在保存…');
    try {
      const result = await api('moments-auto-comment', body, selected.robot);
      if (requested === key() && version === generation) { dirty = false; show(result, true); }
    } catch (error) {
      if (requested === key() && version === generation) status(error.message, true);
    } finally { saving = false; render(); }
  };
  return {render};
};
