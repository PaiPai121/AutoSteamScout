import asyncio
from steampy_scout_core import SteamPyScout # 确保刚才封装的文件名正确
from tabulate import tabulate

class LowKeySentinel(SteamPyScout):
    async def monitor_prices(self):
        page = await self.start()
        if not page:
            return

        print("\n" + "="*50)
        print("🕵️  哨兵模式已启动！")
        print("👉 动作：请在浏览器窗口中进行任何搜索或翻页操作。")
        print("👉 目标：只要屏幕出现价格，我就自动记录。")
        print("="*50 + "\n")

        last_top_price = ""

        try:
            while True:
                # 1. 尝试寻找页面上所有的“商品条目”
                # 这里使用你之前观察到的 iview 结构，加上模糊匹配
                items = await page.query_selector_all(".item-list-item, div[class*='item-']")
                
                current_data = []
                for item in items[:10]:
                    text = await item.inner_text()
                    if "¥" in text or "￥" in text:
                        # 清洗数据，按行分割
                        clean_row = [line.strip() for line in text.split('\n') if line.strip()]
                        current_data.append([" | ".join(clean_row)])

                # 2. 如果抓到了数据，且数据发生了变化（比如你换了搜索词）
                if current_data:
                    current_top_price = current_data[0][0]
                    if current_top_price != last_top_price:
                        print(f"\n✨ 监测到新数据 (时间: {asyncio.get_event_loop().time():.2f})")
                        print(tabulate(current_data, headers=["当前可见报价"], tablefmt="grid"))
                        last_top_price = current_top_price
                
                # 3. 每 3 秒扫描一次，不给服务器增加负担
                await asyncio.sleep(3)

        except Exception as e:
            print(f"🚨 哨兵巡逻中断: {e}")
        finally:
            await self.stop()

if __name__ == "__main__":
    sentinel = LowKeySentinel(headless=False)
    asyncio.run(sentinel.monitor_prices())