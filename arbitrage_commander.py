import asyncio
import sys
import os

# 1. 自动路径挂载
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(ROOT_DIR, "Sonkwo-Scout"))
sys.path.append(os.path.join(ROOT_DIR, "SteamPY-Scout"))

# 2. 导入组件
from sonkwo_hunter import SonkwoCNMonitor
from steampy_hunter import SteamPyMonitor
from feishu_notifier import FeishuNotifier
from ai_engine import ArbitrageAI  # 导入你的新大脑

class ArbitrageCommander:
    def __init__(self):
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

    async def close_all(self):
        await self.sonkwo.stop()
        await self.steampy.stop()

    async def analyze_arbitrage(self, game_name):
        """
        [深度侦察] 展示全平台对齐细节与利润核算
        """
        async with self.lock: # 确保浏览器操作不撞车
            print(f"🛰️ 专项任务启动: [{game_name}]")
            try:
                # 1. 抓取杉果源数据
                sk_results = await self.sonkwo.get_search_results(keyword=game_name)
                if not sk_results:
                    return f"❌ 杉果搜索无果：未找到关于 '{game_name}' 的商品。"

                target = sk_results[0]
                sk_title, sk_price = target['title'], float(target['price'].replace('￥','').replace('券后价',''))

                # 2. 暴露 AI 思考过程
                clean_keyword = self.ai.get_search_keyword(sk_title)
                
                # 3. 抓取变现端 (SteamPy) 详情
                py_data = await self.steampy.get_game_market_price_with_name(clean_keyword)
                
                report = (
                    f"🔍 [侦察详情]\n"
                    f"🔹 杉果原名: {sk_title}\n"
                    f"🤖 AI 提取词: {clean_keyword}\n"
                    f"--------------------------\n"
                )

                if not py_data:
                    return report + f"⚠️ 警报: 杉果价格 ¥{sk_price}，但 SteamPy 暂无匹配项。可能是版本名差异过大，建议手动核实。"

                py_price, py_match_name = py_data
                
                # 4. 版本比对与判定理由
                is_match = False
                reason = ""
                if sk_title.strip() == py_match_name.strip():
                    is_match, reason = True, "完全字符串匹配"
                else:
                    is_match = self.ai.verify_version(sk_title, py_match_name)
                    reason = "AI 语义校验通过" if is_match else "AI 判定版本冲突"

                # 5. 核心核算
                net_rev = py_price * 0.97
                profit = net_rev - sk_price
                roi = (profit / sk_price) * 100 if sk_price > 0 else 0

                report += (
                    f"📦 SteamPy 匹配: {py_match_name}\n"
                    f"⚖️ 判定理由: {reason}\n"
                    f"--------------------------\n"
                    f"🍎 杉果成本: ¥{sk_price}\n"
                    f"🍏 Py 端底价: ¥{py_price}\n"
                    f"💹 扣费后到账: ¥{net_rev:.2f}\n"
                    f"💵 预计净利润: ¥{profit:.2f}\n"
                    f"📈 预计利润率: {roi:.2f}%\n"
                )
                
                if is_match and profit >= self.min_profit:
                    report += "\n🔥 结论: 发现套利空间，建议搬运！"
                else:
                    report += "\n❌ 结论: 利润不足或版本拦截。"
                
                return report

            except Exception as e:
                return f"🚨 侦察异常: {str(e)}"

    async def run_mission(self, keyword=""):
        mode_text = f"定点打击 [{keyword}]" if keyword else "全场史低巡航"
        print(f"\n[MISSION] 🎯 模式: {mode_text}")
        
        try:
            # Step 1: 抓取杉果数据
            sk_results = await self.sonkwo.get_search_results(keyword=keyword)
            if not sk_results:
                print("📌 杉果侧无目标，任务结束。")
                return

            recommendations = []
            for item in sk_results:
                sk_name = item['title']
                # 价格解析容错
                try:
                    price_str = item['price'].replace('￥','').replace('券后价','').strip()
                    sk_price = float(price_str)
                except: continue

                # AI 关键词优化
                await asyncio.sleep(1.2) 
                clean_keyword = self.ai.get_search_keyword(sk_name)
                print(f"\n🤖 AI 优化搜索词: [{sk_name}] -> [{clean_keyword}]")

                # Step 2: 调取 SteamPy 市场价
                py_data = await self.steampy.get_game_market_price_with_name(clean_keyword)
                
                if py_data:
                    py_price, py_match_name = py_data
                    is_version_match = False
                    
                    # 判定逻辑：字符串匹配 -> 核心名包含 -> AI 终审
                    if sk_name.strip() == py_match_name.strip():
                        is_version_match = True
                    elif sk_name in py_match_name or py_match_name in sk_name:
                        tags = ["豪华", "Deluxe", "Gold", "Ultimate", "终极", "季票", "DLC"]
                        if not any(tag in sk_name or tag in py_match_name for tag in tags):
                            is_version_match = True

                    if not is_version_match:
                        print(f"🧐 正在请求 AI 终审: [{sk_name}] vs [{py_match_name}]")
                        await asyncio.sleep(1.5)
                        is_version_match = self.ai.verify_version(sk_name, py_match_name)

                    if is_version_match:
                        net_profit = (py_price * 0.97) - sk_price
                        print(f"💰 利润核算: ￥{net_profit:.2f}")
                        if net_profit >= self.min_profit:
                            recommendations.append({
                                "title": sk_name, "sk_price": sk_price,
                                "py_price": py_price, "profit": net_profit,
                                "url": item.get('url', "https://www.sonkwo.cn")
                            })
            
            # Step 4: 飞书发报
            if recommendations:
                print(f"🚀 捕获 {len(recommendations)} 个盈利目标，发送至飞书...")
                self.notifier.send_arbitrage_report(recommendations)
            else:
                print("📌 本轮巡航未发现可盈利目标。")
        except Exception as e:
            # 局部异常仅打印，不触发重启，交给外部 watchdog 捕获核心崩溃
            print(f"⚠️ 任务执行中发生局部异常: {e}")
            raise e

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