const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

(async () => {
  const app = path.resolve(__dirname, '../../app');
  let scripts = 0;
  for (const file of ['tts_v2.html', 'voice_register.html', 'quick.html']) {
    const html = fs.readFileSync(path.join(app, file), 'utf8');
    for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
      if (match[1].trim()) { new vm.Script(match[1], { filename: file }); scripts++; }
    }
  }
  const html = fs.readFileSync(path.join(app, 'tts_v2.html'), 'utf8');
  const start = html.indexOf('async function generateStoryStreaming(');
  const end = html.indexOf('// 百科问答相关变量', start);
  assert(start >= 0 && end > start);
  const events = [], noop = () => {};
  const element = () => ({ style: {}, classList: { add: noop, remove: noop } });
  const sandbox = {
    console: { log: noop, warn: noop, error: noop }, TextEncoder, TextDecoder,
    setTimeout: callback => { callback(); return 1; }, clearTimeout: noop,
    baikeData: {}, currentStoryParams: {},
    waitForAllVideosReady: async () => events.push('wait'),
    fetch: async () => ({ ok: true, body: { getReader() {
      let sent = false;
      return { read: async () => sent ? { done: true } : (sent = true, { done: false,
        value: new TextEncoder().encode('data: {"type":"error","message":"controlled model failure"}\n\n') }) };
    } } }),
    hideDefaultDisplay: noop, updatePlayButtonsVisibility: noop,
    playParagraph: async () => events.push('play'), enterFullscreenMode: noop,
  };
  vm.createContext(sandbox);
  new vm.Script(html.slice(start, end)).runInContext(sandbox);
  await assert.rejects(() => sandbox.generateStoryStreaming('topic', 'role', 'scene', '', 'cn', false,
    element(), element(), element(), element(), element(), element(), element(), 'fixture'), /controlled model failure/);
  assert.deepEqual(events, []);
  console.log(JSON.stringify({ inline_scripts_syntax_passed: scripts, error_event_rejected_before_playback: true }));
})().catch(error => { console.error(error); process.exitCode = 1; });
