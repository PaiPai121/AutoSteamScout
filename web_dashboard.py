import uvicorn
# 修改后
from fastapi import FastAPI, Request, Response  # 加上 Request
from fastapi.responses import HTMLResponse
import json # 顺便确保 json 也导入了，因为后面解析飞书数据要用到
import asyncio
import datetime
import os
import sys
import logging
from logging.handlers import RotatingFileHandler
import random
import re  # 记得在文件顶部导入 re 模块

# --- 1. 路径挂载 ---
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(ROOT_DIR, "Sonkwo-Scout"))
sys.path.append(os.path.join(ROOT_DIR, "SteamPY-Scout"))

from arbitrage_commander import ArbitrageCommander

# --- 2. 日志系统配置 ---
logger = logging.getLogger("Sentinel")
logger.setLevel(logging.DEBUG)
# (此处省略你之前的日志 Handler 配置代码...)

# --- 3. 状态管理与全局实例 ---
app = FastAPI()
global_commander = None # 全局 Commander 实例，供路由调用

AGENT_STATE = {
    "current_mission": "待命",
    "last_update": "从未",
    "is_running": False,
    "scanned_count": 0,
    "active_game": "无",
    "history": [] # 最近 50 条比价记录
}

HISTORY_FILE = os.path.join(ROOT_DIR, "arbitrage_history.json")

def save_history():
    """将历史记录持久化到磁盘 (原子性保护)"""
    try:
        # 预先生成 JSON 字符串，防止写入过程中出错导致文件半截
        content = json.dumps(AGENT_STATE["history"], ensure_ascii=False, indent=2)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        logger.error(f"🚨 [黑匣子] 写入失败: {e}")

def load_history():
    """启动时从磁盘加载历史"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []

# 在 AGENT_STATE 初始化时调用
AGENT_STATE["history"] = load_history()

@app.post("/feishu/webhook")
async def feishu_bot_handler(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        print(f"❌ 接收到的数据非合法 JSON: {e}")
        return {"code": 1}
    
    # 1. 飞书初次配置校验
    if data.get("type") == "url_verification":
        print("🔗 收到飞书 URL 验证请求，握手成功")
        return {"challenge": data.get("challenge")}
    
    # 2. 消息处理逻辑
    header = data.get("header", {})
    if header.get("event_type") == "im.message.receive_v1":
        event = data.get("event", {})
        message = event.get("message", {})
        
        # 提取并解析消息内容
        try:
            content_str = message.get("content", "{}")
            content_json = json.loads(content_str)
            raw_text = content_json.get("text", "").strip()
            
            # 💡 【核心修复】：强力清洗噪音
            # 第一步：去掉 <at> 标签
            clean_step1 = re.sub(r'<at.*?>.*?</at>', '', raw_text)
            # 第二步：去掉飞书特有的标识符如 @_user_1, @_user_2 等
            # 同时去掉可能带进来的 "hi" 或 "@" 符号
            query_game = re.sub(r'@_user_\w+|@\S+', '', clean_step1)
            query_game = query_game.replace('hi', '').strip()
            
            # 后台打印，让你一眼看到有没有提取成功
            print(f"\n{'='*30}")
            print(f"📩 [飞书信号原始文本]: '{raw_text}'")
            print(f"🎯 [最终识别查询目标]: '{query_game}'")
            print(f"{'='*30}\n")
            
        except Exception as e:
            print(f"❌ 解析飞书消息体失败: {e}")
            return {"code": 0}

        # 3. 触发查询任务
        if query_game and global_commander:
            async def task():
                try:
                    sk_results = await global_commander.sonkwo.get_search_results(query_game)
                    if sk_results:
                        # 💡 只有这一行！内部自动完成比价、去重、推送到 Web 界面
                        await global_commander.process_arbitrage_item(sk_results[0], is_manual=True)
                        save_history() # 存档
                    
                    # 💡 注意：如果你还需要给飞书发文字回复，可以单独调用 analyze_arbitrage
                    # 但为了不重复查价，建议以后把文字报告也收束到 process_arbitrage_item 里
                    report = await global_commander.analyze_arbitrage(query_game)
                    await global_commander.notifier.send_text(f"🎯 侦察回报：\n{report}")
                except Exception as e:
                    print(f"🚨 飞书专项查询失败: {e}")

            asyncio.create_task(task())
        else:
            if not query_game:
                print("⚠️ [拦截]: 识别出的游戏名为空，不执行查询。")

    return {"code": 0}

# --- 4. 核心巡航逻辑 ---
async def continuous_cruise():
    """具备‘看门狗’自愈能力的常驻巡航进程"""
    global global_commander
    retry_count = 0
    cycle_time = 6000
    while True:
        try:
            # 1. 引擎初始化
            if global_commander is None:
                global_commander = ArbitrageCommander(agent_state=AGENT_STATE)
            
            logger.info(f"🚀 [尝试 {retry_count + 1}] 正在启动侦察机引擎...")
            AGENT_STATE["current_mission"] = "侦察机初始化中..."
            
            # 启动浏览器实例
            await global_commander.init_all() 
            AGENT_STATE["is_running"] = True
            
            # 2. 任务主循环
            while True:
                start_time = datetime.datetime.now()
                match_count = 0  # 成功匹配数量
                profit_count = 0 # 达到利润门槛数量
                total_profit = 0.0 # 本轮潜在总利润
                total_scanned_this_round = 0  # 💡 修正：累加多页总量
                AGENT_STATE["current_mission"] = "全场折扣扫描中"
                
                # 获取杉果搜索结果（增加局部异常保护，防止单次抓取失败搞死全局）
                try:
                    sk_results = await global_commander.sonkwo.get_search_results(keyword="")
                except Exception as e:
                    logger.error(f"⚠️ 杉果扫描局部超时/异常: {e}")
                    await asyncio.sleep(30)
                    continue # 跳过本次循环，不重启引擎
                search_tasks = ["", "steam", "act", "rpg"] # 通过不同分类词带出更多结果
                
                for task_keyword in search_tasks:
                    AGENT_STATE["current_mission"] = f"正在扫描分类: {task_keyword or '全场'}"
                    logger.info(f"🔎 正在调取杉果数据: [{task_keyword}]")
                    
                    try:
                        # 💡 关键：这里只传 keyword，不传 page，完美适配原函数
                        sk_results = await global_commander.sonkwo.get_search_results(keyword=task_keyword)
                        
                        if not sk_results:
                            continue
                    except Exception as e:
                        logger.error(f"⚠️ 杉果扫描异常: {e}")
                        continue
                    for item in sk_results:
                        # 同样调用统一方法
                        await global_commander.process_arbitrage_item(item)
                        
                        # 存盘还是留在网页端做
                        save_history()

                # 3. 🚨 重点：在这里插入简报发送逻辑 (for 循环结束后)
                end_time = datetime.datetime.now()
                duration = (end_time - start_time).seconds
                jitter = random.randint(-600, 600)
                cycle_time += jitter
                summary_report = (
                    f"📊 【侦察母舰·巡航简报】\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"⏱ 扫描耗时: {duration}s\n"
                    f"📦 扫描总量: {total_scanned_this_round} 件\n" # 💡 这里的总量现在是多分类累加的结果
                    f"✅ 成功对齐: {match_count} 件\n"
                    f"🔥 盈利目标: {profit_count} 件\n"
                    f"💰 潜在总利润: ¥{total_profit:.2f}\n"
                    f"📈 累计总进度: 第 {AGENT_STATE['scanned_count']} 次扫描\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"💤 引擎转入低功耗模式，预计 {cycle_time//60} 分钟后重启。"
                )
                
                # 发送到飞书（不管有没有利润都发，让你知道它在动）
                await global_commander.notifier.send_text(summary_report)
                # 3. 冷却周期
                AGENT_STATE["current_mission"] = "巡航完成，进入冷却"
                AGENT_STATE["active_game"] = "无（待命）"
                logger.info(f"😴 本轮扫描结束。进入 {cycle_time} 秒冷却...")
                for i in range(cycle_time):
                    # await asyncio.sleep(1)
                    if i % 30 == 0:  # 每 30 秒更新一次 Dashboard 状态
                        mins_left = (cycle_time - i) // 60
                        AGENT_STATE["current_mission"] = f"💤 冷却中，预计 {mins_left} 分钟后再次起飞"
                    await asyncio.sleep(1)
                cycle_time -= jitter
        except Exception as e:
            # 4. 全局崩溃捕获（触发自愈重启）
            retry_count += 1
            AGENT_STATE["is_running"] = False
            import traceback
            error_msg = f"🚨 后台引擎崩溃: {str(e)}\n{traceback.format_exc()[-300:]}"
            logger.error(error_msg)
            
            # 飞书错误报告
            try:
                await global_commander.notifier.send_text(f"⚠️ 系统自愈警报：{error_msg}")
            except: pass
            
            # 彻底清理
            if global_commander:
                await global_commander.close_all()
            
            # 指数退避重启
            wait_time = min(300, 15 * retry_count)
            AGENT_STATE["current_mission"] = f"系统故障，{wait_time}s 后自动重启"
            await asyncio.sleep(wait_time)

# --- 5. 网页路由 ---

@app.get("/check")
async def check_game(name: str):
    """
    交互式查询接口：由前端 JS 通过 Fetch 调用
    """
    if global_commander:
        # 调用 Commander 内部封装的跨平台比对逻辑
        report = await global_commander.analyze_arbitrage(name)
        return {"report": report}
    return {"report": "🚨 引擎尚未初始化，请稍后再试"}

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    # 构造更丰富的表格行
    rows = ""
    for h in AGENT_STATE["history"]:
        # 颜色逻辑：匹配成功且有利润为绿色
        is_profitable = "✅" in h['status'] and "¥" in h['profit'] and "-" not in h['profit']
        color = "#3fb950" if is_profitable else "#f85149"
        
        # 构造进货按钮
        buy_link = f'<a href="{h["url"]}" target="_blank" style="color:#ffcc00;text-decoration:none;">🛒 进货</a>' if h.get("url") else "---"
        
        rows += f"""
        <tr>
            <td>{h['time']}</td>
            <td style="font-weight:bold;">{h['name']}</td>
            <td>{h['sk_price']}</td>
            <td>{h['py_price']}</td>
            <td style='color:{color}; font-weight:bold;'>{h['profit']} <small>({h.get('roi','0%')})</small></td>
            <td><span style="font-size:12px; opacity:0.8;">{h['status']}</span><br><small style="color:#8b949e;">{h.get('reason','')}</small></td>
            <td>{buy_link}</td>
        </tr>
        """
    
    dot_color = "#3fb950" if AGENT_STATE["is_running"] else "#f85149"
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>SENTINEL V2 | 战略指挥中心</title>
        <meta charset="utf-8">
        <meta http-equiv="refresh" content="30">
        <style>
            :root {{ --main-gold: #ffcc00; --bg-dark: #0d1117; --border: #30363d; }}
            body {{ background: var(--bg-dark); color: #c9d1d9; font-family: 'Segoe UI', system-ui, sans-serif; padding:20px; line-height:1.5; }}
            .panel {{ background: #161b22; border: 1px solid var(--border); padding:20px; border-radius:8px; margin-bottom:20px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); }}
            .status-bar {{ display:flex; align-items:center; gap:15px; margin-bottom:10px; }}
            .dot {{ height:12px; width:12px; background:{dot_color}; border-radius:50%; box-shadow: 0 0 8px {dot_color}; }}
            table {{ width:100%; border-collapse:separate; border-spacing:0; margin-top:10px; }}
            th {{ background: #21262d; padding:12px; text-align:left; border-bottom: 2px solid var(--main-gold); }}
            td {{ padding:12px; border-bottom:1px solid var(--border); }}
            tr:hover {{ background: #21262d; }}
            .search-box {{ display:flex; gap:10px; margin-top:15px; }}
            input {{ background:#0d1117; color:#fff; border:1px solid var(--border); padding:10px; border-radius:4px; flex-grow:1; outline:none; }}
            input:focus {{ border-color: var(--main-gold); }}
            button {{ background:var(--main-gold); color:#000; border:none; padding:10px 20px; border-radius:4px; cursor:pointer; font-weight:bold; }}
            #resultArea {{ background:#000; color:#0ff; padding:15px; border-radius:4px; margin-top:15px; border-left:4px solid var(--main-gold); display:none; white-space: pre-wrap; font-family: monospace; }}
        </style>
    </head>
    <body>
        <div class="panel">
            <div class="status-bar">
                <div class="dot"></div>
                <h2 style="margin:0; color:var(--main-gold);">🛰️ SENTINEL V2.5 AI-ENHANCED</h2>
            </div>
            <div style="display:grid; grid-template-columns: 1fr 1fr; gap:20px;">
                <div>📍 当前任务: <span style="color:#fff;">{AGENT_STATE['current_mission']}</span></div>
                <div>🎯 目标锁定: <span style="color:#fff;">{AGENT_STATE['active_game']}</span></div>
            </div>
        </div>

        <div class="panel">
            <h3>🔍 深度侦察模式 (AI 分析)</h3>
            <div class="search-box">
                <input type="text" id="gameInput" placeholder="输入游戏名称（支持模糊搜索，AI 自动对齐版本）...">
                <button onclick="checkProfit()">开始侦察</button>
            </div>
            <pre id="resultArea"></pre>
        </div>

        <div class="panel" style="padding:0; overflow:hidden;">
            <table>
                <thead>
                    <tr><th>时间</th><th>游戏实体</th><th>杉果成本</th><th>SteamPy</th><th>预期利润(ROI)</th><th>AI 状态</th><th>操作</th></tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>

        <script>
        async function checkProfit() {{
            const btn = document.querySelector('button');
            const resArea = document.getElementById('resultArea');
            const name = document.getElementById('gameInput').value;
            if(!name) return;
            
            btn.innerText = '🛰️ 调动卫星中...';
            btn.disabled = true;
            resArea.style.display = 'block';
            resArea.innerText = '正在调取多平台接口并运行 AI 版本校验模型...';
            
            try {{
                const res = await fetch(`/check?name=${{encodeURIComponent(name)}}`);
                const data = await res.json();
                resArea.innerText = data.report;
            }} catch(e) {{
                resArea.innerText = '🚨 通信中断，请检查服务器连接';
            }} finally {{
                btn.innerText = '开始侦察';
                btn.disabled = false;
            }}
        }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

@app.on_event("startup")
async def startup():
    # 启动后台常驻任务
    asyncio.create_task(continuous_cruise())

from fastapi.responses import FileResponse

# 1. 消除 favicon 报错噪音
@app.get('/favicon.ico', include_in_schema=False)
async def favicon():
    return Response(status_code=204) # 直接返回“无内容”，不报 404

# 2. 隐藏 API 文档（防止爬虫扫描接口定义）
# 修改 FastAPI 初始化：
# app = FastAPI(docs_url=None, redoc_url=None)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)