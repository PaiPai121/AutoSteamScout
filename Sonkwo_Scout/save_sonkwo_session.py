import asyncio
import os
import sys
from playwright.async_api import async_playwright

async def save_sonkwo_session_universal():
    async with async_playwright() as p:
        # 1. 使用 os.path.join 确保路径分隔符在 Windows/Linux/macOS 下均正确
        user_data_dir = os.path.join(os.getcwd(), "sonkwo_data")
        
        # 2. 启动持久化上下文，全平台通用参数
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=True,
            # 禁用自动化控制标记，增加全平台登录成功率
            args=["--disable-blink-features=AutomationControlled"]
        )
        
        page = context.pages[0] if context.pages else await context.new_page()
        print("🌐 正在打开杉果登录页面...")
        await page.goto("https://www.sonkwo.cn/sign_in", timeout=6000000)

        try:
            # 3. 切换至手机验证码登录 (通用 ID 和 Class 定位)
            print("🖱️ 正在切换至手机验证码登录...")
            tab_selector = ".right_login .login-tab-button"
            await page.wait_for_selector(tab_selector, state="visible")
            await page.click(tab_selector)
            
            # 4. 填写手机号 (基于 HTML 结构中的 id="phone_number")
            phone_input_selector = "#phone_number"
            await page.wait_for_selector(phone_input_selector, state="visible")
            
            phone_number = input("\n📱 请输入手机号: ").strip()
            print("🎯 正在填入手机号...")
            await page.focus(phone_input_selector)
            # 使用 type 模拟物理按键以触发全平台浏览器的 JS 事件
            await page.type(phone_input_selector, phone_number, delay=100)
            
            # 5. 发送验证码
            print("📩 正在尝试发送验证码...")
            send_btn_selector = ".code-btn button"
            await page.wait_for_selector(send_btn_selector, state="visible")
            await page.click(send_btn_selector)

            # 6. 输入验证码 (基于 HTML 结构中的 id="pending_phone_number_token")
            verify_code = input("⌨️ 请输入收到的 6 位验证码: ").strip()
            code_input_selector = "#pending_phone_number_token"
            await page.wait_for_selector(code_input_selector, state="visible")
            await page.fill(code_input_selector, verify_code)

            # 7. 提交登录
            print("🚀 提交登录...")
            await page.click("button.new_orange")

            # 8. 全平台强制同步逻辑
            # 在全平台下，跳转并等待网络空闲是触发 Cookie 写入磁盘最稳健的方式
            print("\n⏳ 登录已提交，正在执行跨平台同步保护...")
            
            # 冗余等待，确保后端响应完成
            for i in range(10, 0, -1):
                sys.stdout.write(f"\r同步倒计时: {i} 秒...")
                sys.stdout.flush()
                await asyncio.sleep(1)
            
            # 强制跳转到静态页面以触发内核 Flush
            print("\n🔄 触发深度持久化同步...")
            await page.goto("https://www.sonkwo.cn/categories", wait_until="networkidle", timeout=3000000)
            
            # 额外物理缓冲
            await asyncio.sleep(3) 
            print(f"🎉 状态锁定完成。")

        except Exception as e:
            print(f"\n🚨 流程发生错误: {e}")
            print("💡 如果自动操作失败，请在浏览器中手动完成登录，成功后回到这里按 Enter 键。")
            input()
            
        finally:
            # 9. 显式关闭 context 是全平台保存 Session 的最终关卡
            if context:
                print("🔒 正在关闭浏览器并执行最终磁盘同步...")
                await context.close()
            print(f"✅ Session 目录已就绪，全平台可无感调用: {user_data_dir}")

if __name__ == "__main__":
    asyncio.run(save_sonkwo_session_universal())