import asyncio
import os
from playwright.async_api import async_playwright

class SteamPyScout:
    def __init__(self, headless=False):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.user_data_dir = os.path.join(current_dir, "steampy_data")
        self.headless = headless
        self.context = None
        self.browser_instance = None

    async def start(self, url="https://steampy.com/home"):
        """初始化并进入已登录状态的首页"""
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            self.user_data_dir,
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"]
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        
        print("🌐 正在接管  状态...")
        await self.page.goto(url, wait_until="commit", timeout=0)

        try:
            # 增加一点缓冲，等待 Vue 渲染侧边栏
            await self.page.wait_for_selector(".ivu-menu-submenu-title:has-text('CDKey市场')", timeout=100000)
            
            # 尝试寻找“退出登录”按钮或卖家中心标识
            is_login = await self.page.query_selector("li:has-text('退出登录'), .ivu-menu-submenu:has-text('卖家中心')")
            if not is_login:
                raise ValueError("Element Not Found")
            print("✅ SteamPy 登录状态确认。")
        except:
            print("\n" + "🚨 " * 10)
            print("⚠️ [SteamPy] 登录已失效或 Session 文件夹未正确加载。")
            print("👉 请运行 save_session.py 重新扫码。")
            print("🚨 " * 10 + "\n")
            raise ConnectionError("SteamPy Session Expired")
        if url == "https://steampy.com/home":
            print("⏳ 正在等待动态 DOM 渲染...")
            try:
                # 锁定你刚才测试通过的关键元素
                await self.page.wait_for_selector(".ivu-menu-submenu-title", timeout=60000)
                print("✅ 状态确认：'CDKey市场' 已挂载，系统准备就绪。")
                return self.page
            except Exception as e:
                print(f"❌ 初始化失败，页面可能未正常加载: {e}")
                return None
        else:
            print("⚠️ 访问了非预设首页，无法确认状态，但你可以继续操作。")
            return self.page

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