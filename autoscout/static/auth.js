const labels = {
  not_started: '尚未启动', checking_session: '检查会话', opening_login: '打开登录页',
  awaiting_phone: '等待手机号', requesting_sms: '请求验证码',
  awaiting_challenge: '等待拼图验证',
  challenge_confirmed: '拼图已确认', awaiting_code: '等待验证码',
  submitting_code: '提交验证码', cancelling: '正在取消',
  completed: '最近一次登录成功', failed: '登录未完成'
};
const previousFrames = {sonkwo: null, steampy: null};
const states = {sonkwo: null, steampy: null};
const starting = {sonkwo: false, steampy: false};
let token = null;
let pointerQueue = Promise.resolve();
let dragging = null;

function feedback(platform, message, error = false) {
  const element = document.getElementById(`${platform}-feedback`);
  element.textContent = message;
  element.classList.toggle('error', error);
}

async function postAuth(platform, action, data) {
  if (!token) throw new Error('页面尚未准备好，请稍后重试');
  const response = await fetch(`/api/auth/${platform}/${action}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Scout-Token': token},
    body: JSON.stringify(data)
  });
  if (!response.ok) {
    let detail = '请求未完成，请稍后重试';
    try { detail = (await response.json()).detail || detail; } catch (_) { /* no detail */ }
    throw new Error(typeof detail === 'string' ? detail : '输入格式有误');
  }
  return response.json();
}

async function sendPointer(platform, action, event) {
  const state = states[platform];
  const image = document.getElementById(`${platform}-frame`);
  if (!state?.interactive || !state.viewport || !token || image.hidden) return;
  const rect = image.getBoundingClientRect();
  const x = Math.min(state.viewport.width - 1, Math.max(0,
    Math.round((event.clientX - rect.left) * state.viewport.width / rect.width)));
  const y = Math.min(state.viewport.height - 1, Math.max(0,
    Math.round((event.clientY - rect.top) * state.viewport.height / rect.height)));
  pointerQueue = pointerQueue.catch(() => {}).then(async () => {
    const response = await fetch(`/api/auth/${platform}/pointer`, {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Scout-Token': token},
      body: JSON.stringify({action, x, y})
    });
    if (!response.ok) throw new Error('浏览器操作未送达，请确认登录画面仍在运行');
  });
  pointerQueue.catch(error => {
    document.getElementById(`${platform}-message`).textContent = error.message;
  });
}

for (const platform of ['sonkwo', 'steampy']) {
  const image = document.getElementById(`${platform}-frame`);
  const zoom = document.getElementById(`${platform}-zoom`);
  document.getElementById(`${platform}-start`).addEventListener('click', async () => {
    starting[platform] = true;
    feedback(platform, '正在启动无头浏览器并检查现有会话…');
    try {
      await postAuth(platform, 'start', {
        reuse_code: document.getElementById(`${platform}-reuse`).checked
      });
      await refresh(platform);
      setTimeout(() => { starting[platform] = false; refresh(platform).catch(() => {}); }, 15000);
    } catch (error) {
      starting[platform] = false;
      feedback(platform, error.message, true);
      await refresh(platform).catch(() => {});
    }
  });
  for (const kind of ['phone', 'code']) {
    document.getElementById(`${platform}-${kind}-form`).addEventListener('submit', async event => {
      event.preventDefault();
      const form = event.currentTarget;
      const button = form.querySelector('button');
      const input = document.getElementById(`${platform}-${kind}-input`);
      button.disabled = true;
      try {
        await postAuth(platform, 'input', {kind, value: input.value.trim()});
        feedback(platform, kind === 'phone'
          ? '手机号已送入浏览器，正在请求短信；若出现拼图，请在下方画面完成。'
          : '验证码已送入浏览器，正在核对登录结果。');
        if (kind === 'code') input.value = '';
      } catch (error) {
        feedback(platform, error.message, true);
      } finally {
        button.disabled = false;
      }
    });
  }
  zoom.addEventListener('click', () => {
    const enabled = image.parentElement.classList.toggle('zoomed');
    zoom.textContent = enabled ? '适应页面' : '原始大小';
  });
  document.getElementById(`${platform}-cancel`).addEventListener('click', async event => {
    if (!token) return;
    event.currentTarget.disabled = true;
    try {
      const response = await fetch(`/api/auth/${platform}/cancel`, {
        method: 'POST', headers: {'X-Scout-Token': token}
      });
      if (!response.ok) throw new Error('取消请求未送达，请刷新登录状态');
      document.getElementById(`${platform}-message`).textContent = '取消请求已送达，正在关闭无头浏览器…';
    } catch (error) {
      document.getElementById(`${platform}-message`).textContent = error.message;
      event.currentTarget.disabled = false;
    }
  });
  document.getElementById(`${platform}-continue`).addEventListener('click', async event => {
    if (!token) return;
    event.currentTarget.disabled = true;
    try {
      const response = await fetch(`/api/auth/${platform}/continue`, {
        method: 'POST', headers: {'X-Scout-Token': token}
      });
      if (!response.ok) throw new Error('继续请求未送达，请刷新登录状态');
      document.getElementById(`${platform}-message`).textContent = '已确认拼图弹窗消失；网页稍后会显示验证码输入框。';
    } catch (error) {
      document.getElementById(`${platform}-message`).textContent = error.message;
      event.currentTarget.disabled = false;
    }
  });
  image.addEventListener('pointerdown', event => {
    if (!states[platform]?.interactive) return;
    dragging = platform;
    image.setPointerCapture(event.pointerId);
    event.preventDefault();
    sendPointer(platform, 'down', event);
  });
  image.addEventListener('pointermove', event => {
    if (dragging === platform) sendPointer(platform, 'move', event);
  });
  const release = event => {
    if (dragging !== platform) return;
    dragging = null;
    sendPointer(platform, 'up', event);
  };
  image.addEventListener('pointerup', release);
  image.addEventListener('pointercancel', release);
}

async function refresh(platform) {
  const response = await fetch(`/api/auth/${platform}`, {cache: 'no-store'});
  if (!response.ok) throw new Error(`${platform} 状态暂不可读`);
  const state = await response.json();
  if (states[platform]?.stage !== state.stage && state.stage !== 'not_started') {
    feedback(platform, '');
  }
  states[platform] = state;
  if (state.active) starting[platform] = false;
  const start = document.getElementById(`${platform}-start`);
  start.disabled = !token || state.active || starting[platform];
  start.textContent = state.stage === 'completed' ? '检查当前会话' : '开始登录';
  document.getElementById(`${platform}-reuse`).disabled = state.active;
  document.getElementById(`${platform}-phone-form`).hidden = !state.active || state.stage !== 'awaiting_phone';
  document.getElementById(`${platform}-code-form`).hidden = !state.active || state.stage !== 'awaiting_code';
  const badge = document.getElementById(`${platform}-state`);
  badge.textContent = labels[state.stage] || state.stage;
  badge.className = `pill ${state.stage}`;
  document.getElementById(`${platform}-message`).textContent = state.message || '—';
  const phone = document.getElementById(`${platform}-phone`);
  phone.hidden = !state.phone_entered;
  phone.textContent = state.show_login_input
    ? '手机号已填入浏览器；当前调试模式允许显示输入内容。'
    : '手机号已填入浏览器；输入内容已遮挡。';
  document.getElementById(`${platform}-challenge`).hidden = state.stage !== 'awaiting_challenge';
  document.getElementById(`${platform}-stale`).hidden = state.active || !state.frame_at || state.stage === 'completed';
  const cancel = document.getElementById(`${platform}-cancel`);
  cancel.hidden = !state.active;
  cancel.disabled = state.stage === 'cancelling';
  const continueButton = document.getElementById(`${platform}-continue`);
  const challengeWaitMs = state.stage_at ? Date.now() - Date.parse(state.stage_at) : 0;
  continueButton.hidden = state.stage !== 'awaiting_challenge' || challengeWaitMs < 10000;
  if (state.stage !== 'awaiting_challenge') continueButton.disabled = false;
  const since = state.started_at ? `开始：${new Date(state.started_at).toLocaleString('zh-CN')}` : '';
  const frameTime = state.frame_at ? `画面：${new Date(state.frame_at).toLocaleTimeString('zh-CN')}` : '';
  const elapsed = state.active && state.started_at
    ? `已运行：${Math.max(0, Math.floor((Date.now() - Date.parse(state.started_at)) / 1000))} 秒` : '';
  document.getElementById(`${platform}-time`).textContent = `${since}  ${frameTime}  ${elapsed}`;
  const image = document.getElementById(`${platform}-frame`);
  const empty = document.getElementById(`${platform}-empty`);
  if (state.frame_at && state.frame_at !== previousFrames[platform]) {
    image.src = `/api/auth/${platform}/frame?t=${encodeURIComponent(state.frame_at)}`;
    previousFrames[platform] = state.frame_at;
  }
  image.hidden = !state.frame_at;
  empty.hidden = !!state.frame_at;
}

async function refreshAll() {
  for (const platform of ['sonkwo', 'steampy']) {
    try { await refresh(platform); }
    catch (error) { document.getElementById(`${platform}-message`).textContent = error.message; }
  }
}

fetch('/api/session').then(response => response.json()).then(data => { token = data.token; refreshAll(); });
refreshAll();
setInterval(refreshAll, 500);
