import asyncio
import os
import datetime
import sys
from pathlib import Path

# 锚定根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Sonkwo_Scout.sonkwo_hunter import SonkwoCNMonitor
import config

class SonkwoFinanceCollector(SonkwoCNMonitor):
    def __init__(self, **kwargs):
        # 💡 直接继承基类，基类会处理 user_data_dir
        super().__init__(**kwargs)
        self.save_dir = "blackbox/finance_sk"
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)
        self.step_idx = 0

    async def log_step(self, action_name):
        """📸 核心 Debug 机制：记录截图与 HTML"""
        self.step_idx += 1
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        prefix = f"{self.save_dir}/step_{self.step_idx}_{action_name}_{timestamp}"
        
        try:
            await self.page.screenshot(path=f"{prefix}.png")
            content = await self.page.content()
            with open(f"{prefix}.html", "w", encoding="utf-8") as f:
                f.write(content)
            print(f"📁 [DEBUG] 状态已记录: {action_name}")
        except Exception as e:
            print(f"🚨 记录失败: {e}")

    async def run_finance_debug(self):
        """🚀 启动财务模块的交互式 Debug 模式"""
        await self.start()
        print("\n" + "💰 " * 15)
        print("杉果财务账本提取模块 - 交互模式已启动")
        print("指令列表:")
        print("1. [goto]  - 跳转到订单页")
        print("2. [list]  - 解析当前页订单 (获取名称、价格)")
        print("3. [detail] - 解析详情 (获取CDKey)")
        print("4. [shot]  - 手动截图查看状态")
        print("5. [exit]  - 安全退出")
        print("💰 " * 15 + "\n")

        try:
            while True:
                sys.stdout.write(f"\r[财务审计] {datetime.datetime.now().strftime('%H:%M:%S')} | 指令 >> ")
                sys.stdout.flush()

                cmd_raw = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
                cmd = cmd_raw.strip().lower()

                if not cmd: continue
                if cmd == "exit": break

                elif cmd == "goto":
                    print("🚚 正在跳转至订单列表...")
                    await self.page.goto("https://www.sonkwo.cn/member/orders")
                    await asyncio.sleep(3)
                    await self.log_step("navigated_to_orders")

                elif cmd == "list":
                    print("🕵️ 正在解析订单基本信息...")
                    orders = await self.get_order_summary()
                    if orders:
                        print(f"✅ 成功抓取 {len(orders)} 条订单预览")
                    else:
                        print("❌ 未发现订单，请确认页面是否正确或已登录。")

                elif cmd == "shot":
                    await self.log_step("manual_check")
                    print("📸 截图已保存。")

        finally:
            await self.stop()

    async def get_order_summary(self):
        """抓取列表页概要信息"""
        await self.log_step("parsing_list")
        order_items = await self.page.query_selector_all(".order-list-item")
        
        results = []
        for i, item in enumerate(order_items):
            try:
                name_el = await item.query_selector(".sku-name")
                price_el = await item.query_selector(".real-price")
                
                name = (await name_el.text_content()).strip() if name_el else "未知"
                price = (await price_el.text_content()).strip() if price_el else "0"
                
                print(f"  [{i+1}] {name} | 价格: {price}")
                results.append({"name": name, "price": price})
            except:
                continue
        return results

if __name__ == "__main__":
    # 服务器环境强制 headless=True
    collector = SonkwoFinanceCollector(headless=True)
    asyncio.run(collector.run_finance_debug())