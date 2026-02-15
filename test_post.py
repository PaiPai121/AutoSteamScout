import asyncio
from playwright.async_api import async_playwright
import os
async def run_listing_test():
    async with async_playwright() as p:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        user_data_dir = os.path.join(current_dir, "SteamPY-Scout", "steampy_data")
        
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"]
        )
        
        page = context.pages[0] if context.pages else await context.new_page()

        # --- 步骤 1：先去首页打个卡 ---
        print("🌐 正在进入首页掩护...")
        await page.goto("https://steampy.com/home")
        await asyncio.sleep(2) # 假装看一眼公告

        # --- 步骤 2：模拟鼠标点击进入“个人中心”或“卖家中心” ---
        # 根据 SteamPy 的布局，通常需要点击右上角的头像或导航栏
        print("🖱️ 模拟真人操作：点击导航进入发布页...")
        try:
            # 1. 尝试找到“卖家中心”或类似字样并点击
            # 如果菜单是悬停出的，还要模拟 hover
            await page.hover("text='CDKey市场'") 
            await asyncio.sleep(0.5)
            
            # 2. 点击“发布商品”按钮
            # 注意：这里需要根据你屏幕上实际看到的文字微调
            await page.click("text='发布商品'")
            
            print("✅ 成功通过导航路径进入发布页。")
        except Exception as e:
            print(f"⚠️ 模拟点击失败，尝试降级方案：从首页模拟点击跳转...")
            # 如果找不到按钮，我们通过首页的一个内部链接跳转，这样 Referer 就是首页了
            await page.evaluate("() => { window.location.href = '/sell/postItem'; }")

        # --- 步骤 3：进入表单后的行为模拟 ---
        await page.wait_for_selector("input[placeholder*='搜索并选择游戏']")
        
        # 随机等几秒，假装在找 Key 字符串
        print("⏳ 假装在粘贴 Key，稍等...")
        await asyncio.sleep(3)

        # ... 后续填表逻辑保持不变 ...

if __name__ == "__main__":
    asyncio.run(run_listing_test())