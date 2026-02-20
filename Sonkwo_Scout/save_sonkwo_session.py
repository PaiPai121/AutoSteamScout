import asyncio
import os
import sys
import datetime
from playwright.async_api import async_playwright

# 💡 截图存放目录
SHOT_DIR = "blackbox/session_debug"
LIVE_PATH = "blackbox/session_live.png"

async def take_shot(page, step_name):
    """📸 截图辅助函数：同步更新直播图并按步存档"""
    os.makedirs(SHOT_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%H%M%S")
    path = os.path.join(SHOT_DIR, f"step_{timestamp}_{step_name}.png")
    try:
        await page.screenshot(path=path)
        await page.screenshot(path=LIVE_PATH) # 覆盖直播图，方便实时查看
        print(f"📸 [截图已保存] {step_name}")
    except Exception as e:
        print(f"⚠️ 截图失败: {e}")

async def save_sonkwo_session_universal():
    async with async_playwright() as p:
        user_data_dir = os.path.join(os.getcwd(), "sonkwo_data")
        
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=True, # 无头模式下截图至关重要
            args=["--disable-blink-features=AutomationControlled"]
        )
        
        page = context.pages[0] if context.pages else await context.new_page()
        
        # --- 步骤 1: 打开页面 ---
        print("🌐 正在打开杉果登录页面...")
        await page.goto("https://www.sonkwo.cn/sign_in", timeout=60000)
        await asyncio.sleep(2) 
        await take_shot(page, "01_open_signin_page")

        try:
            # --- 步骤 2: 切换登录方式 ---
            print("鼠标 正在切换至手机验证码登录...")
            tab_selector = ".right_login .login-tab-button"
            await page.wait_for_selector(tab_selector, state="visible")
            await page.click(tab_selector)
            await asyncio.sleep(1)
            await take_shot(page, "02_switched_to_phone_login")
            
            # --- 步骤 3: 填入手机号 ---
            phone_input_selector = "#phone_number"
            await page.wait_for_selector(phone_input_selector, state="visible")
            
            phone_number = input("\n📱 请输入手机号: ").strip()
            print("🎯 正在填入手机号...")
            await page.focus(phone_input_selector)
            await page.type(phone_input_selector, phone_number, delay=100)
            await take_shot(page, "03_phone_filled")
            
            # --- 步骤 4: 发送验证码 ---
            print("📩 正在尝试发送验证码...")
            send_btn_selector = ".code-btn button"
            await page.wait_for_selector(send_btn_selector, state="visible")
            await page.click(send_btn_selector)
            
            # 💡 关键：发送后等一下，看看是否弹出了滑动验证码
            await asyncio.sleep(2)
            await take_shot(page, "04_after_send_code_click")
            print("💡 请检查 blackbox/session_live.png 确认是否触发了滑块验证或发送成功")

            # --- 步骤 5: 输入验证码 ---
            verify_code = input("⌨️ 请输入收到的 6 位验证码 (若画面有滑块请先手动处理或重试): ").strip()
            code_input_selector = "#pending_phone_number_token"
            await page.wait_for_selector(code_input_selector, state="visible")
            await page.fill(code_input_selector, verify_code)
            await take_shot(page, "05_code_filled")

            # --- 步骤 6: 提交登录 ---
            print("🚀 提交登录...")
            await page.click("button.new_orange")
            await asyncio.sleep(3)
            await take_shot(page, "06_after_submit")

            # --- 步骤 7: 同步持久化 ---
            print("\n⏳ 执行跨平台同步保护...")
            await page.goto("https://www.sonkwo.cn/categories", wait_until="networkidle")
            await take_shot(page, "07_final_sync_page")
            
            print(f"🎉 状态锁定完成。")

        except Exception as e:
            print(f"\n🚨 流程发生错误: {e}")
            await take_shot(page, "error_occurred")
            input("💡 请检查截图后按 Enter 退出...")
            
        finally:
            if context:
                print("🔒 正在关闭浏览器并执行最终磁盘同步...")
                await context.close()
            print(f"✅ Session 目录已就绪: {user_data_dir}")

if __name__ == "__main__":
    asyncio.run(save_sonkwo_session_universal())