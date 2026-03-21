// --- 🛰️ SENTINEL 前端作战指令集 ---

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
    const btn = document.querySelector('.search-box .btn-gold');  // 明确选择侦察按钮
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

async function refreshDashboardData() {
    try {
        const res = await fetch('/api/history');
        const data = await res.json();

        const missionSpan = document.getElementById('current-mission-text');
        const countSpan = document.getElementById('scanned-count-text');
        if (missionSpan) missionSpan.innerText = data.current_mission;
        if (countSpan) countSpan.innerText = `第 ${data.scanned_count} 次扫描`;

        // commander.js 建议修改
        const tbody = document.getElementById('history-table-body');
        if (!tbody) return;

        let newRows = "";
        if (!data.history || data.history.length === 0) {
            newRows = "<tr><td colspan='7' style='text-align:center; padding:50px; color:#8b949e;'>🛰️ 侦察机巡航中...</td></tr>";
        } else {
            data.history.forEach(h => {
                const isProfitable = h.status.includes("✅");
                const color = isProfitable ? "#3fb950" : "#f85149";
                let starColor = "#8b949e";
                let rVal = parseFloat(h.rating?.replace('%', '') || 0);
                if (rVal >= 90) starColor = "#ffcc00";
                else if (rVal >= 80) starColor = "#3fb950";

                newRows += `
                <tr>
                    <td>${h.time || '--:--:--'}</td>
                    <td>
                        <div style="font-weight:bold; color:#f0f6fc;">${h.name}</div>
                        <div style="font-size:12px; color:${starColor}; margin-top:4px;">
                            <span>⭐ Steam 好评: ${h.rating}</span>
                        </div>
                        <div style="font-size:11px; color:#8b949e; margin-top:2px; line-height:1.4;">
                            ${h.steam_realtime && h.steam_realtime.positive_rate ? `
                                <span style="color:#ffcc00;">⭐</span> 实时：${h.steam_realtime.positive_rate} ${h.steam_realtime.positive_detail || ''}
                            ` : ''}
                        </div>
                    </td>
                    <td>${h.sk_price}</td>
                    <td style="color:#58a6ff; font-family:monospace; font-size:12px;">${h.py_price}</td>
                    <td style='color:${color}; font-weight:bold;'>${h.profit} <small>(${h.roi})</small></td>
                    <td><span style="font-size:12px; opacity:0.8;">${h.status}</span><br><small style="color:#8b949e;">原因: ${h.reason || '无'}</small></td>
                    <td><a href="${h.url}" target="_blank" style="color:#ffcc00; text-decoration:none;">🛒 进货</a></td>
                </tr>`;
            });
        }
        
        if (tbody.innerHTML !== newRows) {
            tbody.innerHTML = newRows;
        }

    } catch (e) {
        console.log("📡 [同步等待] 可能正在重启或信号干扰...");
    }
}

console.log("🚀 [SYSTEM] Commander 引擎点火...");
refreshDashboardData();
const pulse_rate = (window.SENTINEL_CONFIG && window.SENTINEL_CONFIG.radar_interval) ? window.SENTINEL_CONFIG.radar_interval : 5000;

setInterval(() => {
    console.log("📡 [RADAR] 雷达扫描中...");
    refreshDashboardData();
}, pulse_rate);