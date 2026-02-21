import asyncio
import sys
import os
import re
import json
import datetime
import config

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
        if not getattr(config, "ENABLE_SCREENSHOTS", False):
            return
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
        """🚀 [SteamPY 全量审计] 翻页循环 + 登录态实时检测"""
        try:
            # 1. 初始导航（模拟真人路径）
            print("[SteamPy] 🕵️ 检查首页登录状态...")
            # await page.goto("https://steampy.com/home", wait_until="networkidle")
            # 💡 【核心修复】：不要用 networkidle，改用 domcontentloaded
            try:
                await page.goto("https://steampy.com/home", wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                print(f"⚠️ [导航微警报] 页面响应稍慢，但我们将尝试继续操作...")
            await asyncio.sleep(3)

            # 💡 [检查点 A]：首页预检
            if "login" in page.url:
                print("🚨 [SteamPY] 首页检测到登录失效，请检查 Session！")
                return False
            
            # 定位父菜单
            seller_menu_parent = page.locator("li.ivu-menu-submenu").filter(has_text="卖家中心").first
            # 💡 检查是否已经展开：查看 class 里是否有 'ivu-menu-opened'
            is_opened = await seller_menu_parent.evaluate('(el) => el.classList.contains("ivu-menu-opened")')
            
            if not is_opened:
                print("展开卖家中心菜单...")
                await seller_menu_parent.locator(".ivu-menu-submenu-title").click()
                await asyncio.sleep(0.5)
            else:
                print("卖家中心已处于展开状态，直接点击子项。")
            await asyncio.sleep(0.5)
            await page.get_by_text("卖家中心-CDK").first.click()
            
            all_entries = []
            page_num = 1

            while True:
                # 💡 [检查点 B]：循环内实时监控，防止翻页时掉线
                if "login" in page.url:
                    print(f"🚨 [SteamPY] 在第 {page_num} 页遭遇登录拦截，同步被迫中断！")
                    break

                print(f"\n📄 正在全息扫描 SteamPY 第 {page_num} 页...")
                
                try:
                    # 1. 强制等待元素“挂载”到 DOM 树上
                    # state='attached' 比默认状态更底层，能捕获到刚露头的 Vue 组件
                    await page.wait_for_selector(".list-item", timeout=15000, state='attached')
                    
                    # 2. 💡 给 Vue 一个“喘息时间”去完成数据绑定
                    # 有时候容器出来了，但里面的文字（价格、名称）还没填进去，这 0.5 秒非常救命
                    await asyncio.sleep(0.5) 
                    
                except Exception:
                    # 再次核对是否是因为被跳到了登录页才找不到元素
                    if "login" in page.url:
                        print(f"🚨 [SteamPY] 元素载入失败，确认已被踢出登录。")
                    elif "暂无数据" in await page.content():
                        print(f"🏁 第 {page_num} 页未检测到数据行（暂无数据），扫描正常结束。")
                    else:
                        print(f"🛑 第 {page_num} 页加载超时，可能网络波动。")
                    break

                # --- 📥 原始解析逻辑保持不动 ---
                items = await page.query_selector_all(".list-item")
                for item in items:
                    time_el = await item.query_selector(".createTime")
                    name_el = await item.query_selector(".steamGameName")
                    price_els = await item.query_selector_all(".gameTotal") 
                    status_el = await item.query_selector(".tc.w7 .gameTotal")

                    if time_el and name_el and len(price_els) >= 3:
                        otime = (await time_el.text_content()).strip()
                        gname = (await name_el.text_content()).strip()
                        stock = (await price_els[0].text_content()).strip()
                        market_p = (await price_els[1].text_content()).strip()
                        my_p = (await price_els[2].text_content()).strip()
                        status = (await status_el.text_content()).strip() if status_el else "出售"

                        print(f"{otime:<18} | {stock:<5} | {gname:<27} | {my_p} ({status})")

                        all_entries.append({
                            "order_time": otime, "name": gname, "stock": stock,
                            "market_price": market_p, "my_price": my_p, "status": status,
                            "sync_at": datetime.datetime.now().strftime("%H:%M:%S")
                        })
                # --- 📥 解析结束 ---

                # 2. 翻页判定
                next_btn = await page.query_selector(".ivu-page-next")
                if not next_btn:
                    print("🏁 页面无分页组件，全量抓取结束。")
                    break
                
                # 检查是否已禁用 (最后一页)
                is_disabled = await page.evaluate('(el) => el.classList.contains("ivu-page-disabled")', next_btn)
                if is_disabled:
                    print(f"🏁 已到达 SteamPY 末页 (共 {page_num} 页)")
                    break

                # 3. 执行翻页
                print(f"🔜 正在前往 SteamPY 第 {page_num + 1} 页...")
                await next_btn.click()
                page_num += 1
                
                # 💡 等待网络空闲和 Vue 渲染
                await asyncio.sleep(2.5) 

            # 4. 全量持久化
            if all_entries:
                with open(self.sales_file, "w", encoding="utf-8") as f:
                    json.dump(all_entries, f, ensure_ascii=False, indent=4)
                print(f"✅ 同步成功：{len(all_entries)} 条记录已入库至 {self.sales_file}")
                return True
            return False

        except Exception as e:
            print(f"❌ [SteamPY] 全量同步总崩溃: {e}")
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