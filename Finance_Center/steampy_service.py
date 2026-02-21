import asyncio
import sys
import os
import re
import json
import datetime

class SteamPyService:
    def __init__(self, context):
        self.context = context
        self.live_shot = "blackbox/steampy_live.png"
        self.live_html = "blackbox/steampy_debug.html"
        self.save_dir = "blackbox/steampy_service"
        self.sales_file = "data/steampy_sales.json"
        
        os.makedirs(self.save_dir, exist_ok=True)
        os.makedirs("data", exist_ok=True)
        self.step_idx = 0

    async def _log_and_shot(self, page, action_name):
        self.step_idx += 1
        try:
            await page.screenshot(path=self.live_shot)
            content = await page.content()
            with open(self.live_html, "w", encoding="utf-8") as f:
                f.write(content)
            archive_path = f"{self.save_dir}/step_{self.step_idx}_{action_name}"
            await page.screenshot(path=f"{archive_path}.png")
        except: pass

    async def action_fetch_seller_ledger(self, page):
        """🚀 [全量翻页审计] 自动遍历所有页面并抓取挂单"""
        try:
            # 1. 前置导航逻辑 (保持不变)
            print("[SteamPy] 🕵️ 正在同步首页状态...")
            await page.goto("https://steampy.com/home", wait_until="networkidle")
            await asyncio.sleep(2)
            
            print("🖱️ 正在展开卖家中心菜单...")
            seller_menu = page.get_by_text("卖家中心").first
            await seller_menu.click()
            await asyncio.sleep(1)

            print("🚀 正在突入卖家中心-CDK...")
            cdk_link = page.get_by_text("卖家中心-CDK").first
            await cdk_link.click()
            await asyncio.sleep(4) 

            # 2. 循环翻页抓取
            all_entries = []
            page_num = 1
            
            while True:
                print(f"📄 正在扫描第 {page_num} 页...")
                await page.wait_for_selector(".list-item", timeout=10000)
                items = await page.query_selector_all(".list-item")
                
                for item in items:
                    # ... (这里是之前的提取 logic) ...
                    time_el = await item.query_selector(".createTime")
                    name_el = await item.query_selector(".steamGameName")
                    price_els = await item.query_selector_all(".gameTotal") 
                    status_el = await item.query_selector(".tc.w7 .gameTotal")

                    if time_el and name_el and len(price_els) >= 3:
                        all_entries.append({
                            "order_time": (await time_el.text_content()).strip(),
                            "name": (await name_el.text_content()).strip(),
                            "stock": (await price_els[0].text_content()).strip(),
                            "market_price": (await price_els[1].text_content()).strip(),
                            "my_price": (await price_els[2].text_content()).strip(),
                            "status": (await status_el.text_content()).strip() if status_el else "出售"
                        })

                # 3. 寻找“下一页”按钮并判断是否结束
                next_btn = await page.query_selector(".ivu-page-next")
                if not next_btn:
                    print("🏁 未发现分页器，扫描结束。")
                    break
                
                # 检查是否是最后一页
                is_disabled = await next_btn.get_attribute("class")
                if "ivu-page-disabled" in is_disabled:
                    print(f"🏁 已到达最后一页 (共 {page_num} 页)，侦察完毕。")
                    break
                
                # 点击下一页并等待
                await next_btn.click()
                page_num += 1
                await asyncio.sleep(3) # 给渲染留出缓冲时间
                await self._log_and_shot(page, f"SYNC_PAGE_{page_num}")

            # 4. 落地存储
            with open(self.sales_file, "w", encoding="utf-8") as f:
                json.dump(all_entries, f, ensure_ascii=False, indent=4)
            
            print("-" * 100)
            print(f"✅ 全量审计完成：共发现 {len(all_entries)} 条跨页挂单记录")
            return True

        except Exception as e:
            print(f"❌ 翻页审计失败: {e}")
            return False


    async def enter_interactive_mode(self):
        """交互分机主循环"""
        print("\n" + "💰 " * 12 + "\n【SteamPY 卖家审计】就绪\n" + "💰 " * 12 + "\n")
        page = await self.context.new_page()
        try:
            while True:
                sys.stdout.write(f"\r[SteamPy分机] >> ")
                sys.stdout.flush()
                cmd_raw = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
                if not cmd_raw: break
                cmd = cmd_raw.strip().lower()

                if cmd == "exit": break
                elif cmd == "sync": # 🚀 指挥官，只需输入 sync 即可完成全自动流程
                    await self.action_fetch_seller_ledger(page)
                elif cmd == "shot":
                    await self._log_and_shot(page, "MANUAL_SHOT")
                else:
                    print(f"❓ 未知指令: {cmd} (可用指令: sync, shot, exit)")
        finally:
            if not page.is_closed(): await page.close()
            print("🔙 已返回主控制台。")