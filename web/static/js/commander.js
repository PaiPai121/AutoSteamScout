async function triggerSync() {
    console.log("📡 [SENTINEL] 同步指令发射...");
    const btn = document.getElementById('syncBtn');
    if (!confirm("⚠️ 同步将接管浏览器执行审计，预计耗时1-3分钟。是否继续？")) return;
    btn.disabled = true; btn.style.opacity = '0.5'; btn.innerText = '⏳ 任务排队中...';
    try {
        const res = await fetch('/api/sync_all', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
            alert("🛰️ 指令已下达！\n母舰正在后台同步，完成后将发送飞书回执。");
        } else {
            alert("❌ 失败: " + data.msg);
        }
    } catch(e) {
        console.error(e);
        alert("🚨 信号中断：无法连接至主服务器。");
    } finally {
        setTimeout(() => {
            btn.disabled = false; btn.style.opacity = '1'; btn.innerText = '🔄 一键全平台资产同步';
        }, 5000);
    }
}

async function submitPost() {
    const status = document.getElementById('postStatus');
    const payload = {
        game: document.getElementById('postGame').value,
        key: document.getElementById('postKey').value,
        price: document.getElementById('postPrice').value
    };
    status.innerText = '📡 正在发送指令...';
    try {
        const res = await fetch('/web_post', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        status.innerHTML = `<span style="color:${data.status === 'success' ? '#3fb950' : '#f85149'}">${data.msg}</span>`;
    } catch(e) { status.innerText = '🚨 无法连接至指挥部服务器'; }
}

async function checkProfit() {
    const btn = document.querySelector('button');
    const resArea = document.getElementById('resultArea');
    const name = document.getElementById('gameInput').value;
    if(!name) return;
    btn.innerText = '🛰️ 正在调动卫星...'; btn.disabled = true;
    resArea.style.display = 'block'; resArea.innerText = '正在调取多平台接口并启动 AI 版本匹配算法，请稍候...';
    try {
        const res = await fetch(`/check?name=${encodeURIComponent(name)}`);
        const data = await res.json();
        resArea.innerText = data.report;
    } catch(e) { resArea.innerText = '🚨 信号中断：无法连接至主服务器。'; }
    finally { btn.innerText = '开始侦察'; btn.disabled = false; }
}
