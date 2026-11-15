/* Plain-browser book reader. Pure helpers are also available to Node tests. */
(function (factory) {
  'use strict';
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', api.start, { once: true });
    else api.start();
  }
})(function () {
  'use strict';
  function containRect(container, sourceWidth, sourceHeight) {
    if (!(sourceWidth > 0 && sourceHeight > 0 && container.width > 0 && container.height > 0)) return null;
    const scale = Math.min(container.width / sourceWidth, container.height / sourceHeight);
    const width = sourceWidth * scale, height = sourceHeight * scale;
    return { left: (container.left || 0) + (container.width - width) / 2,
      top: (container.top || 0) + (container.height - height) / 2, width, height, scale };
  }
  function mapClickToSource(point, container, sourceWidth, sourceHeight) {
    const rect = containRect(container, sourceWidth, sourceHeight);
    if (!rect || point.x < rect.left || point.x > rect.left + rect.width || point.y < rect.top || point.y > rect.top + rect.height) return null;
    return { x: (point.x - rect.left) / rect.scale, y: (point.y - rect.top) / rect.scale };
  }
  function boxToDisplay(box, contract, container) {
    if (!contract || contract.coordinate_space !== 'pixel' || !Array.isArray(box) || box.length !== 4 || box.some(value => !Number.isFinite(value))) return null;
    const rect = containRect(container, contract.reference_width, contract.reference_height);
    if (!rect || box[2] <= box[0] || box[3] <= box[1]) return null;
    return { left: rect.left + box[0] * rect.scale, top: rect.top + box[1] * rect.scale,
      width: (box[2] - box[0]) * rect.scale, height: (box[3] - box[1]) * rect.scale };
  }
  function hitDetection(detections, point) {
    return (Array.isArray(detections) ? detections : []).filter(item => {
      const box = item.box;
      return Array.isArray(box) && box.length === 4 && box.every(Number.isFinite) && box[2] > box[0] && box[3] > box[1] && point.x >= box[0] && point.x <= box[2] && point.y >= box[1] && point.y <= box[3];
    }).sort((a, b) => (a.box[2] - a.box[0]) * (a.box[3] - a.box[1]) - (b.box[2] - b.box[0]) * (b.box[3] - b.box[1]))[0] || null;
  }
  function sameContext(snapshot, current) {
    if (!snapshot || !current) return false;
    if (!['bookId', 'pageId', 'mediaPath', 'epoch'].every(key => snapshot[key] === current[key])) return false;
    return snapshot.source !== 'video' || (current.source === 'video' && Number.isFinite(snapshot.frameTime) && Number.isFinite(current.frameTime) && Math.abs(snapshot.frameTime - current.frameTime) < .04);
  }
  function responseMatches(snapshot, current, response) {
    return sameContext(snapshot, current) && response && response.book_id === snapshot.bookId && response.page_id === snapshot.pageId && response.request_id === snapshot.requestId;
  }
  function shouldResume(snapshot, current, outcome) {
    outcome = outcome || {};
    return !!snapshot && snapshot.wasPlaying === true && !outcome.cancelled && !outcome.playbackRejected && sameContext(snapshot, current);
  }
  function formatTime(seconds) {
    seconds = Math.max(0, Math.floor(Number(seconds) || 0));
    return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
  }
  function start() {
    if (document.documentElement.dataset.bookReaderReady) return;
    document.documentElement.dataset.bookReaderReady = 'true';
    const $ = id => document.getElementById(id);
    const state = { bookId: '', book: null, pageIndex: 0, epoch: 0, selection: 0, mediaKey: '', source: 'image', preferVideo: true, caps: {}, jobs: new Map(), sessions: {}, sessionId: '', asking: new Set(), ignorePause: new WeakSet(), recognition: null, highlight: null, speech: null, lastSpoken: null, imageRef: null, audioRef: null, recording: null };
    const image = $('pageImage'), video = $('pageVideo'), audio = $('pageAudio');
    const idPattern = /^[A-Za-z0-9_-]{1,80}$/;
    const textStates = { needs_ocr: '待识字', ocr_failed: '识字需重试', empty: '无字页', pdf_text: '已有文字', pdf_text_unverified: '文字待核对', ocr: '已识字' };
    const bookStates = { queued: '等待整理', running: '正在整理', ready: '可以阅读', needs_ocr: '部分书页待识字', degraded: '部分内容待完善', failed: '整理需重试', interrupted: '整理已中断' };
    const jobNames = { import: '整理书页', media: '准备朗读与动画', export: '合成完整影片' };
    const jobStates = { queued: '等待中', running: '进行中', succeeded: '已完成', failed: '未完成', interrupted: '已中断', unknown: '进度暂不可读' };
    const answerNames = { model_with_validated_citations: '依据原书回答', model_with_retrieved_evidence: '依据原书回答', model_abstained: '信息还不够', no_evidence: '暂未找到依据', extractive: '原文摘录' };
    function node(tag, text, className) {
      const element = document.createElement(tag);
      if (text !== undefined && text !== null) element.textContent = String(text);
      if (className) element.className = className;
      return element;
    }
    function friendlyError(value) {
      return String(value || '暂时没有完成，请稍后重试。').slice(0, 350).replace(/[A-Za-z]:[\\/][^\s，。；\n]*/g, '本地资源').replace(/(?:sk-[A-Za-z0-9_-]{10,}|Bearer\s+[^\s]+)/gi, '凭据已隐藏');
    }
    function notice(message, error) { $('appStatus').textContent = friendlyError(message); $('appStatus').classList.toggle('error', !!error); }
    async function api(url, options) {
      const response = await fetch(url, options); let result;
      try { result = await response.json(); } catch (_) { throw new Error('服务暂时没有返回可读取的结果，请稍后重试。'); }
      if (!response.ok || result.success === false) throw new Error(friendlyError(result.error || '操作暂时没有完成。'));
      return result;
    }
    function post(url, value, signal) { return api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value), signal }); }
    function storeIds() { try { localStorage.setItem('paperbook.activeBook', state.bookId); localStorage.setItem('paperbook.sessions', JSON.stringify(state.sessions)); } catch (_) {} }
    function safeAssetURL(value, bookId) {
      if (typeof value !== 'string' || !idPattern.test(bookId || state.bookId)) return '';
      try {
        const url = new URL(value, location.origin), prefix = '/api/books/' + encodeURIComponent(bookId || state.bookId) + '/assets/';
        if (url.origin !== location.origin || !url.pathname.startsWith(prefix) || /[\\\u0000]/.test(value)) return '';
        return url.pathname + url.search;
      } catch (_) { return ''; }
    }
    function assetForRef(ref, bookId) {
      if (typeof ref !== 'string' || /[:\\\u0000]/.test(ref) || ref.startsWith('/') || ref.split('/').some(part => !part || part === '.' || part === '..')) return '';
      if (!/^(attachments|images|media)\//.test(ref)) return '';
      return safeAssetURL('/api/books/' + encodeURIComponent(bookId) + '/assets/' + ref.split('/').map(encodeURIComponent).join('/'), bookId);
    }
    function page() { return state.book && state.book.pages && state.book.pages[state.pageIndex]; }
    function context() { const current = page(); return { bookId: state.bookId, pageId: current ? current.page_id : '', mediaPath: state.mediaKey, epoch: state.epoch, source: state.source, frameTime: state.source === 'video' ? video.currentTime : 0 }; }
    function isPlaying() { return (!audio.hidden && !audio.paused && !audio.ended) || (!video.hidden && !video.paused && !video.ended); }
    function pauseOne(element) { if (!element.paused) { state.ignorePause.add(element); element.pause(); } }
    function pausePage() { pauseOne(audio); pauseOne(video); updatePlayback(); }
    function stopRecording(cancelled) {
      const recording = state.recording; if (!recording) return;
      if (cancelled) recording.cancelled = true;
      if (recording.recorder.state !== 'inactive') recording.recorder.stop();
      recording.stream.getTracks().forEach(track => track.stop());
    }
    function invalidate(reason) {
      const wasActive = !!state.recognition || !!state.speech; state.epoch++;
      state.lastSpoken = null; $('replaySpoken').hidden = true;
      if (state.recognition) state.recognition.controller.abort(); state.recognition = null;
      if (state.speech) state.speech.cancel(); state.speech = null;
      state.highlight = null; $('objectOverlay').replaceChildren(); $('cancelInteraction').hidden = true;
      if (state.imageRef && state.imageRef.origin === 'recognition') state.imageRef = null;
      if (wasActive && reason) $('recognitionStatus').textContent = '点读已取消，' + reason;
      renderPending();
    }
    function clearAttachments() { state.imageRef = null; state.audioRef = null; renderPending(); }
    function currentPlayer() { return !audio.hidden ? audio : (!video.hidden ? video : null); }
    function updatePlayback() {
      const player = currentPlayer(), duration = player && Number.isFinite(player.duration) ? player.duration : 0, current = player ? player.currentTime : 0;
      $('playPause').textContent = isPlaying() ? '暂停' : '播放'; $('seek').disabled = !duration;
      $('seek').max = String(duration || 1); $('seek').value = String(Math.min(current, duration || 1));
      $('currentTime').textContent = formatTime(current); $('duration').textContent = formatTime(duration);
      const currentPage = page(); if (!currentPage) return;
      const sentences = currentPage.media && currentPage.media.tts && currentPage.media.tts.sentences;
      const sentence = Array.isArray(sentences) && !audio.hidden ? sentences.find(row => current >= row.start && current < row.end) : null;
      $('pageSubtitle').textContent = sentence ? sentence.text : currentPage.text || (currentPage.text_status === 'empty' ? '这一页没有文字，一起看看画面吧。' : '这一页还在等待识字，可以先看看原图。');
    }
    function updateControls() {
      const current = page(), hasBook = !!state.bookId;
      const busy = [...state.jobs.values()].some(job => job.bookId === state.bookId && ['queued', 'running'].includes(job.status)), asking = state.asking.has(state.bookId);
      $('prevPage').disabled = !current || state.pageIndex === 0; $('nextPage').disabled = !current || state.pageIndex >= state.book.pages.length - 1;
      $('playPause').disabled = !current || (audio.hidden && video.hidden); $('reanalyze').disabled = !hasBook || busy;
      ['generateBook', 'generatePage', 'exportBook'].forEach(id => { $(id).disabled = !current || busy; });
      $('questionInput').disabled = !current; $('sendQuestion').disabled = !current || asking; $('newSession').disabled = !hasBook || asking;
      $('recordQuestion').disabled = !current || asking || state.caps.asr !== true || !window.MediaRecorder || !navigator.mediaDevices;
      $('audioUpload').disabled = !current || asking || state.caps.asr !== true; $('imageUpload').disabled = !current || asking;
      $('sendQuestion').textContent = asking ? '正在找依据…' : '提问'; $('recordQuestion').textContent = state.recording ? '■ 结束录音' : '🎙 语音提问';
      $('recordingStatus').textContent = state.recording ? '正在录音，点击“结束录音”后提问。' : state.caps.asr !== true ? '语音识别尚未准备，可先输入问题。' : !window.MediaRecorder || !navigator.mediaDevices ? '当前浏览器不能录音，可上传语音文件提问。' : '可以录音提问，也可以上传语音文件。'; updatePlayback();
    }
    async function playPage() {
      stopRecording(true); invalidate('已重新播放。');
      try {
        if (!audio.hidden) { await audio.play(); if (!video.hidden) { video.loop = true; await video.play(); } }
        else if (!video.hidden) { video.loop = false; await video.play(); }
        else { notice('这一页尚无朗读或动画，可以先阅读原图。'); return; }
        notice(audio.hidden ? '正在播放本页动画；这一页尚无朗读。' : '正在播放本页朗读。');
      } catch (_) { pausePage(); notice('浏览器没有允许播放，请再次点击播放按钮。', true); }
      updatePlayback();
    }
    function renderPages() {
      const list = $('pageList'); list.replaceChildren();
      const pages = state.book && state.book.pages || [];
      $('pageCount').textContent = pages.length ? pages.length + ' 页' : '尚无书页';
      if (!pages.length) { list.append(node('p', state.bookId ? '书页正在整理，完成后会出现在这里。' : '导入或选择一本绘本吧。', 'empty')); return; }
      pages.forEach((item, index) => {
        const button = node('button', null, 'page-item'); button.type = 'button';
        if (index === state.pageIndex) button.setAttribute('aria-current', 'page');
        const thumb = node('img'); thumb.src = safeAssetURL(item.image_url); thumb.alt = ''; thumb.loading = 'lazy';
        const text = node('span', '第 ' + item.page_num + ' 页'); text.append(node('small', textStates[item.text_status] || '可查看'));
        button.append(thumb, text); button.addEventListener('click', () => changePage(index)); list.append(button);
      });
    }
    function renderExports() {
      const holder = $('exportLinks'); holder.replaceChildren();
      const result = state.book && state.book.exports; if (!result) return;
      [['video_url', '完整影片', 'book.mp4'], ['srt_url', '字幕', 'subtitles.srt'], ['timeline_url', '阅读时间表', 'timeline.json']].forEach(([key, text, filename]) => {
        const url = safeAssetURL(result[key]); if (!url) return;
        const link = node('a', '下载' + text); link.href = url; link.download = filename; holder.append(link);
      });
    }
    function renderPage(force) {
      const current = page();
      $('bookTitle').textContent = state.book ? state.book.title : '故事，正等着翻开';
      $('bookState').textContent = state.book ? bookStates[state.book.status] || '可查看' : '请选择绘本';
      if (!current) {
        pausePage(); image.hidden = video.hidden = audio.hidden = true;
        $('stageEmpty').hidden = false; $('pageNumber').textContent = '— / —';
        $('pageText').textContent = ''; $('pageSubtitle').textContent = state.bookId ? '正在整理书页，完成后即可开始。' : '页面文字会显示在这里。';
        state.mediaKey = ''; updateControls(); renderExports(); return;
      }
      const tts = current.media && current.media.tts || {}, clip = current.media && current.media.video || {};
      const videoURL = clip.status === 'ready' ? safeAssetURL(clip.url) : '';
      const audioURL = ['ready', 'silent'].includes(tts.status) ? safeAssetURL(tts.url) : '';
      const imageURL = safeAssetURL(current.image_url), source = state.preferVideo && videoURL ? 'video' : 'image';
      const key = source + ':' + (source === 'video' ? videoURL : imageURL) + '|' + audioURL;
      if (force || state.mediaKey !== key) {
        pausePage(); invalidate('画面或朗读已经改变。'); state.source = source; state.mediaKey = key;
        image.hidden = source !== 'image'; video.hidden = source !== 'video'; audio.hidden = !audioURL;
        if (image.dataset.path !== imageURL) { image.src = imageURL; image.dataset.path = imageURL; }
        image.alt = '第 ' + current.page_num + ' 页绘本原图';
        if (video.dataset.path !== videoURL) { if (videoURL) video.src = videoURL; else video.removeAttribute('src'); video.dataset.path = videoURL; video.load(); }
        if (audio.dataset.path !== audioURL) { if (audioURL) audio.src = audioURL; else audio.removeAttribute('src'); audio.dataset.path = audioURL; audio.load(); }
      }
      $('stageEmpty').hidden = true;
      $('showImage').setAttribute('aria-pressed', String(source === 'image')); $('showVideo').setAttribute('aria-pressed', String(source === 'video')); $('showVideo').disabled = !videoURL;
      $('mediaState').textContent = videoURL ? '原图和动画都已准备好' : '原图阅读 · 动画尚未准备好';
      $('pageText').textContent = current.text || (current.text_status === 'empty' ? '已确认本页无文字。' : '尚未读取到可靠文字。');
      $('pageNumber').textContent = current.page_num + ' / ' + state.book.pages.length;
      $('generationHint').textContent = tts.status === 'silent' ? '这一页已确认无字，影片中会保留安静的停留。' : !audioURL ? '这一页尚无朗读；可以先读原图，或准备朗读与动画。' : '本页朗读已就绪，其他页仍可在后台准备。';
      renderExports(); updateControls();
    }
    function changePage(index) {
      if (!state.book || index < 0 || index >= state.book.pages.length) return;
      stopRecording(true); pausePage(); invalidate('已切换书页。'); clearAttachments();
      state.pageIndex = index; renderPages(); renderPage(true); $('recognitionStatus').textContent = '点击画面，可以认识里面的物体。';
    }
    async function refreshManifest(bookId) {
      const selection = state.selection, result = await api('/api/books/' + encodeURIComponent(bookId));
      if (state.bookId !== bookId || state.selection !== selection) return;
      if (state.book && state.book.updated_at && result.book.updated_at < state.book.updated_at) return;
      const currentId = page() && page().page_id; state.book = result.book;
      const retained = currentId ? state.book.pages.findIndex(row => row.page_id === currentId) : -1;
      state.pageIndex = retained >= 0 ? retained : 0; renderPages(); renderPage(false);
    }
    async function loadBooks() {
      const result = await api('/api/books'), select = $('bookSelect'); select.replaceChildren(node('option', '选择一本绘本')); select.firstChild.value = '';
      (result.books || []).forEach(book => { const option = node('option', book.title); option.value = book.book_id; select.append(option); });
      select.value = state.bookId; return result.books || [];
    }
    async function selectBook(bookId) {
      if (!bookId || !idPattern.test(bookId)) return;
      state.selection++; stopRecording(true); pausePage(); invalidate('已切换绘本。'); clearAttachments();
      state.bookId = bookId; state.book = null; state.pageIndex = 0; state.mediaKey = ''; state.preferVideo = true;
      state.sessionId = state.sessions[bookId] || ''; state.lastSpoken = null; $('replaySpoken').hidden = true;
      $('chatMessages').replaceChildren(node('p', '你想知道故事里的什么呢？', 'empty'));
      storeIds(); $('bookSelect').value = bookId; renderPages(); renderPage(true); renderTasks();
      try { await refreshManifest(bookId); if (state.bookId !== bookId) return; await restoreSession(bookId, state.selection); notice('已打开绘本。可以先阅读原页，朗读与动画不必整本等待。'); }
      catch (error) { if (state.bookId === bookId) notice(error.message, true); }
    }
    function renderCapabilities(result) {
      state.caps = result.capabilities || {}; const holder = $('capabilities'); holder.replaceChildren();
      [['ocr', '识字'], ['vision', '看图'], ['tts', '朗读'], ['video', '动画'], ['answer', '问答'], ['asr', '语音识别']].forEach(([key, label]) => {
        const ready = state.caps[key] === true; holder.append(node('span', label + ' · ' + (ready ? '已准备，连接待确认' : '未配置或未就绪'), 'capability' + (ready ? ' ready' : '')));
      }); updateControls();
    }
    function renderTasks() {
      const holder = $('taskList'); holder.replaceChildren();
      const jobs = [...state.jobs.values()].filter(job => job.bookId === state.bookId);
      if (!jobs.length) { holder.append(node('p', '暂时没有进行中的任务。', 'subtle')); return; }
      jobs.slice().reverse().forEach(job => {
        const row = node('div', null, 'task-item' + (['failed', 'interrupted', 'unknown'].includes(job.status) ? ' failed' : ''));
        row.append(node('strong', (jobNames[job.kind] || '准备内容') + ' · ' + (jobStates[job.status] || '等待中')));
        const progress = job.progress || {};
        if (progress.total > 0) { const bar = node('progress'); bar.max = progress.total; bar.value = progress.current || 0; bar.setAttribute('aria-label', '已完成 ' + bar.value + ' / ' + bar.max); row.append(bar, node('p', '已完成 ' + bar.value + ' / ' + bar.max)); }
        if (job.error) row.append(node('p', friendlyError(job.error)));
        if (['failed', 'interrupted', 'unknown'].includes(job.status)) {
          const button = node('button', job.status === 'unknown' ? '继续查看进度' : '重试', 'quiet'); button.type = 'button';
          button.addEventListener('click', async () => { button.disabled = true; try { if (job.status !== 'unknown') await post('/api/jobs/' + job.jobId + '/retry', {}); job.status = 'queued'; job.error = null; renderTasks(); pollJob(job); } catch (error) { notice(error.message, true); button.disabled = false; } }); row.append(button);
        } holder.append(row);
      }); updateControls();
    }
    async function pollJob(job) {
      if (job.polling) return; job.polling = true; let failures = 0;
      try {
        while (['queued', 'running'].includes(job.status)) {
          try {
            const result = await api('/api/jobs/' + encodeURIComponent(job.jobId)); Object.assign(job, result.job); failures = 0;
            if (state.bookId === job.bookId) { await refreshManifest(job.bookId); renderTasks(); }
            if (!['queued', 'running'].includes(job.status)) { await loadBooks(); if (state.bookId === job.bookId) notice(job.status === 'succeeded' ? (jobNames[job.kind] || '内容') + '已完成。' : friendlyError(job.error || '任务未完成，可以重试。'), job.status !== 'succeeded'); break; }
          } catch (_) { failures++; if (failures >= 3) { job.status = 'unknown'; job.error = '暂时无法读取进度，请点击继续查看。'; renderTasks(); break; } }
          await new Promise(resolve => setTimeout(resolve, 1000));
        }
      } finally { job.polling = false; updateControls(); }
    }
    function trackJob(bookId, jobId, kind) {
      if (!idPattern.test(jobId || '')) throw new Error('没有收到可用的任务编号，请刷新书架查看。');
      const job = { bookId, jobId, kind, status: 'queued', progress: {} }; state.jobs.set(jobId, job); renderTasks(); pollJob(job);
    }
    async function createMedia(wholeBook) {
      const current = page(); if (!current) return; const selected = wholeBook ? state.book.pages : [current];
      if (selected.some(item => ['needs_ocr', 'ocr_failed'].includes(item.text_status))) { notice('还有书页未识别到可靠文字，请先用“自动识字”或“仔细看图”重新整理。', true); return; }
      if (state.caps.tts !== true) { notice('朗读尚未准备好，可以先阅读原图。', true); return; }
      try {
        const bookId = state.bookId, result = await post('/api/books/' + bookId + '/media', { voice: $('voice').value, page_ids: wholeBook ? undefined : [current.page_id], force: $('forceMedia').checked });
        trackJob(bookId, result.job_id, 'media'); notice(state.caps.video === true ? '正在准备，已就绪的书页可以先播放。' : '动画尚未配置；配音可能先完成，任务如有未完成内容会明确显示。');
      } catch (error) { notice(error.message, true); }
    }
    function drawHighlight() {
      $('objectOverlay').replaceChildren(); const highlight = state.highlight;
      if (!highlight || !sameContext(highlight.snapshot, context())) return;
      const stage = $('mediaStage').getBoundingClientRect(), display = boxToDisplay(highlight.detection.box, highlight.contract, { left: 0, top: 0, width: stage.width, height: stage.height });
      if (!display) return;
      const box = node('div', null, 'object-box'); Object.assign(box.style, { left: display.left + 'px', top: display.top + 'px', width: display.width + 'px', height: display.height + 'px' });
      const label = node('span', highlight.detection.label, 'object-label'); if (display.top < 35) { label.style.bottom = 'auto'; label.style.top = '3px'; }
      box.append(label); $('objectOverlay').append(box);
    }
    async function playSpoken(text, snapshot) {
      if (state.caps.tts !== true) { $('recognitionStatus').textContent = '朗读尚未准备好，文字结果已保留。'; return; }
      const bookId = snapshot.bookId; let activeSpeech = null;
      const controller = new AbortController(), pending = { cancel() { controller.abort(); } };
      state.speech = pending; $('cancelInteraction').hidden = false;
      try {
        const result = await post('/api/books/' + bookId + '/speak', { text: String(text).slice(0, 2000), voice: $('voice').value, language: state.book && state.book.language || 'zh' }, controller.signal);
        if (!sameContext(snapshot, context())) return;
        const url = safeAssetURL(result.audio_url, bookId); if (!url) throw new Error('朗读资源暂不可用。');
        state.lastSpoken = { url, bookId, snapshot }; $('replaySpoken').hidden = false;
        const player = new Audio(url); let cancelled = false, settle;
        const ended = new Promise((resolve, reject) => { settle = resolve; player.onended = () => resolve(); player.onerror = () => reject(new Error('朗读音频没有加载成功，请点击重新播放。')); });
        const speech = { audio: player, cancel() { cancelled = true; player.pause(); settle(); } };
        activeSpeech = speech; state.speech = speech; $('cancelInteraction').hidden = false;
        try { await player.play(); } catch (_) { speech.cancel(); throw new Error('浏览器没有允许朗读，请点击“重新播放刚刚的朗读”。'); }
        await ended;
        if (state.speech === speech) state.speech = null; $('cancelInteraction').hidden = true;
        if (shouldResume(snapshot, context(), { cancelled })) await playPage();
      } catch (error) { if (sameContext(snapshot, context())) { $('recognitionStatus').textContent = friendlyError(error.message) + ' 文字结果仍在。'; notice(error.message, true); } }
      finally { if (state.speech === pending || (activeSpeech && state.speech === activeSpeech)) { state.speech = null; $('cancelInteraction').hidden = true; } }
    }
    async function recognize(event) {
      const current = page(); if (!current || state.recording) return;
      const element = state.source === 'video' ? video : image, width = state.source === 'video' ? video.videoWidth : image.naturalWidth, height = state.source === 'video' ? video.videoHeight : image.naturalHeight;
      if (!width || !height) { $('recognitionStatus').textContent = '画面还在加载，稍等一下再点。'; return; }
      const point = mapClickToSource({ x: event.clientX, y: event.clientY }, element.getBoundingClientRect(), width, height);
      if (!point) return;
      if (state.caps.vision !== true && !(state.source === 'image' && current.objects && current.objects.detections && current.objects.detections.length)) { $('recognitionStatus').textContent = '看图功能尚未准备好，可以先阅读原图。'; return; }
      const wasPlaying = isPlaying(); pausePage(); invalidate('开始新的点读。');
      const snapshot = { ...context(), wasPlaying, requestId: (globalThis.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()) }, controller = new AbortController();
      state.recognition = { controller, snapshot }; $('cancelInteraction').hidden = false; $('recognitionStatus').textContent = '正在看看你点到了什么…';
      try {
        const result = await post('/api/books/' + snapshot.bookId + '/recognize', { page_id: snapshot.pageId, source: snapshot.source, frame_time: snapshot.frameTime, request_id: snapshot.requestId }, controller.signal);
        if (!responseMatches(snapshot, context(), result)) return;
        const contract = result.objects;
        if (!contract || contract.coordinate_space !== 'pixel' || !(contract.reference_width > 0 && contract.reference_height > 0)) throw new Error('这次识别没有返回可用的位置，请重试。');
        const detection = hitDetection(contract.detections, { x: point.x / width * contract.reference_width, y: point.y / height * contract.reference_height });
        state.recognition = null; $('cancelInteraction').hidden = true;
        if (typeof result.image_ref === 'string' && result.image_ref.startsWith('attachments/') && assetForRef(result.image_ref, snapshot.bookId)) { state.imageRef = { ref: result.image_ref, bookId: snapshot.bookId, origin: 'recognition', url: safeAssetURL(result.image_url, snapshot.bookId) }; renderPending(); }
        if (!detection) { $('recognitionStatus').textContent = '这里还没有识别到物体，试试点画面中的其他地方。'; return; }
        state.highlight = { detection, contract, snapshot }; drawHighlight(); $('recognitionStatus').textContent = '你点到了：' + detection.label; await playSpoken(detection.label, snapshot);
      } catch (error) {
        if (error.name !== 'AbortError' && sameContext(snapshot, context())) { state.recognition = null; $('cancelInteraction').hidden = true; $('recognitionStatus').textContent = friendlyError(error.message) + ' 可以点播放继续阅读。'; }
      }
    }
    function renderPending() {
      const holder = $('pendingAttachments'); holder.replaceChildren();
      [['imageRef', '已附图片'], ['audioRef', '已附提问语音']].forEach(([key, label]) => {
        const attachment = state[key]; if (!attachment || attachment.bookId !== state.bookId) return;
        const row = node('div', null, 'pending-attachment'); row.append(node('span', label));
        const remove = node('button', '移除'); remove.type = 'button'; remove.addEventListener('click', () => { state[key] = null; renderPending(); }); row.append(remove); holder.append(row);
      });
    }
    function appendTurn(turn, bookId) {
      const holder = $('chatMessages'), empty = holder.querySelector('.empty'); if (empty) empty.remove();
      const row = node('article', null, 'chat-turn'); row.append(node('div', turn.question, 'chat-user'));
      if (turn.image_ref) { const url = assetForRef(turn.image_ref, bookId); if (url) { const picture = node('img'); picture.src = url; picture.alt = '这次提问附带的图片'; row.append(picture); } }
      if (turn.audio_ref) { const url = assetForRef(turn.audio_ref, bookId); if (url) { const recording = node('audio'); recording.controls = true; recording.src = url; recording.setAttribute('aria-label', '这次提问的语音'); row.append(recording); } }
      row.append(node('div', turn.answer, 'chat-answer'), node('span', answerNames[turn.answer_source] || '故事回答，请核对原页', 'answer-source'));
      (turn.citations || []).forEach(citation => {
        const source = node('div', null, 'citation'), jump = node('button', '查看第 ' + citation.page_num + ' 页'); jump.type = 'button';
        jump.addEventListener('click', () => { if (state.bookId !== bookId || !state.book) return; const index = state.book.pages.findIndex(item => item.page_id === citation.page_id); if (index >= 0) { changePage(index); $('mediaStage').focus(); } }); source.append(jump);
        const exports = state.bookId === bookId && state.book && state.book.exports, url = exports && safeAssetURL(exports.video_url, bookId), start = citation.book_start === undefined ? citation.start : citation.book_start;
        if (url && Number.isFinite(start)) { const time = node('a', '影片 ' + formatTime(start)); time.href = url + '#t=' + Math.max(0, start); time.target = '_blank'; time.rel = 'noopener'; source.append(time); }
        const detail = node('details'), summary = node('summary', '核对原文'); detail.append(summary, node('p', citation.excerpt)); source.append(detail); row.append(source);
      }); holder.append(row); holder.scrollTop = holder.scrollHeight;
    }
    async function restoreSession(bookId, selection) {
      const sessionId = state.sessions[bookId]; if (!sessionId) return;
      try {
        const result = await api('/api/books/' + bookId + '/sessions/' + encodeURIComponent(sessionId));
        if (state.bookId !== bookId || state.selection !== selection) return;
        $('chatMessages').replaceChildren(); (result.session.turns || []).forEach(turn => appendTurn(turn, bookId)); state.sessionId = sessionId;
      } catch (_) { if (state.bookId === bookId && state.selection === selection) notice('之前的对话暂时无法打开，可以开始新的提问。'); }
    }
    async function sendQuestion(question, extra) {
      question = String(question || '').trim(); const current = page();
      if (!question || !current || state.asking.has(state.bookId)) return;
      if (question.length > 4000) { notice('问题有点长，请控制在4000字以内。', true); return; }
      const wasPlaying = isPlaying(), pendingImage = state.imageRef, pendingAudio = state.audioRef;
      pausePage(); invalidate('开始提问。'); const snapshot = { ...context(), wasPlaying };
      const value = { question, session_id: state.sessionId || undefined, page_id: snapshot.pageId };
      if (snapshot.source === 'video') value.frame_time = snapshot.frameTime;
      // A page clip has no whole-film clock. Never invent a book_time.
      if (pendingImage && pendingImage.bookId === snapshot.bookId) value.image_ref = pendingImage.ref;
      if (pendingAudio && pendingAudio.bookId === snapshot.bookId) value.audio_ref = pendingAudio.ref;
      Object.assign(value, extra || {}); state.asking.add(snapshot.bookId); updateControls();
      try {
        const result = await post('/api/books/' + snapshot.bookId + '/qa', value);
        state.sessions[snapshot.bookId] = result.session_id; storeIds(); if (state.bookId !== snapshot.bookId) return;
        state.sessionId = result.session_id; appendTurn(result.turn || { question, answer: result.answer, answer_source: result.answer_source, citations: result.citations }, snapshot.bookId);
        $('questionInput').value = ''; clearAttachments(); notice('回答已保存，可以点击引用回到原页核对。');
        if ($('speakAnswers').checked && sameContext(snapshot, context())) await playSpoken(result.answer, snapshot);
      } catch (error) { if (state.bookId === snapshot.bookId) notice(error.message, true); }
      finally { state.asking.delete(snapshot.bookId); updateControls(); }
    }
    async function uploadImage(file) {
      if (!file || !page()) return; const snapshot = context(), form = new FormData(); form.append('file', file); form.append('kind', 'image');
      try {
        const result = await api('/api/books/' + snapshot.bookId + '/attachments', { method: 'POST', body: form }); if (!sameContext(snapshot, context())) return;
        if (!assetForRef(result.ref, snapshot.bookId)) throw new Error('图片引用无效，请重新上传。');
        state.imageRef = { ref: result.ref, url: safeAssetURL(result.url, snapshot.bookId), bookId: snapshot.bookId, origin: 'upload' }; renderPending(); notice('图片已附在下一次问题中。');
      } catch (error) { if (state.bookId === snapshot.bookId) notice(error.message, true); }
    }
    async function transcribeAndAsk(blob, filename, snapshot) {
      if (!sameContext(snapshot, context())) return; $('recordingStatus').textContent = '正在听你的问题…';
      const form = new FormData(); form.append('file', blob, filename); form.append('language', state.book && state.book.language || 'zh');
      try {
        const result = await api('/api/books/' + snapshot.bookId + '/transcribe', { method: 'POST', body: form }); if (!sameContext(snapshot, context())) return;
        state.audioRef = { ref: result.audio_ref, url: safeAssetURL(result.audio_url, snapshot.bookId), bookId: snapshot.bookId }; renderPending(); $('questionInput').value = result.text;
        await sendQuestion(result.text, { audio_ref: result.audio_ref });
      } catch (error) { if (state.bookId === snapshot.bookId) notice(error.message, true); }
      finally { updateControls(); }
    }
    async function toWav(blob) {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) throw new Error('浏览器暂不能转换录音，请上传 WAV 语音文件。');
      const ctx = new AudioContextClass();
      try {
        const decoded = await ctx.decodeAudioData(await blob.arrayBuffer()), source = new Float32Array(decoded.length);
        for (let channel = 0; channel < decoded.numberOfChannels; channel++) { const samples = decoded.getChannelData(channel); for (let index = 0; index < source.length; index++) source[index] += samples[index] / decoded.numberOfChannels; }
        const rate = 16000, count = Math.max(1, Math.round(source.length * rate / decoded.sampleRate)), output = new ArrayBuffer(44 + count * 2), view = new DataView(output);
        function ascii(offset, text) { for (let index = 0; index < text.length; index++) view.setUint8(offset + index, text.charCodeAt(index)); }
        ascii(0, 'RIFF'); view.setUint32(4, 36 + count * 2, true); ascii(8, 'WAVE'); ascii(12, 'fmt ');
        view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true); ascii(36, 'data'); view.setUint32(40, count * 2, true);
        for (let index = 0; index < count; index++) {
          const position = index * decoded.sampleRate / rate, left = Math.floor(position), fraction = position - left;
          const sample = (source[Math.min(left, source.length - 1)] || 0) * (1 - fraction) + (source[Math.min(left + 1, source.length - 1)] || 0) * fraction, clamped = Math.max(-1, Math.min(1, sample));
          view.setInt16(44 + index * 2, clamped < 0 ? clamped * 32768 : clamped * 32767, true);
        } return new Blob([output], { type: 'audio/wav' });
      } finally { await ctx.close(); }
    }
    async function toggleRecording() {
      if (state.recording) { stopRecording(false); return; } if (!page() || state.caps.asr !== true) return;
      pausePage(); invalidate('开始录音。'); const snapshot = context();
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        if (!sameContext(snapshot, context())) { stream.getTracks().forEach(track => track.stop()); return; }
        const preferred = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'].find(type => MediaRecorder.isTypeSupported(type)), recorder = preferred ? new MediaRecorder(stream, { mimeType: preferred }) : new MediaRecorder(stream);
        const recording = { recorder, stream, cancelled: false, snapshot, chunks: [] };
        recorder.ondataavailable = event => { if (event.data.size) recording.chunks.push(event.data); };
        recorder.onstop = async () => {
          stream.getTracks().forEach(track => track.stop()); if (state.recording === recording) state.recording = null; updateControls();
          if (recording.cancelled || !sameContext(snapshot, context())) return;
          try { const wav = await toWav(new Blob(recording.chunks, { type: recorder.mimeType })); await transcribeAndAsk(wav, 'question.wav', snapshot); }
          catch (error) { if (sameContext(snapshot, context())) notice(error.message, true); }
        };
        recorder.onerror = () => { recording.cancelled = true; stopRecording(true); notice('录音没有成功，请检查麦克风权限或上传语音文件。', true); };
        state.recording = recording; recorder.start(); updateControls();
      } catch (_) { notice('没有获得麦克风权限，可以改用上传语音文件或文字提问。', true); updateControls(); }
    }
    $('importForm').addEventListener('submit', async event => {
      event.preventDefault(); const file = $('pdfFile').files[0]; if (!file) return; $('importButton').disabled = true;
      try {
        const form = new FormData(); form.append('file', file); form.append('mode', $('readMode').value); form.append('verify_ocr', String($('verifyOcr').checked));
        const result = await api('/api/books/import', { method: 'POST', body: form });
        await loadBooks(); await selectBook(result.book_id); trackJob(result.book_id, result.job_id, 'import'); $('pdfFile').value = ''; notice('绘本已收到，正在整理书页。');
      } catch (error) { notice(error.message, true); } finally { $('importButton').disabled = false; }
    });
    $('readMode').addEventListener('change', () => { const fast = $('readMode').value === 'fast'; $('verifyOcr').disabled = fast; if (fast) $('verifyOcr').checked = false; });
    $('bookSelect').addEventListener('change', () => { if ($('bookSelect').value) selectBook($('bookSelect').value); });
    $('refreshBooks').addEventListener('click', async () => { try { await loadBooks(); if (state.bookId) await refreshManifest(state.bookId); notice('书架已刷新。'); } catch (error) { notice(error.message, true); } });
    $('reanalyze').addEventListener('click', async () => { if (!state.bookId) return; try { const bookId = state.bookId, result = await post('/api/books/' + bookId + '/analyze', { mode: $('readMode').value, verify_ocr: $('verifyOcr').checked }); trackJob(bookId, result.job_id, 'import'); } catch (error) { notice(error.message, true); } });
    $('generateBook').addEventListener('click', () => createMedia(true));
    $('generatePage').addEventListener('click', () => createMedia(false));
    $('exportBook').addEventListener('click', async () => { if (!state.bookId) return; try { const bookId = state.bookId, result = await post('/api/books/' + bookId + '/export', { force: $('forceMedia').checked }); trackJob(bookId, result.job_id, 'export'); notice('正在把书页合成一部完整影片。'); } catch (error) { notice(error.message, true); } });
    $('prevPage').addEventListener('click', () => changePage(state.pageIndex - 1)); $('nextPage').addEventListener('click', () => changePage(state.pageIndex + 1));
    $('playPause').addEventListener('click', () => { if (isPlaying()) { invalidate('已暂停播放。'); pausePage(); } else playPage(); });
    $('showImage').addEventListener('click', () => { state.preferVideo = false; renderPage(true); }); $('showVideo').addEventListener('click', () => { state.preferVideo = true; renderPage(true); });
    $('mediaStage').addEventListener('click', recognize);
    $('cancelInteraction').addEventListener('click', () => { invalidate('请点播放继续阅读。'); $('recognitionStatus').textContent = '已取消点读，请点播放继续阅读。'; });
    $('replaySpoken').addEventListener('click', async () => {
      const last = state.lastSpoken;
      if (!last || !sameContext(last.snapshot, context())) return;
      const wasPlaying = isPlaying(); pausePage(); invalidate('重新朗读。');
      const snapshot = { ...context(), wasPlaying }, player = new Audio(last.url);
      let cancelled = false, settle;
      const done = new Promise((resolve, reject) => { settle = resolve; player.onended = resolve; player.onerror = () => reject(new Error('朗读音频没有加载成功，请重试。')); });
      const speech = { cancel() { cancelled = true; player.pause(); settle(); } };
      state.speech = speech; $('cancelInteraction').hidden = false;
      state.lastSpoken = { ...last, snapshot }; $('replaySpoken').hidden = false;
      try {
        await player.play(); await done;
        if (state.speech === speech) state.speech = null;
        if (shouldResume(snapshot, context(), { cancelled })) await playPage();
      } catch (_) { speech.cancel(); if (sameContext(snapshot, context())) notice('浏览器仍未允许播放，请检查声音设置后重试。', true); }
      finally { if (state.speech === speech) state.speech = null; $('cancelInteraction').hidden = true; }
    });
    $('questionForm').addEventListener('submit', event => { event.preventDefault(); sendQuestion($('questionInput').value); });
    $('questionInput').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendQuestion(event.target.value); } });
    $('newSession').addEventListener('click', () => { if (state.asking.has(state.bookId)) return; state.sessionId = ''; delete state.sessions[state.bookId]; storeIds(); clearAttachments(); $('chatMessages').replaceChildren(node('p', '新的对话开始啦，想问些什么？', 'empty')); });
    $('recordQuestion').addEventListener('click', toggleRecording);
    $('audioUpload').addEventListener('change', event => { const file = event.target.files[0]; if (file && page()) { pausePage(); invalidate('开始语音提问。'); transcribeAndAsk(file, file.name, context()); } event.target.value = ''; });
    $('imageUpload').addEventListener('change', event => { uploadImage(event.target.files[0]); event.target.value = ''; });
    $('seek').addEventListener('input', () => {
      stopRecording(true); invalidate('播放位置已改变。'); const player = currentPlayer(); if (!player) return;
      player.currentTime = Math.min(Number($('seek').value), Number.isFinite(player.duration) ? player.duration : 0);
      if (player === audio && !video.hidden && video.duration > 0) video.currentTime = audio.currentTime % video.duration; updatePlayback();
    });
    [audio, video].forEach(element => {
      element.addEventListener('timeupdate', updatePlayback); element.addEventListener('loadedmetadata', updatePlayback);
      element.addEventListener('seeking', () => { invalidate('播放位置已改变。'); if (element === audio && !video.hidden && video.duration > 0) video.currentTime = audio.currentTime % video.duration; });
      element.addEventListener('play', () => {
        invalidate('已重新播放。');
        if (element === audio && !video.hidden && video.paused) { video.loop = true; video.play().catch(() => { notice('动画没有开始播放，朗读仍可继续。'); }); } updatePlayback();
      });
      element.addEventListener('pause', () => { if (state.ignorePause.has(element)) state.ignorePause.delete(element); else { invalidate('已暂停播放。'); if (element === audio) pauseOne(video); } updatePlayback(); });
      element.addEventListener('error', () => { if (element.dataset.path) notice(element === audio ? '本页朗读暂时无法加载，可先阅读原图。' : '本页动画暂时无法加载，可以切回原图。', true); });
    });
    audio.addEventListener('ended', () => { pauseOne(video); updatePlayback(); if (state.book && state.pageIndex + 1 < state.book.pages.length) notice('这一页读完了，可以翻到下一页。'); });
    image.addEventListener('error', () => { if (image.dataset.path) notice('本页原图暂时无法加载，请刷新或重新整理绘本。', true); });
    window.addEventListener('resize', drawHighlight);
    window.addEventListener('beforeunload', () => { stopRecording(true); invalidate('页面已关闭。'); });
    document.addEventListener('keydown', event => {
      if (['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(document.activeElement && document.activeElement.tagName)) return;
      if (event.key === 'ArrowLeft') changePage(state.pageIndex - 1); if (event.key === 'ArrowRight') changePage(state.pageIndex + 1);
    });
    (async () => {
      try { const sessions = JSON.parse(localStorage.getItem('paperbook.sessions') || '{}'); Object.entries(sessions).forEach(([bookId, sessionId]) => { if (idPattern.test(bookId) && typeof sessionId === 'string' && idPattern.test(sessionId)) state.sessions[bookId] = sessionId; }); } catch (_) {}
      try { renderCapabilities(await api('/api/capabilities')); } catch (_) { renderCapabilities({ capabilities: {} }); notice('暂时无法确认功能准备情况，仍可尝试打开已有绘本。'); }
      try {
        const books = await loadBooks(); let saved = ''; try { saved = localStorage.getItem('paperbook.activeBook') || ''; } catch (_) {}
        const selected = books.find(book => book.book_id === saved) || books[0];
        if (selected) await selectBook(selected.book_id); else notice('欢迎，导入一本 PDF 绘本开始阅读吧。');
      } catch (error) { notice(error.message, true); }
    })();
  }
  return { containRect, mapClickToSource, boxToDisplay, hitDetection, sameContext, responseMatches, shouldResume, formatTime, start };
});
