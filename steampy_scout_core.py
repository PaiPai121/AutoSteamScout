import asyncio
import os
from playwright.async_api import async_playwright

class SteamPyScout:
    def __init__(self, headless=False):
        self.user_data_dir = os.path.join(os.getcwd(), "steampy_data")
        self.headless = headless
        self.context = None
        self.browser_instance = None

    async def start(self):
        """初始化并进入已登录状态的首页"""
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            self.user_data_dir,
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"]
        )
        page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        
        print("🌐 正在接管 SteamPY 状态...")
        await page.goto("https://steampy.com/home", wait_until="commit", timeout=0)
        
        print("⏳ 正在等待动态 DOM 渲染...")
        try:
            # 锁定你刚才测试通过的关键元素
            await page.wait_for_selector(".ivu-menu-submenu-title", timeout=60000)
            print("✅ 状态确认：'CDKey市场' 已挂载，系统准备就绪。")
            return page
        except Exception as e:
            print(f"❌ 初始化失败，页面可能未正常加载: {e}")
            return None

    async def stop(self):
        """安全关闭"""
        if self.context:
            await self.context.close()
        if hasattr(self, 'playwright'):
            await self.playwright.stop()
        print("🔒 侦察机已安全返航。")

# --- 使用示例（你可以直接运行这个脚本进行最后的确认） ---
async def main():
    scout = SteamPyScout(headless=False)
    page = await scout.start()
    
    if page:
        print("🎉 [核心模块测试通过] 你现在可以人工在浏览器操作，或者在此之后添加其他逻辑。")
        # 保持 10 秒让你确认，然后才关闭
        await asyncio.sleep(10)
    
    await scout.stop()

if __name__ == "__main__":
    asyncio.run(main())