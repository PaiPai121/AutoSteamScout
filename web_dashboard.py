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
import config

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
from fastapi.templating import Jinja2Templates
# 告诉 FastAPI 模板文件在 web/templates 文件夹里
templates = Jinja2Templates(directory="web/templates")

from fastapi.staticfiles import StaticFiles

# 挂载静态文件目录
app.mount("/static", StaticFiles(directory="web/static"), name="static")

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
AGENT_STATE["history"] = [] # load_history()

def build_post_card(game_name=""):
    return {
        "config": {
            "wide_screen_mode": True
        },
        "header": {
            "title": {"tag": "plain_text", "content": "🚀 SENTINEL 上架指挥部"},
            "template": "orange"
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "plain_text", "content": "💬 请完善以下信息以执行 SteamPy 自动上架指令："}
            },
            {
                "tag": "column_set",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "input",
                                "name": "game_name_input",
                                "required": True, # 💡 尝试开启必填校验
                                "default_value": game_name,
                                "label": {"tag": "plain_text", "content": "🎮 游戏名称"},
                                "placeholder": {"tag": "plain_text", "content": "例如：街霸 6"}
                            },
                            {
                                "tag": "input",
                                "name": "cdkey_input",
                                "required": True, # 💡 尝试开启必填校验
                                "label": {"tag": "plain_text", "content": "🔑 激活码 (Key)"},
                                "placeholder": {"tag": "plain_text", "content": "请输入 AAAAA-BBBBB 格式"}
                            },
                            {
                                "tag": "input",
                                "name": "price_input",
                                "required": True, # 💡 尝试开启必填校验
                                "label": {"tag": "plain_text", "content": "💰 上架价格 (元)"},
                                "placeholder": {"tag": "plain_text", "content": "例如：88.5"}
                            }
                        ]
                    }
                ]
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "确认发布至 SteamPy"},
                        "type": "primary",
                        "value": {"action": "confirm_post"}
                    }
                ]
            }
        ]
    }


@app.post("/feishu/webhook")
async def feishu_bot_handler(request: Request):
    raw_body = await request.body()
    print(f"\n📡 [原始信号侦测] 长度: {len(raw_body)} 字节")
    try:
        data = await request.json()
        print(f"\n📡 [收到飞书信号] 类型: {data.get('header', {}).get('event_type') or data.get('type')}")
    except Exception as e:
        print(f"❌ 接收到的数据非合法 JSON: {e}")
        return {"code": 1}
    
    # 1. 飞书初次配置校验
    if data.get("type") == "url_verification":
        print("🔗 收到飞书 URL 验证请求，握手成功")
        return {"challenge": data.get("challenge")}
    # 💡 2. 新版卡片回调处理 (适配你截图中的 card.action.trigger)
    header = data.get("header", {})
    if header.get("event_type") == "card.action.trigger":
        print("🎯 [命中] 检测到卡片按钮点击")
        event = data.get("event", {})
        action_data = event.get("action", {})
        val = action_data.get("value", {})
        
        if val.get("action") == "confirm_post":
            print("🚀 正在创建后台上架任务...")
            # 拿到输入框里的值 (新版结构在 event["action"]["form_value"])
            form_vals = action_data.get("form_value", {})
            game = form_vals.get("game_name_input")
            key = form_vals.get("cdkey_input")
            price = form_vals.get("price_input")
            print(f"📝 提取表单数据: 游戏={game}, 价格={price}, Key={'已拿到' if key else '缺失'}")
            # 🚨 [关键拦截]：如果关键数据为空，直接弹窗报错而不执行后续逻辑
            if not game or not key or not price:
                print("⚠️ [拦截] 用户提交了空白表单")
                return {
                    "toast": {"type": "error", "content": "❌ 请完整填写所有信息后再提交！"},
                    # 保持卡片不变，不进入“处理中”状态
                }
            # 启动后台任务
            async def feedback_task():
                async with global_commander.lock:
                    print(f"🚀 [上架] 已抢占浏览器控制权，开始挂载: {game}")
                    success = await global_commander.steampy.action_post_flow(f"{game}|{key}|{price}")
                    status_icon = "✅" if success else "❌"
                    await global_commander.notifier.send_text(f"{status_icon} 上架反馈：{game} " + ("成功" if success else "失败"))

            asyncio.create_task(feedback_task())
            print("✅ 正在尝试向飞书返回 200 OK 响应体")
            # ⚠️ 必须返回特定的响应格式，否则飞书会报错
            return {
                "toast": {"type": "info", "content": "🛰️ 信号已接收，正在同步 SteamPy..."},
                "card": {
                    "header": {"title": {"tag": "plain_text", "content": "⏳ 指令处理中"}, "template": "blue"},
                    "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": f"正在处理：{game}\n请等待后台回执。"}}]
                }
            }
    # 2. 消息处理逻辑
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
            # 💡 [新增]：识别“上架”指令格式，例如：上架 街霸6|AAAA-BBBB-CCCC|88
            # 加在这里可以确保指令不被当作普通游戏名去杉果搜索
            # 💡 [分流识别]：区分【直接上架】与【呼叫卡片】
            is_post_cmd = query_game.startswith("上架") or query_game.lower().startswith("post")
            
            if is_post_cmd:
                print("上架")
                # 提取除去“上架”二字后的内容
                target_content = re.sub(r'^(上架|post)\s*', '', query_game, flags=re.IGNORECASE).strip()
                
                # 模式 A：检测到 "|" 分隔符，走老牌“极客直接上架”
                if "|" in target_content:
                    print(f"🚀 [飞书指令] 触发远程直接上架: {target_content}")
                    if global_commander:
                        asyncio.create_task(global_commander.steampy.action_post_flow(target_content))
                        await global_commander.notifier.send_text(f"📥 收到直接指令，执行中...")
                    return {"code": 0} # 👈 必须 return，否则会去查名为“上架 xxx|xxx”的游戏
                
                # 模式 B：通用上架卡片（包含只有“上架”二字的情况）
                else:
                    print(f"🎴 [方案 A] 准备异步推送卡片，目标内容: {target_content}")
                    if global_commander:
                        # 将提取到的内容作为默认值传给卡片输入框
                        card_payload = build_post_card(target_content)
                        asyncio.create_task(global_commander.notifier.send_card(card_payload))
                        print(f"✅ 任务已挂载至后台，正在响应飞书 ACK信号")
                    return {"code": 0} # 👈 必须 return，防止下方的杉果查询逻辑被触发
            # 后台打印，让你一眼看到有没有提取成功
            print(f"\n{'='*30}")
            # print(f"📩 [飞书信号原始文本]: '{raw_text}'")
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
                        # save_history() # 存档
                    
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
            # await global_commander.init_all() 
            # AGENT_STATE["is_running"] = True
            
            # 2. 任务主循环
            while True:
                if AGENT_STATE["is_running"]:
                    print("🧹 [清理] 正在回收旧浏览器实例，准备全新环境...")
                    await global_commander.close_all()
                    await asyncio.sleep(2) # 给系统一点缓冲时间
                
                print("🚀 [重启] 正在启动全新的侦察机引擎...")
                async with global_commander.lock:
                    print("🚀 [重启] 正在启动全新引擎...")
                    await global_commander.init_all() 
                    AGENT_STATE["is_running"] = True
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
                # search_tasks = ["", "steam", "act", "rpg"] # 通过不同分类词带出更多结果
                search_tasks = [
                        "",           # 核心：全场史低/热门
                        "steam",      # 重点：确保是 Steam 激活码（过滤掉育碧/其他平台）
                        "action",     # 分类：动作类（受众广，变现快）
                        "rpg",        # 分类：角色扮演（价格稳）
                        "strategy",   # 分类：策略类
                        "adventure",  # 分类：冒险类
                        "indie",      # 蓝海：独立游戏（经常有高 ROI 的小目标）
                        "ubisoft",    # 扩展：如果你也做育碧转单，可以开启
                        "capcom",     # 厂商：卡普空（经常有大折扣）
                        "bandai"      # 厂商：万代南梦宫
                    ]
                target_modes = ["lowest", "new_lowest"]
                # 💡 设置扫描深度：每类扫 3 页（大约覆盖 1000+ 商品）
                max_pages = 3 # 💡 每类探测 3 页，覆盖约 600-900 个动态目标
                
                for mode in target_modes: # 🚀 第一层：切换 史低/超史低
                    for task_keyword in search_tasks:
                        # 💡 新增：内层页码循环
                        for p in range(1, max_pages + 1):
                            # 💡 每一页开始前拿锁，扫完这一页自动放锁
                            async with global_commander.lock:
                                mode_tag = "超史低" if mode == "new_lowest" else "史低"
                                # AGENT_STATE["current_mission"] = f"正在扫描: {task_keyword or '全场'} [第{p}页]"
                                AGENT_STATE["current_mission"] = f"正在扫描: {task_keyword or '全场'} [{mode_tag}-P{p}]"
                                logger.info(f"🔎 正在调取杉果数据: [{task_keyword}] P{p}")
                                
                                try:
                                    # 💡 传入已验证的 page 参数
                                    sk_results = await global_commander.sonkwo.get_search_results(keyword=task_keyword, page=p, status=mode)
                                    
                                    # 💡 智能熔断：如果这一页没数据，说明该分类已到底，直接 break 跳到下一个分类
                                    if not sk_results:
                                        logger.info(f"📭 分类 [{task_keyword}] 已扫描完毕 (共 {p-1} 页)")
                                        break
                                        
                                except Exception as e:
                                    logger.error(f"⚠️ 杉果扫描异常 (词:{task_keyword} 页:{p}): {e}")
                                    continue

                            # --- 处理当前页抓到的战利品 ---
                            for item in sk_results:
                                log_entry = await global_commander.process_arbitrage_item(item)
                                total_scanned_this_round += 1  
                                
                                if log_entry:
                                    # 1. 成功对齐计数
                                    if log_entry.get("py_price") and "¥" in str(log_entry.get("py_price")):
                                        match_count += 1
                                    
                                    # 2. 盈利目标审计与利润累加
                                    if "成功" in log_entry.get("status", ""):
                                        profit_count += 1
                                        try:
                                            p_str = log_entry.get("profit", "0").replace("¥", "").strip()
                                            total_profit += float(p_str)
                                        except:
                                            pass

                # --- 🛰️ [核心排序逻辑]：当轮战利品大排队 ---
                if AGENT_STATE["history"]:
                    def extract_profit_val(h_item):
                        """辅助函数：提取利润数值用于排序"""
                        try:
                            # 提取利润字符串并清理符号，例如 '¥15.50' -> 15.5
                            val = str(h_item.get('profit', '0')).replace('¥', '').strip()
                            return float(val) if val != '---' else -999.0
                        except:
                            return -999.0

                    # 1. 局部去重：防止同一个游戏在不同分类任务中重复出现
                    unique_map = {}
                    for h in AGENT_STATE["history"]:
                        g_name = h.get('name')
                        current_p = extract_profit_val(h)
                        # 如果是新游戏，或者发现该游戏有更高的利润记录，则更新
                        if g_name not in unique_map or current_p > extract_profit_val(unique_map[g_name]):
                            unique_map[g_name] = h
                    
                    # 2. 执行排序：按利润从高到低排列 (reverse=True)
                    sorted_list = list(unique_map.values())
                    sorted_list.sort(key=extract_profit_val, reverse=True)
                    
                    # 3. 结果写回：同步到全局状态，只保留前 100 名最赚钱的目标
                    AGENT_STATE["history"] = sorted_list[:100]
                    
                    # 💡 注意：虽然不跨重启，但这里调用 save_history() 可以方便你在运行期间随时查看 json
                    save_history() 
                    
                    print(f"✅ 排序完成！当前榜首: {AGENT_STATE['history'][0].get('name')} | 利润: {AGENT_STATE['history'][0].get('profit')}")
                # --- [排序结束] ---

                # 3. 🚨 简报发送逻辑 (此时变量已完成累加)
                AGENT_STATE["scanned_count"] += 1 # 每次巡航完成，总进度+1
                # 3. 🚨 重点：在这里插入简报发送逻辑 (for 循环结束后)
                end_time = datetime.datetime.now()
                duration = (end_time - start_time).seconds
                jitter = random.randint(-600, 600)
                cycle_time += jitter
                # --- 🛰️ [新增]：提取本轮精锐名单 ---
                top_targets = ""
                if AGENT_STATE["history"]:
                    # 只取前 3 个最赚钱且通过审计的目标
                    for i, h in enumerate(AGENT_STATE["history"][:3]):
                        if "✅" in h.get('status', ''):
                            top_targets += f"🎯 {h.get('name')} | 利润: {h.get('profit')}\n"
                
                target_section = f"🔝 本轮精锐目标：\n{top_targets}" if top_targets else "🛡️ 暂无优质目标"
                summary_report = (
                    f"📊 【侦察母舰·巡航简报】\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"{target_section}\n" # 💡 把名单插在这里
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
async def get_dashboard(request: Request):
    # --- 1. 历史数据渲染核心逻辑 ---
    rows = ""
    history_list = AGENT_STATE.get("history", [])
    
    if not history_list:
        # 初始无数据时的占位行
        rows = "<tr><td colspan='7' style='text-align:center; padding:50px; color:#8b949e;'>🛰️ 侦察机巡航中，暂未发现利润目标...</td></tr>"
    else:
        for h in history_list:
            h_status = h.get('status', '未知状态')
            # 判定盈利且审计通过的逻辑
            is_profitable = "✅" in h_status
            star_color = "#8b949e"
            color = "#3fb950" if is_profitable else "#f85149"
            raw_rating = h.get('rating', '---')
            try:
                # 提取数字进行颜色判定
                r_val = float(str(raw_rating).replace('%', '')) if '%' in str(raw_rating) else 0
                star_color = "#ffcc00" if r_val >= 90 else ("#3fb950" if r_val >= 80 else "#8b949e")
            except:
                star_color = "#8b949e"
            rows += f"""
            <tr>
                <td>{h.get('time', '--:--:--')}</td>
                <td>
                    <div style="font-weight:bold; color:#f0f6fc;">{h.get('name', '未知商品')}</div>
                    <div style="font-size:12px; color:{star_color}; margin-top:4px;">
                        <span>⭐ Steam 好评: {raw_rating}</span>
                    </div>
                </td>
                <td>{h.get('sk_price', '---')}</td>
                <td style="color:#58a6ff; font-family:monospace; font-size:12px;">{h.get('py_price', '---')}</td>
                <td style='color:{color}; font-weight:bold;'>{h.get('profit', '---')} <small>({h.get('roi','0%')})</small></td>
                <td><span style="font-size:12px; opacity:0.8;">{h_status}</span><br><small style="color:#8b949e;">原因: {h.get('reason','无')}</small></td>
                <td><a href="{h.get('url','#')}" target="_blank" style="color:#ffcc00; text-decoration:none;">🛒 进货</a></td>
            </tr>
            """
    
    # 获取运行状态点颜色
    dot_color = "#3fb950" if AGENT_STATE.get("is_running") else "#f85149"
    
    # --- 2. 完整 HTML/CSS/JS 全量恢复 ---
    # --- 2. 核心变化：调用模板文件 ---
    # 这里的 "base_dashboard.html" 对应你在 web/templates 下创建的文件
    return templates.TemplateResponse("base_dashboard.html", {
        "request": request,
        "css_version": "1.0.1", # 随便写个版本号
        "rows": rows,
        "dot_color": dot_color,
        "current_mission": AGENT_STATE.get('current_mission', '待命'),
        "scanned_count": AGENT_STATE.get('scanned_count', 0)
    })


@app.get("/api/history")
async def get_history_api():
    """专门为前端提供最新的 50 条比价历史记录 (JSON 格式)"""
    return {
        "scanned_count": AGENT_STATE.get("scanned_count", 0),
        "current_mission": AGENT_STATE.get("current_mission", "待命"),
        "is_running": AGENT_STATE.get("is_running", False),
        "history": AGENT_STATE.get("history", [])[:50]
    }


@app.on_event("startup")
async def startup():
    # 启动后台常驻任务
    asyncio.create_task(continuous_cruise())

from fastapi.responses import FileResponse

# 1. 消除 favicon 报错噪音
@app.get('/favicon.ico', include_in_schema=False)
async def favicon():
    return Response(status_code=204) # 直接返回“无内容”，不报 404


@app.post("/web_post")
async def web_post_game(request: Request):
    try:
        data = await request.json()
        game, key, price = data.get("game", "").strip(), data.get("key", "").strip(), data.get("price", "").strip()

        if not game or not key or not price:
            return {"status": "error", "msg": "❌ 信息不完整"}

        async def web_task():
            try:
                # 💡 核心：进入排队序列
                async with global_commander.lock:
                    print(f"🛰️ [Web 指令] 正在执行挂载: {game}")
                    success = await global_commander.steampy.action_post_flow(f"{game}|{key}|{price}")
                    status = "✅ 成功" if success else "❌ 失败"
                    await global_commander.notifier.send_text(f"🖥️ Web端挂载反馈：{game} {status}")
            except Exception as e:
                logger.error(f"🚨 Web上架任务崩溃: {e}")
                await global_commander.notifier.send_text(f"🚨 Web端任务异常: {game}\n原因: {str(e)[:100]}")

        # 挂载后台任务
        asyncio.create_task(web_task())
        
        # 立即告知用户指令已送达
        return {"status": "success", "msg": f"✅ {game} 指令已排队，请留意飞书回执"}

    except Exception as e:
        return {"status": "error", "msg": f"🚨 系统错误: {str(e)}"}
# 2. 隐藏 API 文档（防止爬虫扫描接口定义）
# 修改 FastAPI 初始化：
# app = FastAPI(docs_url=None, redoc_url=None)

# --- 1. 财务数据接口 ---
@app.get("/api/audit_stats")
async def get_audit_stats():
    from Finance_Center.auditor import FinanceAuditor
    # 直接调用你刚才写好的详细审计函数
    return FinanceAuditor().run_detailed_audit()

# --- 2. 财务全息看板（直接用 HTML 字符串返回，不建文件） ---
@app.get("/audit", response_class=HTMLResponse)
async def get_audit_page(request: Request):
    # 现在这里只需要这一句话，优雅且专业
    return templates.TemplateResponse("audit_dashboard.html", {"request": request})
    
# --- 在文件顶部导入区添加 ---
from Finance_Center.sync_manager import SyncManager  # 确保路径正确

# --- 在 FastAPI 路由定义区添加同步接口 ---

@app.post("/api/sync_all")
async def sync_all_platforms():
    """🚀 一键同步按钮的后端实现"""
    global global_commander
    print("⏳ 同步指令已排队，等待当前巡航任务交出浏览器控制权...")
    if not global_commander:
        return {"status": "error", "msg": "❌ 引擎尚未初始化，请刷新页面重试"}

    async def background_sync():
        # 使用 global_commander 的锁，防止同步时干扰正在进行的自动巡航
        async with global_commander.lock:
            await asyncio.sleep(2)
            import gc
            try:
                manager = SyncManager(global_commander)
                result = await manager.run_full_sync()
                # 同步完成后，通过飞书知会一声
                status_ico = "✅" if result["status"] == "success" else "❌"
                await global_commander.notifier.send_text(f"{status_ico} 跨平台同步反馈：{result['msg']}")
            finally:
                del manager  # 销毁实例
                gc.collect() # 强制收割内存碎屑

    # 挂载后台任务，立即给前端返回“已开始”
    asyncio.create_task(background_sync())
    return {"status": "success", "msg": "📡 指令已下达，正在后台静默同步..."}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)