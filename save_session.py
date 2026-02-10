import asyncio
import os
from playwright.async_api import async_playwright

async def save_steampy_session():
    async with async_playwright() as p:
        # 指定存储路径（建议放在项目根目录）
        user_data_dir = os.path.join(os.getcwd(), "steampy_data")
        
        # 启动持久化上下文
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,  # 必须开启窗口，方便你操作
            args=["--disable-blink-features=AutomationControlled"]
        )
        
        page = context.pages[0] if context.pages else await context.new_page()
        
        print("🌐 正在打开登录页面，请完成登录...")
        await page.goto("https://steampy.com/login", timeout=0)

        # 循环监测状态
        print("⏳ 脚本正在监测中。登录成功并看到个人中心/首页后，请回到这里...")
        
        try:
            # 只要 URL 不再包含 'login'，说明已经进入了已登录区域
            while "login" in page.url:
                await asyncio.sleep(2)
            
            # 给浏览器 5 秒钟来写入所有的本地缓存文件
            print("✅ 检测到跳转成功！正在写入 Session 数据到本地...")
            await asyncio.sleep(5)
            print(f"🎉 登录信息已保存至: {user_data_dir}")
            
        except KeyboardInterrupt:
            print("\n🛑 用户强制停止。")
        
        finally:
            await context.close()
            print("🔒 浏览器已关闭。下次运行抓取脚本时，将自动继承该状态。")

if __name__ == "__main__":
    asyncio.run(save_steampy_session())