import asyncio
import sys
import os
import datetime
import traceback
import re
import config
from pathlib import Path
from Finance_Center.finance_service import FinanceService
from Finance_Center.steampy_service import SteamPyService  # ✅ 新增这一行

# --- 🚀 路径自愈逻辑 ---
# 强制定位当前脚本所在的绝对路径为根目录
PROJECT_ROOT = Path(__file__).resolve().parent

# 1. 确保根目录在搜索路径的第一位
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 2. 特别处理：如果还是找不到，手动把当前目录加入 PYTHONPATH 环境
os.environ["PYTHONPATH"] = str(PROJECT_ROOT)

import config

# --- 📦 导入组件 ---
# 既然已经有了 __init__.py 且路径已锚定，这样写就稳了
from Sonkwo_Scout.sonkwo_hunter import SonkwoCNMonitor
from SteamPY_Scout.steampy_hunter import SteamPyMonitor
from feishu_notifier import FeishuNotifier
from ai_engine import ArbitrageAI
from game_rating.rating_manager import GameRatingManager
from auto_lister import AutoLister, ListingStatus  # 🆕 自动上架引擎

def get_search_query(raw_name):
    # 1. 剔除噪音词
    garbage = r"(券后价|秒杀价|激活码|【.*】|\[.*\]|现货|秒发|CDKEY|Digital|数字版|Steam版|CN/HK|Global|全球版|标准版|典藏版|最终版|周年纪念版|原罪学者|皇家版)"
    clean = re.sub(garbage, "", raw_name, flags=re.IGNORECASE).strip()
    
    # 2. 💡 重点：清除所有形式的括号及其内部的空内容
    clean = re.sub(r"[\(\)（）\s]+$", "", clean) # 清除结尾的括号和空格
    clean = re.sub(r"[\(\)（）]", " ", clean)     # 将中间的括号转为空格
    
    # 3. 深度清理多余空格
    clean = " ".join(clean.split())
    return clean

class ArbitrageCommander:
    def __init__(self, agent_state=None): # 💡 加上这个参数
        self.agent_state = agent_state   # 💡 将 Web 状态挂载到实例上
        self.sonkwo = SonkwoCNMonitor()
        self.steampy = SteamPyMonitor()
        self.ai = ArbitrageAI()
        # 💡 [新增] 将评分中心挂载到 Commander 上，并复用已有的 AI 引擎
        self.rating_center = GameRatingManager(ai_handler=self.ai)
        self.notifier = FeishuNotifier(config.NOTIFIER_CONFIG["WEBHOOK_URL"])
        self.steampy.notifier = self.notifier
        self.lock = asyncio.Lock()
        self.min_profit = config.AUDIT_CONFIG["MIN_PROFIT"]  # 有了 AI 过滤，我们可以把门槛稍微调低点
        self.status = {
            "state": "IDLE",      # IDLE, RUNNING, RECOVERY, ERROR
            "last_run": None,
            "retry_count": 0,
            "current_mission": "等待指令"
        }
        self.finance = None
        self.steampy_center = None
        self.auto_lister = None  # 🆕 自动上架引擎

    async def init_all(self):
        self.status["state"] = "INITIALIZING"
        print("🛰️  正在启动【AI 增强版】双平台联合侦察系统...")
        # 💡 [关键点] 必须在这里初始化评分中心的数据库
        if not self.rating_center.initialize():
            print("❌ 评分中心初始化失败，请检查 steamspy_all.json 是否存在。")
            return False
        # 依次启动避免浏览器冲突
        try:
            await self.sonkwo.start()
            await self.steampy.start()
            if not self.finance:
                self.finance = FinanceService(self.sonkwo.context)
            if not self.steampy_center:
                self.steampy_center = SteamPyService(self.steampy.context)
            if not self.auto_lister:
                self.auto_lister = AutoLister(steampy_monitor=self.steampy, notifier=self.notifier)
            print("✅ 所有系统组件启动成功，进入待命状态。")
            self.status["state"] = "RUNNING"
            return True
        except ConnectionError as e:
            # 捕获异常，更新 AGENT_STATE 并在终端报错
            print(f"🛑 初始化失败: {e}")
            # 如果你有 AGENT_STATE，可以更新它
            # AGENT_STATE["current_mission"] = f"错误: {e}"
            return False
        
    async def update_result(self, log_entry):
        if self.agent_state is not None:
            # 💡 这里绝对不能执行 .append() 或 .insert()！
            # 这里的目的只是为了让 commander 运行时不报错
            print(f"📡 [DATA_SYNC] 情报已传回母舰: {log_entry['name']}")

    async def close_all(self):
        await self.sonkwo.stop()
        await self.steampy.stop()

    def _find_best_match(self, sk_results, target_name):
        """
        在杉果搜索结果中找到最匹配目标名称的商品
        匹配优先级：完全匹配 > 去空格匹配 > 包含关系
        """
        target_lower = target_name.lower()
        target_nospace = target_lower.replace(' ', '')

        best_candidate = None

        for item in sk_results:
            title = item.get('title', '')
            title_lower = title.lower()
            title_nospace = title_lower.replace(' ', '')

            # 完全匹配：直接返回
            if title == target_name or title_lower == target_lower:
                return item

            # 去空格后完全匹配
            if title_nospace == target_nospace:
                return item

            # 包含关系：记录为候选，继续找更好的
            if best_candidate is None:
                if target_lower in title_lower or title_lower in target_lower:
                    best_candidate = item
                elif target_nospace in title_nospace or title_nospace in target_nospace:
                    best_candidate = item

        return best_candidate

    async def analyze_arbitrage(self, game_name):
        """专项点杀：适配 Top 5 展示"""
        clean_name = get_search_query(game_name)
        sk_results = await self.sonkwo.get_search_results(keyword=clean_name)

        if not sk_results:
            return "❌ 杉果未找到该商品"

        # 💡 在搜索结果中找到最匹配的商品
        target_item = self._find_best_match(sk_results, game_name)

        if not target_item:
            return f"❌ 杉果搜索结果中未找到匹配「{game_name}」的商品"

        log_entry = await self.process_arbitrage_item(target_item, is_manual=True)

        if not log_entry:
            return "❌ 变现端未搜到匹配结果"

        report = (
            f"🔍 [侦察详情]\n🔹 杉果原名: {log_entry['name']}\n"
            f"⚖️ 判定结果: {log_entry['status']}\n"
            f"--------------------------\n"
            f"🍎 成本: {log_entry['sk_price']}\n"
            f"🍏 SteamPy (Top5): {log_entry['py_price']}\n"
            f"💵 预计净利: {log_entry['profit']} | 📈 ROI: {log_entry['roi']}\n"
            f"📝 审计理由: {log_entry['reason']}\n"
            f"--------------------------\n"
            f"🔗 详情直达: \n{log_entry['url']}"
        )
        return report

    async def process_arbitrage_item(self, sk_item, is_manual=False):
        """
        全能加工中心：负责清洗、搜索、AI 语义审计（含理由捕获）及利润核算
        """
        sk_name = sk_item.get('title', '未知商品')
        # --- 1. [关键补回] 进货价提取与防弹处理 ---
        raw_price_str = str(sk_item.get('price', '0'))
        try:
            # 暴力提取数字和小数点，彻底解决 '...' 或 '券后价' 导致的崩溃
            clean_price_str = re.sub(r'[^\d.]', '', raw_price_str)
            sk_price = float(clean_price_str) if clean_price_str and clean_price_str != "." else 0.0
        except Exception:
            sk_price = 0.0

        if sk_price <= 0: 
            return None # 价格异常不具备分析价值
        
        # --- 2. 统一质量/版本审计 ---
        appid, rating_data, status = await self.rating_center.get_rating_and_id(sk_name)
        
        rating_val = None 
        total_reviews = 0
        print(f"🔬 [底层数据] {sk_name} -> 状态: {status} | 原始返回: {rating_data}")
        if status == "SUCCESS" and isinstance(rating_data, dict):
            # 💡 [关键修正] 适配真实的返回字段
            # 优先从 review_count 获取总评论数
            total_reviews = rating_data.get('review_count', 0)
            
            # 💡 [关键修正] 从 info 字符串中正则提取百分比数字 (例如 "Rating: 95%" -> 95)
            info_str = rating_data.get('info', '')
            score_match = re.search(r'Rating:\s*(\d+)%', info_str)
            if score_match:
                rating_val = int(score_match.group(1))
            else:
                # 如果正则失败，尝试 fallback 到旧的 pos/neg 计算（以防万一）
                pos = rating_data.get('positive', 0)
                neg = rating_data.get('negative', 0)
                if pos + neg > 0:
                    total_reviews = pos + neg
                    rating_val = int((pos / total_reviews) * 100)

        # --- 核心拦截策略调整：疑罪从无 ---
        if not is_manual:
            # 策略 A：如果审计状态是 ERROR (代码报错)，我们要拦截以防万一
            if status == "ERROR":
                print(f"🚨 [系统异常] {sk_name} -> {rating_data}")
                return None
            
            # 策略 B：只有在【明确拿到高样本量】且【明确差评】时才熔断
            # 如果是“识别弃权”或“搜不到”，rating_val 会是 None，从而跳过这个 if
            if isinstance(rating_val, int) and total_reviews > config.AUDIT_CONFIG["MIN_REVIEWS"]:
                if rating_val < config.AUDIT_CONFIG["MIN_SCORE"]: # 确定的差评大作
                    print(f"🗑️ [差评熔断] {sk_name} (好评率:{rating_val}%)，已拦截。")
                    return None
            
            # 💡 这里不再对 UNCERTAIN (识别弃权) 进行 return None，而是让它流下去继续比价
            if status == "UNCERTAIN":
                print(f"⚠️ [审计模糊] {sk_name} -> AI无法确定身份，放行至变现端进一步对齐。")

        # --- 3. 记录数据 (兼容字符串和数字显示) ---
        sk_item['steam_appid'] = appid
        rating = rating_val if rating_val is not None else rating_data
        # 调试输出：一眼看出这款游戏在数据库里的真实底细
        print(f"📊 [审计快报] {sk_name} | 状态: {status} | 评分: {rating if isinstance(rating, int) else 'N/A'}% | 样本: {total_reviews}")
        sk_item['steam_rating_detail'] = rating_data.get('info', 'N/A') if isinstance(rating_data, dict) else "N/A"

        # --- 2. 搜索词降噪（不缩词，调用类外定义的 get_search_query） ---
        search_keyword = get_search_query(sk_name)
        print(f"🔍 [COMMANDER] 原始名: [{sk_name}] -> 降噪搜索词: [{search_keyword}]")

        # 💡 提取 cd_key 用于缓存匹配
        cd_key = sk_item.get('cd_key') or (sk_item.get('all_keys', [None])[0] if sk_item.get('all_keys') else None)

        # --- 3. 跨平台侦察 (SteamPy 撞库) ---
        py_data = None
        # --- 3. 跨平台侦察 (SteamPy 撞库) ---
        async with self.lock:
            try:
                # 💡 传入原始名用于版本校验，cd_key 用于缓存匹配
                res = await self.steampy.get_game_market_price_with_name(search_keyword, original_name=sk_name, cd_key=cd_key)

                if not res or len(res) < 3:
                    print(f"⚠️ [COMMANDER] {search_keyword} 变现端无匹配或格式错误")
                    return None

                # 解包三元组
                py_price, py_match_name, top5_list = res

            except Exception as e:
                print(f"🚨 SteamPy 搜索链路故障: {e}")
                return None

        # 💡 修改点 2：将 Top 5 价格列表格式化
        py_price_display = " | ".join([f"¥{p}" for p in top5_list]) if top5_list else f"¥{py_price}"
        
        print(f"🎯 [COMMANDER] 进货端: {sk_name} (¥{sk_price}) | 变现端(Top5): {py_price_display}")        # py_price, py_match_name = py_data
        # print(f"🎯 [COMMANDER] 进货端: {sk_name} (¥{sk_price}) | 变现端: {py_match_name} (¥{py_price})")
        # --- 4. AI 语义审计（判定结果 + 理由捕获） ---
        audit_prompt = f"""
        请对比以下两个游戏商品，判断它们是否为【同一个游戏】且【版本价值对等】。
        
        1. 进货端(杉果): {sk_name}
        2. 变现端(市场): {py_match_name}

        【判定规则】:
        - MATCH: 同款且版本一致，或进货版本更高。
        - VERSION_ERROR: 同款但进货版本低（如标准版对标豪华版价）。
        - ENTITY_ERROR: 根本不是同一个游戏。
        【强制执行准则】:
        1. 版本严阵以待：如果进货端是“标准版/Standard”，而变现端含有“豪华/Deluxe/Gold/Ultimate/Super”等字样，必须判定为 VERSION_ERROR。
        2. 价值不对等拦截：严禁“低版本”对标“高版本”。哪怕是同款游戏，只要版本后缀不同，一律拦截。
        3. 实体校验：如果一个是游戏本体，另一个是 DLC、原声带、合集，必须判定为 ENTITY_ERROR。
        4. 别名放行：允许 P5R 对应 Persona 5 Royal 这种合理的翻译或缩写对齐。
        5. 渠道对齐规则：
           - 进货端含有“Steam版”或“Steam Key”字样，而变现端只写了游戏名（如：古剑奇谭），这种情况应视为【同一个游戏】。
           - 变现端（SteamPy）本身就是基于 Steam 市场的，所以不需要重复确认“是否为 Steam 版”。
           - 只要游戏名称、版本（标准/豪华）匹配，分发渠道的描述差异可以忽略。
        【特例放行清单】:
            - 必须识别常见的官方中文翻译，例如：
            * "异形工厂" 就是 "shapez.io"
            * "双人成行" 就是 "It Takes Two"
            * "泰拉瑞亚" 就是 "Terraria"
        输出要求：严格按下面两行格式输出，禁止任何前言和总结。
        判定: [结果]
        理由: [原因]
        """
        
        # 直接调用底层接口获取原始文本，以便解析理由
        # 直接调用底层接口获取原始文本
        raw_response = self.ai._call_with_retry(audit_prompt)
        
        # 1. 设定初始值
        audit_result = "ERROR"
        audit_reason = "AI 响应解析失败"
        
        if raw_response:
            # 2. 尝试提取判定词（兼容中英文冒号）
            res_match = re.search(r'判定[:：]\s*(\w+)', raw_response, re.I)
            
            if res_match:
                # 解析成功：更新结果
                audit_result = res_match.group(1).upper()
                # 提取理由
                reason_match = re.search(r'理由[:：]\s*(.*)', raw_response)
                audit_reason = reason_match.group(1).strip() if reason_match else "已通过审计"
                # 💡 成功时打印真实的结论
                print(f"🧠 [AI 审计] 结论: {audit_result} | 理由: {audit_reason}")
            else:
                # 💡 解析失败：打印原始响应，这是最关键的调试信息！
                print(f"\n{'!'*40}")
                print(f"⚠️ AI 格式错误，无法解析！原始文本如下：\n{raw_response}")
                print(f"{'!'*40}\n")
        else:
            print("🚨 AI 未能返回任何响应")
            
        # --- 5. 结果核算与状态分流 ---
        status_text, profit_str, current_roi = "🛑 审核未通过", "---", "0%"
        
        if audit_result == "MATCH":
            net_profit = (py_price * 0.97) - sk_price
            profit_str = f"¥{net_profit:.2f}"
            current_roi = f"{(net_profit / sk_price * 100):.1f}%" if sk_price > 0 else "0%"
            status_text = "✅ 匹配成功" if net_profit > self.min_profit else "📉 利润微薄"
        elif audit_result == "VERSION_ERROR":
            status_text = "⚠️ 版本错位"
        elif audit_result == "ENTITY_ERROR":
            status_text = "❌ 实体不符"

        # 1. 构造友好的简短评价
        if isinstance(rating, int):
            display_rating = f"{rating}%"
        else:
            # 如果是 AI 的长篇大论，我们只在 Web 评价栏显示“待核实”或“需手动”
            # 而把那一大串理由留在 log_entry['reason'] 供鼠标悬停查看
            display_rating = "🔍 待核实" if "识别弃权" in str(rating) else "⚠️ 审计跳过"
        # 构造完整 log_entry，确保包含 'profit' 等所有字段防止前端 KeyError
        log_entry = {
            "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "name": f"🛰️(点杀) {sk_name}" if is_manual else sk_name,
            "rating": display_rating,
            "sk_price": f"¥{sk_price}",
            "py_price": f"¥{py_price_display}",
            "profit": profit_str,
            "status": status_text,
            "url": sk_item.get('url', 'https://www.sonkwo.cn'),
            "reason": audit_reason,
            "roi": current_roi
        }

        await self.update_result(log_entry)
        return log_entry

    async def run_mission(self, keyword=""):
        mode_text = f"定点打击 [{keyword}]" if keyword else "全场史低巡航"
        print(f"\n[MISSION] 🎯 模式: {mode_text}")
        
        try:
            # Step 1: 抓取杉果原始结果
            sk_results = await self.sonkwo.get_search_results(keyword=keyword)
            if not sk_results:
                print("📌 杉果侧无目标，任务结束。")
                return

            for item in sk_results:
                # 💡 [战略核心]：不再手动拼逻辑，直接调用已经修好 URL 的加工中心
                # 它内部会自动执行：URL补全 -> AI查价 -> AI对齐 -> 更新Web状态
                log_entry = await self.process_arbitrage_item(item)
                
                if not log_entry: continue

                # 💡 [判定发报]：从加工好的 log_entry 里提取利润
                try:
                    # 剥离 ¥ 符号进行数值判定
                    profit_val = float(log_entry['profit'].replace('¥','')) if '¥' in log_entry['profit'] else 0
                except: profit_val = 0

                if profit_val >= self.min_profit and "✅" in log_entry['status']:
                    print(f"🔥 发现利润点: {log_entry['name']} | 预计赚: {log_entry['profit']}")
                    
                    # 💡 [异步通知]：这里的 URL 现在绝对是详情页链接了
                    asyncio.create_task(self.notifier.send_arbitrage_report([{
                        "title": log_entry['name'], 
                        "sk_price": log_entry['sk_price'], 
                        "py_price": log_entry['py_price'], 
                        "profit": log_entry['profit'], 
                        "url": log_entry['url'] # 这里引用的是加工后的 log_entry 里的 url
                    }]))
                
                # 巡航频率控制
                await asyncio.sleep(1.0) 

        except Exception as e:
            print(f"⚠️ 巡航任务发生局部异常: {e}")
            

    # 🆕 一键上架待售商品
    async def auto_list_missing_items(self, missing_items: list) -> dict:
        """
        一键上架待售商品（从财务审计接口调用）
        
        Args:
            missing_items: 待售商品列表，每项包含：
                - name: 游戏名
                - cd_key: 激活码
                - cost: 采购成本
                
        Returns:
            汇总报告字典
        """
        if not self.auto_lister:
            return {"success": False, "message": "自动上架引擎尚未初始化"}
        
        if not missing_items:
            return {"success": True, "message": "没有待上架商品", "results": []}

        print(f"\n🚀 [一键上架] 开始处理 {len(missing_items)} 个待售商品")
        
        # 调用自动上架引擎
        results = await self.auto_lister.list_missing_items(missing_items)
        
        # 生成汇总报告
        summary = {
            "success": True,
            "total": len(results),
            "success_count": sum(1 for r in results if r.status.value == "success"),
            "failed_count": sum(1 for r in results if r.status.value == "failed"),
            "skipped_count": sum(1 for r in results if "skipped" in r.status.value),
            "results": [
                {
                    "name": r.purchase_name,
                    "status": r.status.value,
                    "message": r.message,
                    "listing_price": r.listing_price,
                    "profit": r.profit
                }
                for r in results
            ]
        }
        
        return summary

async def start_cruise_with_watchdog(commander, target_keyword):
    retry_count = 0
    while True:
        try:
            # 1. 尝试初始化
            await commander.init_all()
            while True:
                await asyncio.sleep(5)
            # 2. 执行任务逻辑
            # 这里调用的是 commander 内部的方法
            await commander.run_mission(target_keyword)
            
            if target_keyword: 
                print("🎯 定点打击完成，系统安全下线。")
                await commander.close_all()
                break 
                
            print("💤 巡航结束，等待 30 分钟后进行下一轮...")
            await commander.close_all() # 周期性重启可以防止浏览器缓存堆积
            await asyncio.sleep(1800)
            
        except Exception as e:
            retry_count += 1
            error_msg = traceback.format_exc()
            print(f"🚨 监测到核心崩溃: {e}")
            
            # 发送飞书警报
            try:
                await commander.notifier.send_text(
                    f"⚠️ 【侦察机故障报告】\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"原因: {str(e)}\n"
                    f"状态: 正在尝试第 {retry_count} 次自动重启...\n"
                    f"📍 堆栈摘要:\n{error_msg[-400:]}"
                )
            except: pass
            
            # 彻底关闭旧资源，释放 Session 文件夹锁
            await commander.close_all()
            
            # 等待 15 秒后重启
            await asyncio.sleep(15)

async def main():
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    commander = ArbitrageCommander()
    
    # 💡 暂时注释掉报错的 Web 引用
    # from web_dashboard import run_web_server 

    print("🛰️  Arbitrage Sentinel 引擎准备就绪")

    # --- 🚀 交互指令监听任务 ---
    async def input_loop():
        # 给 Watchdog 一点初始化浏览器的时间
        await asyncio.sleep(10) 
        print("\n" + "🎮 " * 10)
        print("【系统就绪】输入 'sync' 进入财务审计交互模式")
        print("🎮 " * 10 + "\n")
        
        while True:
            cmd_raw = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            cmd = cmd_raw.strip().lower()
            
            if cmd == "sync":
                if commander.finance:
                    await commander.finance.enter_interactive_mode()
                else:
                    print("❌ 财务服务尚未就绪（Watchdog 还在初始化...）")
            elif cmd == "exit":
                break
            elif cmd == "py":  # 比如输入 py 进入 SteamPy 模块
                if commander.steampy_center:
                    await commander.steampy_center.enter_interactive_mode()
                else:
                    print("❌ SteamPy 服务尚未就绪（Watchdog 还在初始化...）")

    # --- 🚀 只运行巡航和监听 ---
    await asyncio.gather(
        start_cruise_with_watchdog(commander, target),
        input_loop()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 用户手动停止。")