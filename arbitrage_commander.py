import asyncio
import sys
import os
import datetime
import traceback
import re
# 1. 自动路径挂载
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(ROOT_DIR, "Sonkwo-Scout"))
sys.path.append(os.path.join(ROOT_DIR, "SteamPY-Scout"))

# 2. 导入组件
from sonkwo_hunter import SonkwoCNMonitor
from steampy_hunter import SteamPyMonitor
from feishu_notifier import FeishuNotifier
from ai_engine import ArbitrageAI  # 导入你的新大脑

def get_search_query(raw_name):
    """
    侦察兵降噪逻辑：只删杂质，不砍名字。
    确保中英文核心词完整进入搜索框。
    """
    # 剔除行业噪音词
    garbage = r"(券后价|激活码|【.*】|\[.*\]|现货|秒发|CDKEY|Digital|数字版|Steam版|CN/HK|Global|全球版)"
    clean = re.sub(garbage, "", raw_name, flags=re.IGNORECASE).strip()
    
    # 进一步清理多余的连字符或空格
    clean = re.sub(r"\s+", " ", clean) 
    return clean

class ArbitrageCommander:
    def __init__(self, agent_state=None): # 💡 加上这个参数
        self.agent_state = agent_state   # 💡 将 Web 状态挂载到实例上
        self.sonkwo = SonkwoCNMonitor()
        self.steampy = SteamPyMonitor()
        self.ai = ArbitrageAI()
        self.notifier = FeishuNotifier("https://open.feishu.cn/open-apis/bot/v2/hook/70423ec9-8744-40c2-a3af-c94bbbd0990a")
        self.lock = asyncio.Lock()
        self.min_profit = 5.0  # 有了 AI 过滤，我们可以把门槛稍微调低点
        self.status = {
            "state": "IDLE",      # IDLE, RUNNING, RECOVERY, ERROR
            "last_run": None,
            "retry_count": 0,
            "current_mission": "等待指令"
        }

    async def init_all(self):
        self.status["state"] = "INITIALIZING"
        print("🛰️  正在启动【AI 增强版】双平台联合侦察系统...")
        # 依次启动避免浏览器冲突
        try:
            await self.sonkwo.start()
            await self.steampy.start()
            self.status["state"] = "RUNNING"
            return True
        except ConnectionError as e:
            # 捕获异常，更新 AGENT_STATE 并在终端报错
            print(f"🛑 初始化失败: {e}")
            # 如果你有 AGENT_STATE，可以更新它
            # AGENT_STATE["current_mission"] = f"错误: {e}"
            return False
    
    async def update_result(self, log_entry):
        """统一结果分发中心"""
        if self.agent_state:
            # 方案 B：去重覆盖逻辑
            self.agent_state["history"] = [
                h for h in self.agent_state["history"] 
                if h['name'] != log_entry['name']
            ]
            self.agent_state["history"].insert(0, log_entry)
            self.agent_state["history"] = self.agent_state["history"][:50]

    async def close_all(self):
        await self.sonkwo.stop()
        await self.steampy.stop()

    async def analyze_arbitrage(self, game_name):
        """专项点杀：直接复用加工中心的成果"""
        clean_name = get_search_query(game_name) 
        sk_results = await self.sonkwo.get_search_results(keyword=clean_name)
        
        if not sk_results: return "❌ 杉果未找到该商品"

        # 💡 这里直接取搜索结果的第一条（因为现在是精准搜索了）
        log_entry = await self.process_arbitrage_item(sk_results[0], is_manual=True)

        report = (
            f"🔍 [侦察详情]\n🔹 杉果原名: {log_entry['name']}\n"
            f"⚖️ 判定结果: {log_entry['status']}\n"
            f"--------------------------\n"
            f"🍎 成本: {log_entry['sk_price']} | 🍏 变现: {log_entry['py_price']}\n"
            f"💵 净利: {log_entry['profit']} | 📈 ROI: {log_entry['roi']}\n"
            f"🔗 详情直达: \n{log_entry['url']}" # 💡 这里的 URL 必须来自 log_entry
        )
        return report

    async def process_arbitrage_item(self, sk_item, is_manual=False):
        """
        全能加工中心：负责清洗、搜索、AI审计及利润核算
        """
        sk_name = sk_item.get('title', '未知商品')
        
        # --- 1. 价格防弹处理 ---
        raw_price_str = str(sk_item.get('price', '0'))
        try:
            # 暴力提取数字和小数点，无视 '...' 或 '￥'
            clean_price_str = re.sub(r'[^\d.]', '', raw_price_str)
            sk_price = float(clean_price_str) if clean_price_str and clean_price_str != "." else 0.0
        except Exception:
            sk_price = 0.0

        if sk_price <= 0:
            # 价格异常的商品不具备套利分析价值
            return None

        # --- 2. 搜索词降噪 (不缩词) ---
        # 调用我们写在类外的 get_search_query 函数
        search_keyword = get_search_query(sk_name)

        # --- 3. 跨平台侦察 (SteamPy 撞库) ---
        py_data = None
        async with self.lock:
            try:
                # 拿着洗好的名字去 SteamPy 详情页抓价格
                py_data = await self.steampy.get_game_market_price_with_name(search_keyword)
            except Exception as e:
                print(f"🚨 SteamPy 搜索链路故障: {e}")

        if not py_data:
            return None

        py_price, py_match_name = py_data

        # --- 4. AI 语义审计 (判定版本与实体) ---
        audit_prompt = f"""
        请对比以下两个游戏商品，判断它们是否为【同一个游戏】且【版本价值对等】。
        
        1. 进货端(杉果): {sk_name}
        2. 变现端(市场): {py_match_name}

        【判定规则】:
        - MATCH: 同款且版本一致，或进货版本更高（如进货豪华版，变现标准版）。
        - VERSION_ERROR: 同款但进货版本低（如进货标准版，变现豪华版，会导致利润虚高）。
        - ENTITY_ERROR: 根本不是同一个游戏（如《空洞骑士》vs《丝之歌》）。

        仅输出一个单词: MATCH / VERSION_ERROR / ENTITY_ERROR
        """
        
        # 调用我们在 ai_engine 里修好的 quick_call
        audit_result = self.ai.quick_call(audit_prompt)

        # --- 5. 结果核算与封装 ---
        status_text, profit_str, current_roi = "🛑 审核未通过", "---", 0.0
        reason = f"AI判定: {audit_result}"

        if audit_result == "MATCH":
            # 这里的 0.97 是扣除 Steam 市场手续费后的约值
            net_profit = (py_price * 0.97) - sk_price
            profit_str = f"¥{net_profit:.2f}"
            current_roi = (net_profit / sk_price * 100) if sk_price > 0 else 0
            
            if net_profit > self.min_profit:
                status_text = "✅ 匹配成功"
                reason = "语义对齐通过，利润达标"
            else:
                status_text = "📉 利润微薄"
                reason = "版本对齐但利润过低"
                
        elif audit_result == "VERSION_ERROR":
            status_text = "⚠️ 版本错位"
            reason = "进货版低于变现版，价格不可对标"
        elif audit_result == "ENTITY_ERROR":
            status_text = "❌ 实体不符"
            reason = "搜索结果非同款游戏"
        else:
            status_text = "❓ 审计失败"
            reason = f"AI返回异常: {audit_result}"

        # 构造最终日志条目
        log_entry = {
            "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "name": f"🛰️(点杀) {sk_name}" if is_manual else sk_name,
            "sk_price": f"¥{sk_price}",
            "py_price": f"¥{py_price}",
            "profit": profit_str,
            "status": status_text,
            "url": sk_item.get('url', 'https://www.sonkwo.cn'),
            "reason": reason,
            "roi": f"{current_roi:.1f}%"
        }

        # 更新 Web 界面显示
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
            
async def start_cruise_with_watchdog(commander, target_keyword):
    retry_count = 0
    while True:
        try:
            # 1. 尝试初始化
            await commander.init_all()
            
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
    
    # 获取 Web 服务启动任务
    # 注意：run_web_server 内部应该通过 commander 引用来获取数据展示
    from web_dashboard import run_web_server 

    print("🛰️  Arbitrage Sentinel 双引擎准备就绪")
    
    # 并发运行：Dashboard 挂了不影响巡航，巡航重启不影响 Dashboard 访问
    await asyncio.gather(
        run_web_server(commander),                # 传入 commander 实例供 API 调用
        start_cruise_with_watchdog(commander, target)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 用户手动停止。")