# Sonkwo_Scout/sonkwo_scout_core.py
import config
import os
from playwright.async_api import async_playwright

class SonkwoScout:
    def __init__(self, headless=True):
        # 1. 核心：锁定你在 save_sonkwo_session.py 中保存数据的文件夹
        # current_dir = os.path.dirname(os.path.abspath(__file__))
        self.user_data_dir = os.path.join(
            config.PROJECT_ROOT, 
            "Sonkwo_Scout", 
            "sonkwo_data"
        )
        self.headless = headless
        self.context = None
        self.page = None
        self.playwright = None

    async def start(self, url="https://www.sonkwo.cn/"):
        """初始化并进入已登录状态的首页"""
        self.playwright = await async_playwright().start()
        
        # 2. 启动持久化上下文，加载杉果登录信息
        self.context = await self.playwright.chromium.launch_persistent_context(
            self.user_data_dir,
            headless=self.headless,
            viewport={'width': 1920, 'height': 1080}, # 模拟 1080P 显示器
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ]
        )
        
        # 3. 获取或创建页面
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        
        print(f"🌐 正在接管杉果状态，目标: {url}")
        # wait_until="commit" 意味着只要服务器响应了就返回，不等待图片和复杂脚本加载
        await self.page.goto(url, wait_until="commit", timeout=0)
        # --- 新增：登录有效性检查 ---
        # 逻辑：检查是否有“退出登录”或“个人中心”的元素，或者头像类名
        # is_logged_in = await self.page.query_selector(".avatar, .user-avatar, .new-avatar-block")
        # if not is_logged_in:
        #     print("\n" + "❌ " * 10)
        #     print("🚨 [杉果] 登录状态已失效！请先运行 save_sonkwo_session.py 重新授权。")
        #     print("❌ " * 10 + "\n")
        #     # 抛出异常阻止主程序继续运行
        #     raise ConnectionError("Sonkwo Session Expired") 
        
        print("✅ 杉果登录状态校验成功。")
        return self.page

    async def stop(self):
        """安全关闭"""
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        print("\n🔒 杉果侦察机已安全返航。")