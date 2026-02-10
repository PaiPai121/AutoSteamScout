import asyncio
from steampy_scout_core import SteamPyScout

class MarketNavigator(SteamPyScout):
    async def go_to_china_market(self):
        page = await self.start()
        if not page:
            return None

        try:
            print("🖱️ 正在尝试点击 'CDKey市场' 菜单...")
            # 1. 寻找并点击一级菜单
            # 使用文本匹配最稳健，因为类名可能变，但字不会变
            cdkey_menu = await page.wait_for_selector("text=CDKey市场", timeout=10000)
            await cdkey_menu.click()
            await asyncio.sleep(1) # 等待菜单展开动画

            print("🖱️ 正在选择 '国区'...")
            # 2. 寻找并点击二级菜单 '国区'
            # 锁定在 ivu-menu-item 内的国区字样，防止点错
            china_region = await page.wait_for_selector(".ivu-menu-item:has-text('国区')", timeout=5000)
            await china_region.click()

            # 3. 等待市场列表加载的标志（比如搜索框或者价格符号）
            # 3. 等待市场列表加载的标志
            print("⏳ 正在进入国区 CDKey 市场...")
            # 我们分别等搜索框出现，或者等任意包含“¥”的元素出现
            await page.wait_for_selector(".ivu-input", timeout=20000)
            print("✅ 搜索框已加载。")
            
            # 这里虽然有时会超时，但如果它过了，我们就继续
            price_marker = page.get_by_text("¥")
            await price_marker.first.wait_for(state="attached", timeout=20000) # 改为 attached 降低门槛
            
            print("✅ 报价列表渲染成功！")
            
            # --- 仅在成功后添加这一小段抓取逻辑，不做其他改动 ---
            print("📊 正在提取前 5 条报价供确认...")
            await asyncio.sleep(2) # 给 Vue 最后一点同步时间
            
            # 优先从容器提取
            rows = await page.query_selector_all(".item-list-item, .ivu-table-row, .ivu-card")
            found_count = 0
            
            print("\n" + "="*50)
            print("📋 实时报价抓取结果：")
            print("-" * 50)

            for row in rows:
                txt = await row.text_content()
                if txt and ("¥" in txt or "￥" in txt):
                    clean_txt = " | ".join([s.strip() for s in txt.split() if s.strip()])
                    if len(clean_txt) > 20:
                        print(f"[{found_count+1}] {clean_txt}")
                        found_count += 1
                if found_count >= 10: break

            if found_count == 0:
                print("🕵️ 尝试暴力全页扫描...")
                body_text = await page.inner_text("body")
                for line in body_text.split('\n'):
                    if ("¥" in line or "￥" in line) and len(line) > 10:
                        print(f"🔥 捕获: {line.strip()}")
            
            print("="*50)
            return page

        except Exception as e:
            print(f"❌ 导航失败: {e}")
            print("💡 提示：如果脚本找不到菜单，请尝试在浏览器手动点一下，看脚本是否能继续。")
            return page

async def run_and_wait():
    nav = MarketNavigator(headless=False)
    page = await nav.go_to_china_market()
    
    if page:
        print("\n📢 任务已完成！数据已显示在上方。")
        print("💡 浏览器已进入【手动接管模式】，你可以继续操作。")
        print("⌨️  按【回车键】或在终端按 Ctrl+C 退出并关闭浏览器...")
        
        # 这个 loop.run_in_executor 让异步程序停下来等待用户输入
        # 从而完美避免了 Event Loop 提前关闭导致的报错
        await asyncio.get_event_loop().run_in_executor(None, input)
    
    await nav.stop()

if __name__ == "__main__":
    # 测试导航
    try:
        asyncio.run(run_and_wait())
    except KeyboardInterrupt:
        print("\n👋 用户中断，正在关闭...")