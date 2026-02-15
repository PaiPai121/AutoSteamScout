import asyncio
import os
import sys
from playwright.async_api import async_playwright

async def save_steampy_phone_login_interactive():
    async with async_playwright() as p:
        user_data_dir = os.path.join(os.getcwd(), "steampy_data")
        
        # 1. 启动持久化上下文
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,  # 强制 Headless 模式
            args=["--disable-blink-features=AutomationControlled"]
        )
        
        page = context.pages[0] if context.pages else await context.new_page()
        page.set_default_timeout(600000)
        
        print("🌐 [Headless] 正在进入 SteamPy 手机号登录流程...")
        await page.goto("https://steampy.com/login", wait_until="domcontentloaded")
        await asyncio.sleep(5) # 等待 iView 渲染

        try:
            # 2. 强制切换到“手机号登录” Tab
            # 根据 HTML，这是第二个 .ivu-tabs-tab
            print("📱 正在切换至手机号登录 Tab...")
            tabs = await page.query_selector_all(".ivu-tabs-tab")
            if len(tabs) >= 2:
                await tabs[1].click(force=True)
                await asyncio.sleep(1.5)
            
            # 3. 强制勾选“自动登录”和“协议”
            # 使用 JS 确保勾选状态机被触发
            print("✅ 正在自动勾选协议与自动登录...")
            checkboxes = await page.query_selector_all(".ivu-checkbox-input")
            for cb in checkboxes:
                await cb.evaluate("node => node.checked = true")
                await cb.evaluate("node => node.dispatchEvent(new Event('change', { bubbles: true }))")

            # 4. 输入手机号
            phone_num = input("\n📱 请输入手机号: ").strip()
            # 定位“请输入手机号”的输入框
            phone_input = await page.wait_for_selector("input[placeholder='请输入手机号']", state="visible")
            await phone_input.fill(phone_num)

            # 5. 点击“获取验证码”
            print("📩 正在请求发送短信验证码...")
            # 查找包含“获取验证码”文本的按钮
            send_btn = await page.wait_for_selector("button:has-text('获取验证码')", state="visible")
            await send_btn.click()
            print("✅ 短信已发送，请注意查收手机。")

            # 6. 输入短信验证码
            sms_code = input("💬 请输入收到的 6 位短信验证码: ").strip()
            code_input = await page.wait_for_selector("input[placeholder='请输入短信验证码']", state="visible")
            await code_input.fill(sms_code)

            # 7. 点击登录按钮
            print("🚀 正在提交登录...")
            # 锁定具有 .login-btn 类名的按钮
            await page.click("button.login-btn")

            # 8. 闭环验证：确认 Token 是否写入
            print("⏳ 正在验证并执行磁盘同步...")
            success = False
            for _ in range(20):
                # 嗅探 SteamPy 的内存凭证
                has_token = await page.evaluate("""
                    () => localStorage.getItem('accessToken') !== null || 
                           localStorage.getItem('userInfo') !== null
                """)
                if has_token:
                    success = True
                    break
                await asyncio.sleep(1)
                sys.stdout.write(".")
                sys.stdout.flush()

            if success:
                print("\n✅ 登录成功！正在锁定磁盘 I/O...")
                # 导出状态快照作为备份，同时触发 Flush
                await context.storage_state(path=os.path.join(user_data_dir, "state.json"))
                # 诱导刷新
                await page.goto("https://steampy.com/home", wait_until="domcontentloaded")
                await asyncio.sleep(5)
                print(f"🎉 SteamPy Session 已在 Headless 模式下安全锁定。")
            else:
                print("\n❌ 登录未成功，请检查验证码是否输入正确或已过期。")

        except Exception as e:
            print(f"\n🚨 运行异常: {e}")
        finally:
            if context:
                await context.close()
            print(f"✅ 处理结束。Session 目录: {user_data_dir}")

if __name__ == "__main__":
    asyncio.run(save_steampy_phone_login_interactive())