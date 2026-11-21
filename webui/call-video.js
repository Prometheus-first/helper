'use strict';
window.createCallVideoWorkspace = ({api, available}) => {
  const $ = selector => document.querySelector(selector);
  let robot = '', account = '', generation = 0, state = null, selected = '', busy = false;
  let checking = false, previewing = false, photoSignature = '', configured = false, sequence = 0;
  let lastCheck = 0, message = '';
  let queuedVideo = '';
  let uploadJob = null;
  const visible = () => location.hash === '#call-video';
  const current = request => request.generation === generation && request.robot === robot && request.account === account;
  const capture = () => ({robot, account, generation});
  function clearPreview() {
    $('#video-preview').removeAttribute('src'); $('#video-preview').classList.add('hidden');
    $('#video-placeholder').classList.remove('hidden'); sequence = 0;
  }
  function setTarget(nextRobot, nextAccount) {
    if (nextRobot === robot && nextAccount === account) return;
    robot = nextRobot; account = nextAccount; generation++; state = null; selected = '';
    if (uploadJob) uploadJob.cancelled = true;
    uploadJob = null;
    busy = false; checking = false; previewing = false; photoSignature = ''; configured = false; lastCheck = 0; message = '';
    queuedVideo = '';
    clearPreview(); $('#video-photos').replaceChildren();
    render();
  }
  function draw() {
    const enabled = !!state?.enabled;
    const phases = {stopped:'已停止', waiting_call:'已就绪，等待微信视频通话', starting:'正在加载模型…',
      checking_models:'正在检查模型…', warming_models:'正在预热模型…', models_ready:'模型已就绪',
      camera_ready:'视频源已就绪', streaming:'实时换脸中', failed:'换脸已停止'};
    $('#video-start').disabled = busy || !available() || !state?.supported || !selected || enabled;
    $('#video-stop').disabled = busy || !available() || !enabled;
    $('#video-upload').disabled = busy || !account || !available();
    $('#video-upload-cancel').classList.toggle('hidden', !uploadJob);
    $('#video-upload-cancel').disabled = !uploadJob || uploadJob.finishing;
    $('#video-delete').disabled = busy || !selected || enabled &&
      (selected === state.photo_id || selected === state.pending_photo_id || selected === state.playing_video_id);
    for (const id of ['#video-python', '#video-repository', '#video-mode']) $(id).disabled = enabled || busy;
    let status = message || state?.error || (state?.playing_video_id ?
      (state.playback_status === 'pending' ? '正在准备视频…' : '正在播放视频，播完自动恢复换脸') :
      queuedVideo ? '等待模型和通话就绪后播放视频…' : state?.pending_photo_id ? '正在检查并切换新照片…' :
      !state?.connected ? '请先在运行概览启动当前微信账号。' : !state?.supported ? '当前微信版本不支持视频换脸。' : phases[state?.phase] || '正在读取状态…');
    $('#video-status').textContent = status;
    const ai = state?.stats?.ai;
    $('#video-playback').classList.toggle('hidden', !state?.playing_video_id);
    const playing = state?.photos?.find(row => row.id === state.playing_video_id);
    const duration = ai?.video_duration_ms || (playing?.duration || 0) * 1000;
    const position = Math.min(ai?.video_position_ms || 0, duration || Infinity);
    const clock = value => { const seconds = Math.max(0, Math.floor(value / 1000)); return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`; };
    $('#video-playback-progress').value = duration ? Math.min(100, position / duration * 100) : 0;
    $('#video-playback-time').textContent = `${clock(position)} / ${clock(duration)}`;
    $('#video-metrics').textContent = ai ? `推理 ${ai.inference_ms} ms · 延迟 ${ai.latency_ms} ms` : '';
    if (!enabled || state?.phase === 'waiting_call' || state?.phase === 'failed') clearPreview();
    const signature = JSON.stringify([state?.photos, selected, state?.photo_id, state?.pending_photo_id, state?.playing_video_id, queuedVideo, busy]);
    if (signature !== photoSignature) {
      photoSignature = signature;
      const list = $('#video-photos'); list.replaceChildren();
      for (const row of state?.photos || []) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'video-photo';
        const active = row.id === (state?.playing_video_id || selected);
        button.classList.toggle('selected', active); button.setAttribute('aria-pressed', String(active));
        const image = document.createElement('img'); image.src = row.thumbnail; image.alt = row.name;
        const label = document.createElement('span'); label.textContent = row.name;
        const badge = document.createElement('small'); badge.textContent = row.kind === 'video' ?
          (row.id === state.playing_video_id ? (state.playback_status === 'pending' ? '准备播放' : '▶ 正在播放') :
            row.id === queuedVideo ? '等待播放' : `▶ 视频 · ${Math.round(row.duration || 0)} 秒`) :
          row.id === state.photo_id && enabled ? (state.playing_video_id ? '播完恢复此图' : '正在换脸') : row.id === state.pending_photo_id ? '切换中' : '点击换脸';
        button.append(image, label, badge); button.disabled = busy || !!state?.pending_photo_id ||
          enabled && !['streaming', 'waiting_call'].includes(state.phase);
        button.onclick = () => action(async request => {
          queuedVideo = '';
          if (row.kind === 'video') {
            if (!state?.enabled) {
              await api('call-video/start', startBody(), request.robot);
              if (current(request)) queuedVideo = row.id;
            } else if (state.phase !== 'streaming') queuedVideo = row.id;
            else await api('call-video/play', {video_id:row.id,session_id:state.session_id}, request.robot);
          } else if (state?.enabled) await api('call-video/switch', {photo_id:row.id,session_id:state.session_id}, request.robot);
          if (current(request)) selected = row.id;
        });
        list.append(button);
      }
      if (!list.children.length) { const empty = document.createElement('p'); empty.className = 'note'; empty.textContent = '添加一张正面、清晰的照片开始。'; list.append(empty); }
    }
  }
  async function refresh() {
    if (!visible() || checking || !robot || !available()) return;
    const request = capture(); checking = true;
    try {
      const result = await api('call-video', undefined, request.robot);
      if (!current(request) || result.account !== account) return;
      if (state?.session_id && state.session_id !== result.session_id) { clearPreview(); selected = ''; queuedVideo = ''; }
      if (state?.playing_video_id && !result.playing_video_id) selected = result.photo_id;
      state = result;
      if (!state.enabled && !busy) queuedVideo = '';
      if (!(state.photos || []).some(row => row.id === selected)) selected = state.photo_id || state.photos?.[0]?.id || '';
      if (!configured) {
        $('#video-python').value = state.settings.python_executable;
        $('#video-repository').value = state.settings.repository;
        $('#video-mode').value = state.settings.mode; configured = true;
      }
      draw();
      if (queuedVideo && state.enabled && state.phase === 'streaming' && !busy) {
        const clip = queuedVideo; queuedVideo = '';
        action(request => api('call-video/play', {video_id:clip,session_id:state.session_id}, request.robot));
      }
    } catch (error) { if (current(request)) { message = error.message; draw(); } }
    finally { if (current(request)) checking = false; }
  }
  async function action(callback) {
    if (busy || !available()) return;
    const request = capture(); busy = true; message = ''; draw();
    try { await callback(request); }
    catch (error) { if (current(request)) message = error.message; }
    finally { if (current(request)) { busy = false; await refresh(); draw(); } }
  }
  async function preview() {
    if (!visible() || document.hidden || previewing || !state?.enabled || !available()) return;
    const request = capture(), session = state.session_id; previewing = true;
    try {
      const result = await api('call-video/preview', undefined, request.robot);
      if (!current(request) || state?.session_id !== session || !state?.enabled) return;
      if (result.image && result.sequence !== sequence) {
        sequence = result.sequence; $('#video-preview').src = result.image;
        $('#video-preview').classList.remove('hidden'); $('#video-placeholder').classList.add('hidden');
      }
    } catch { /* The status poll reports disconnection; never keep a stale image. */ if (current(request)) clearPreview(); }
    finally { if (current(request)) previewing = false; }
  }
  function startBody() {
    const images = (state?.photos || []).filter(row => row.kind !== 'video');
    const photo = images.find(row => row.id === selected) || images.find(row => row.id === state?.photo_id) || images[0];
    if (!photo) throw new Error('请先添加一张换脸图片，视频播完后会恢复到这张图片。');
    return {session_id:state.session_id, photo_id:photo.id, settings:{python_executable:$('#video-python').value.trim(),
      repository:$('#video-repository').value.trim(),mode:$('#video-mode').value}};
  }
  $('#video-start').onclick = () => action(async request => {
    await api('call-video/start', startBody(), request.robot);
    if (current(request) && state.photos.find(row=>row.id===selected)?.kind === 'video') queuedVideo = selected;
  });
  $('#video-stop').onclick = () => action(request => { queuedVideo = ''; return api('call-video/stop', {session_id:state.session_id}, request.robot); });
  $('#video-delete').onclick = () => action(request => api('call-video/delete', {photo_id:selected}, request.robot));
  $('#video-upload-cancel').onclick = () => {
    if (uploadJob && !uploadJob.finishing) { uploadJob.cancelled = true; message = '正在取消上传…'; draw(); }
  };
  $('#video-upload').onchange = event => {
    const file = event.target.files[0]; event.target.value = ''; if (!file) return;
    action(async request => {
      const read = blob => new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error('文件读取失败，请重新选择。')); reader.readAsDataURL(blob); });
      if (file.type.startsWith('video/') || /\.(mp4|mov|webm|mkv)$/i.test(file.name)) {
        if (!file.size || file.size > 128 * 1024 * 1024) throw new Error('视频须为非空且不超过 128 MiB');
        const upload = await api('call-video/upload-video', {action:'begin',name:file.name,size:file.size}, request.robot);
        const job = {cancelled:false,finishing:false};
        if (current(request)) uploadJob = job;
        draw();
        try {
          for (let offset = 0; offset < file.size; offset += upload.chunk_bytes) {
            if (!current(request) || job.cancelled) throw new Error('视频上传已取消');
            const chunk = await read(file.slice(offset, offset + upload.chunk_bytes));
            await api('call-video/upload-video', {action:'chunk',upload_id:upload.upload_id,offset,bytes:chunk.split(',')[1]}, request.robot);
            if (current(request)) { message = `视频上传 ${Math.min(100,Math.round((offset + upload.chunk_bytes) / file.size * 100))}%`; draw(); }
          }
          if (!current(request) || job.cancelled) throw new Error('视频上传已取消');
          job.finishing = true;
          if (current(request)) { message = '上传完成，正在读取视频信息…'; draw(); }
          const result = await api('call-video/upload-video', {action:'finish',upload_id:upload.upload_id}, request.robot);
          if (current(request)) { selected = result.video_id; message = ''; }
        } catch(error) {
          try { await api('call-video/upload-video', {action:'cancel',upload_id:upload.upload_id}, request.robot); } catch {}
          throw error;
        } finally {
          if (current(request) && uploadJob === job) { uploadJob = null; draw(); }
        }
        return;
      }
      if (file.size > 8 * 1024 * 1024) throw new Error('照片不能超过 8 MiB');
      const data_url = await read(file);
      if (!current(request)) return;
      const result = await api('call-video/upload', {name:file.name, data_url}, request.robot);
      if (!current(request)) return;
      selected = result.photo_id;
      if (state?.enabled && ['streaming', 'waiting_call'].includes(state.phase))
        await api('call-video/switch', {photo_id:selected, session_id:state.session_id}, request.robot);
    });
  };
  function render() { if (visible()) { draw(); if (Date.now() - lastCheck > 500) { lastCheck = Date.now(); refresh(); } } }
  setInterval(() => { render(); }, 1000);
  setInterval(preview, 160);
  return {setTarget, render};
};
