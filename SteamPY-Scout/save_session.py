import asyncio
import os
import sys
from playwright.async_api import async_playwright

async def save_steampy_headless_optimized():
    async with async_playwright() as p:
        # 确保路径指向 SteamPY-Scout 内部
        current_dir = os.path.dirname(os.path.abspath(__file__))
        user_data_dir = os.path.join(current_dir, "steampy_data")
        
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=True,  # 云端必须为 True
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        
        page = context.pages[0] if context.pages else await context.new_page()
        # 设置较大的超时，应对云端网络波动
        page.set_default_timeout(600000)
        
        print("🌐 [Sentinel] 正在进入 SteamPy 深度登录取证模式...")
        await page.goto("https://steampy.com/login", wait_until="networkidle")
        await asyncio.sleep(3)

        try:
            # 1. 强制切换 Tab
            print("📱 切换手机号登录...")
            tabs = await page.query_selector_all(".ivu-tabs-tab")
            if len(tabs) >= 2:
                await tabs[1].click(force=True)
            
            # 2. 强制勾选协议 (使用注入 JS 绕过点击拦截)
            print("✅ 注入协议勾选状态...")
            await page.evaluate("() => { document.querySelectorAll('.ivu-checkbox-input').forEach(c => { c.checked = true; c.dispatchEvent(new Event('change', { bubbles: true })); }); }")

            # 3. 输入手机号
            phone_num = input("\n📱 请输入手机号: ").strip()
            await page.fill("input[placeholder='请输入手机号']", phone_num)

            # 4. 请求验证码并截图诊断
            print("📩 发送验证码...")
            await page.click("button:has-text('获取验证码')")
            await asyncio.sleep(2)
            await page.screenshot(path="debug_after_sms.png")
            print("📸 [诊断] 已生成 debug_after_sms.png，若未收到短信请检查是否有滑动验证码。")

            # 5. 输入验证码
            sms_code = input("💬 请输入短信验证码: ").strip()
            await page.fill("input[placeholder='请输入短信验证码']", sms_code)

            # 6. 核心：强制执行登录逻辑并监控 LocalStorage
            print("🚀 提交登录指令...")
            # 这种点击方式能更好地触发 Vue 组件事件
            login_btn = await page.wait_for_selector("button.login-btn")
            await login_btn.click()

            # 7. 闭环验证：循环探测 Token 和 URL 变化
            print("⏳ 正在捕捉加密 Token...")
            success = False
            for i in range(15):
                # 检查两个关键存储项
                token_data = await page.evaluate("""
                    () => {
                        return localStorage.getItem('accessToken') || sessionStorage.getItem('accessToken');
                    }
                """)
                
                if token_data:
                    print(f"\n✨ 成功捕获 Token: {token_data[:15]}...")
                    success = True
                    break
                
                # 如果 URL 变成了 home，也算成功
                if "home" in page.url:
                    success = True
                    break
                    
                await asyncio.sleep(2)
                sys.stdout.write("🛰️ ")
                sys.stdout.flush()
                # 过程截图
                if i % 3 == 0:
                    await page.screenshot(path=f"debug_login_step_{i}.png")

            if success:
                print("\n✅ 验证通过。正在强制刷新并锁定磁盘...")
                # 诱导刷新以触发持久化存储
                await page.goto("https://steampy.com/home", wait_until="networkidle")
                await context.storage_state(path=os.path.join(user_data_dir, "state.json")) # 备份状态
                await asyncio.sleep(3)
                print(f"🎉 Session 已在无头模式下安全固化。")
            else:
                print("\n❌ 登录超时或被拦截。请检查生成的 debug_*.png 截图。")

        except Exception as e:
            print(f"\n🚨 关键路径故障: {e}")
        finally:
            await context.close()
            print(f"✅ 处理结束。")

if __name__ == "__main__":
    asyncio.run(save_steampy_headless_optimized())