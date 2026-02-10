import asyncio
import re
from steampy_scout_core import SteamPyScout
from tabulate import tabulate

class MarketMission(SteamPyScout):
    async def run_mission(self):
        # 1. 沿用 100% 成功的起飞逻辑
        page = await self.start()
        if not page: return

        try:
            # 2. 完全复刻刚才成功的导航动作
            print("🖱️ 正在点击 'CDKey市场'...")
            await page.click("text=CDKey市场")
            await asyncio.sleep(1.5) 
            
            print("🖱️ 正在选择 '国区'...")
            # 回到刚才成功的定位逻辑：直接找类名 + 文本过滤
            await page.locator(".ivu-menu-item").filter(has_text=re.compile(r"^国区$")).click()

            # 3. 等待市场列表加载（刚才这里报错，现在我们用 attached 模式修复）
            print("⏳ 正在同步市场数据...")
            # 只要搜索框出来，就说明导航成功了
            await page.wait_for_selector(".ivu-input", timeout=30000)
            print("✅ 成功进入国区列表页。")

            # 给 Vue 填数据的时间
            await asyncio.sleep(3)

            # 4. 暴力提取数据 (使用 text_content 绕过 hidden)
            print("📊 正在解析前 10 条报价...")
            # 锁定商品容器（包含价格的 div）
            items = await page.query_selector_all(".item-list-item, .ivu-table-row, div[class*='item-']")
            
            final_results = []
            seen = set()

            for item in items:
                raw_text = await item.text_content()
                if raw_text and ("¥" in raw_text or "￥" in raw_text):
                    # 格式化文本：去掉多余空格，合并为一行
                    clean_row = " | ".join([s.strip() for s in raw_text.split('\n') if s.strip()])
                    
                    # 简单过滤重复和过短的噪音
                    if len(clean_row) > 15 and clean_row[:30] not in seen:
                        final_results.append([clean_row])
                        seen.add(clean_row[:30])
                
                if len(final_results) >= 10: break

            if final_results:
                print("\n🚀 侦察成功！当前实时报价如下：")
                print(tabulate(final_results, headers=["报价详情 (卖家 | 价格 | 状态)"], tablefmt="grid"))
            else:
                print("❌ 虽然进到了页面，但没能抓到带 ¥ 的报价，请检查网络或手动刷新。")

        except Exception as e:
            print(f"🚨 运行异常: {e}")
        finally:
            print("\n💡 侦察机待命中，15秒后自动关闭...")
            await asyncio.sleep(15)
            await self.stop()

if __name__ == "__main__":
    mission = MarketMission(headless=False)
    asyncio.run(mission.run_mission())