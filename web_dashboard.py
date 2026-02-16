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
                    print(f"🚀 [专项任务启动]: 正在侦察游戏 [{query_game}]...")
                    # 执行分析
                    report = await global_commander.analyze_arbitrage(query_game)
                    
                    # 发送回馈
                    await global_commander.notifier.send_text(f"🎯 侦察回报：\n{report}")
                    print(f"✅ [任务完成]: 报告已成功发回飞书群。")
                except Exception as task_e:
                    print(f"🚨 [专项任务崩溃]: {task_e}")

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
                global_commander = ArbitrageCommander()
            
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
                        total_scanned_this_round += 1 # 💡 累加总量
                        sk_name = item['title']
                        # 清理价格
                        try:
                            sk_price_raw = item['price'].replace('￥','').replace('券后价','').strip()
                            sk_price = float(sk_price_raw) if sk_price_raw else 0.0
                        except: continue

                        AGENT_STATE["active_game"] = sk_name
                        
                        # A. AI 优化关键词
                        await asyncio.sleep(1.5) 
                        clean_keyword = global_commander.ai.get_search_keyword(sk_name)
                        
                        # B. SteamPy 查价 (持有 Lock)
                        async with global_commander.lock:
                            py_data = await global_commander.steampy.get_game_market_price_with_name(clean_keyword)
                        
                        # C. 比价逻辑
                        profit_str = "---"
                        status_text = "⚠️ 未搜到"
                        py_price_display = "---"
                        
                        if py_data:
                            py_price, py_match_name = py_data
                            py_price_display = f"¥{py_price}"
                            
                            await asyncio.sleep(1.2)
                            is_match = global_commander.ai.verify_version(sk_name, py_match_name)
                            
                            if is_match:
                                net_profit = (py_price * 0.97) - sk_price
                                profit_str = f"¥{net_profit:.2f}"
                                status_text = "✅ 匹配成功"
                                
                                if net_profit >= global_commander.min_profit:
                                    logger.info(f"🔥 发现利润点: {sk_name} | 预计赚: {profit_str}")
                                    # 飞书报报
                                    global_commander.notifier.send_arbitrage_report([{
                                        "title": sk_name, "sk_price": sk_price, 
                                        "py_price": py_price, "profit": net_profit, 
                                        "url": item.get('url', "")
                                    }])
                            else:
                                status_text = "🛑 版本拦截"
                                profit_str = "0.00"
                        
                        # 更新 Dashboard 状态
                        log_entry = {
                            "time": datetime.datetime.now().strftime("%H:%M:%S"),
                            "name": sk_name, "sk_price": f"¥{sk_price}",
                            "py_price": py_price_display, "profit": profit_str,
                            "status": status_text
                        }
                        AGENT_STATE["history"].insert(0, log_entry)
                        AGENT_STATE["history"] = AGENT_STATE["history"][:50]
                        AGENT_STATE["scanned_count"] += 1
                        AGENT_STATE["last_update"] = log_entry["time"]
                        logger.info(f"📊 进度 [{AGENT_STATE['scanned_count']}]: {sk_name} -> {status_text}")
                        if py_data and is_match:
                            match_count += 1
                            if net_profit >= global_commander.min_profit:
                                profit_count += 1
                                total_profit += net_profit

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
    # 生成历史记录表格行
    rows = ""
    for h in AGENT_STATE["history"]:
        color = "#00ff41" if "¥" in h['profit'] and "-" not in h['profit'] else "#ff4444"
        rows += f"<tr><td>{h['time']}</td><td>{h['name']}</td><td>{h['sk_price']}</td><td>{h['py_price']}</td><td style='color:{color}'>{h['profit']}</td><td>{h['status']}</td></tr>"
    
    dot_color = "#00ff41" if AGENT_STATE["is_running"] else "#ff4444"
    
    # 嵌入交互面板的 HTML
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>SENTINEL DASHBOARD</title>
        <meta charset="utf-8">
        <style>
            body {{ background:#0a0a0a; color:#00ff41; font-family:'Consolas', monospace; padding:30px; }}
            .panel {{ border:1px solid #00ff41; padding:20px; box-shadow:0 0 10px #00ff4133; margin-bottom:20px; }}
            .dot {{ height:10px; width:10px; background:{dot_color}; border-radius:50%; display:inline-block; }}
            table {{ width:100%; border-collapse:collapse; margin-top:20px; }}
            th, td {{ padding:10px; border-bottom:1px solid #1a1a1a; text-align:left; }}
            input {{ background:#000; color:#0ff; border:1px solid #00ff41; padding:8px; width:250px; }}
            button {{ background:#00ff41; color:#000; border:none; padding:8px 15px; cursor:pointer; font-weight:bold; }}
            #resultArea {{ color:#0ff; background:#111; padding:10px; border-radius:5px; margin-top:15px; border-left:3px solid #0ff; display:none; white-space: pre-wrap; }}
        </style>
    </head>
    <body>
        <div class="panel">
            <h2><span style="animation: blink 1s infinite;">🛰️</span> SENTINEL CONTROL PANEL</h2>
            <p><span class="dot"></span> 状态: {AGENT_STATE['current_mission']}</p>
            <p>锁定目标: {AGENT_STATE['active_game']}</p>
        </div>

        <div class="panel">
            <h3>🔍 专项套利侦察 (交互式)</h3>
            <input type="text" id="gameInput" placeholder="输入游戏名称...">
            <button onclick="checkProfit()">执行分析</button>
            <pre id="resultArea"></pre>
        </div>

        <table>
            <thead><tr><th>时间</th><th>游戏</th><th>杉果</th><th>SteamPy</th><th>预期利润</th><th>判定</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>

        <script>
        async function checkProfit() {{
            const btn = document.querySelector('button');
            const resArea = document.getElementById('resultArea');
            const name = document.getElementById('gameInput').value;
            
            if(!name) return;
            
            btn.innerText = '侦察中...';
            resArea.style.display = 'block';
            resArea.innerText = '🛰️ 正在调动 AI 与浏览器资源进行跨平台比对...';
            
            try {{
                const res = await fetch(`/check?name=${{encodeURIComponent(name)}}`);
                const data = await res.json();
                resArea.innerText = data.report;
            }} catch(e) {{
                resArea.innerText = '🚨 通信故障';
            }} finally {{
                btn.innerText = '执行分析';
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