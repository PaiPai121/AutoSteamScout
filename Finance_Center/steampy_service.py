import asyncio
import sys
import os
import re
import json
import datetime
import config
import random

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

    async def action_verify_and_goto_seller_cdk(self, page):
        """🚀 [SteamPY 导航] 带有实时战况汇报的版本"""
        try:
            print(f"[NAV] 🕵️ 启动导航程序，当前 URL: {page.url}")
            
            if "sellerCenterCDK" in page.url:
                print("[NAV] ✅ 已经在目标页面，跳过导航。")
                return True

            if page.url == "about:blank":
                print("[NAV] 🌐 初始探测：前往主页...")
                await page.goto("https://steampy.com/home", wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)

            # 登录状态检查
            if "login" in page.url:
                print(f"🚨 [NAV] 拦截告警：当前处于登录页，请检查登录态！")
                return False

            print("[NAV] 🖱️ 寻找卖家中心菜单块...")
            seller_menu_parent = page.locator("li.ivu-menu-submenu").filter(has_text="卖家中心").first
            
            if await seller_menu_parent.count() == 0:
                print("⚠️ [NAV] 菜单组件未找到，执行物理强跳...")
                await page.goto("https://steampy.com/sellerCenterCDK", wait_until="networkidle")
            else:
                is_opened = await seller_menu_parent.evaluate('(el) => el.classList.contains("ivu-menu-opened")')
                if not is_opened:
                    print("[NAV] 📂 菜单已锁定，正在执行展开动作...")
                    await seller_menu_parent.locator(".ivu-menu-submenu-title").click()
                    await asyncio.sleep(0.8)
                
                print("[NAV] 🖱️ 点击子项：卖家中心-CDK...")
                cdk_btn = page.get_by_text("卖家中心-CDK").first
                await cdk_btn.wait_for(state="visible", timeout=5000)
                await cdk_btn.click()

            print("[NAV] ⏳ 等待页面路由重定向完成...")
            try:
                # 只要包含 sellerCDKey 就算成功
                await page.wait_for_url(lambda url: "sellerCDKey" in url, timeout=10000)
                print(f"[NAV] 🎯 路由达成: {page.url}")
            except Exception as e:
                # 如果超时了，我们做一次最后的物理检查
                current_url = page.url
                if "sellerCDKey" in current_url:
                    print(f"[NAV] ⚠️ 逻辑同步微调：URL 已到位 ({current_url})，继续任务。")
                else:
                    raise e # 真的没跳过去，才报错

            await page.wait_for_load_state("networkidle")
            return True
        except Exception as e:
            print(f"❌ [NAV ERROR] 导航链路中断: {str(e)}")
            import traceback
            traceback.print_exc() # 打印完整堆栈，揪出幕后真凶
            return False

    def _clean_price(self, price_str):
        """将带有货币符号的字符串转为浮点数"""
        if not price_str: return 0.0
        try:
            cleaned = re.sub(r'[^\d.]', '', str(price_str))
            return float(cleaned) if cleaned else 0.0
        except: return 0.0

    async def action_fetch_seller_ledger(self, page):
        """🚀 [SteamPY 绝对闭环审计] 文字锚点锁定 + 自动去重存档"""
        print("\n" + "🛰️ " * 10 + "\n[AUDIT] 启动最高优先级扫描...")
        if not await self.action_verify_and_goto_seller_cdk(page): return False

        try:
            # 1. 定位并点击 Tab
            print("[AUDIT] 📡 正在物理锁定【库存总列表】选项卡...")
            inventory_tab = page.locator(".ivu-tabs-tab").filter(has_text="库存总列表").first
            await inventory_tab.click()
            print("[AUDIT] ✅ 已点击选项卡，等待数据流注入...")

            # 2. 等待渲染关键字（只要出现了“出库”或“未出库”，说明列表加载完了）
            try:
                await page.wait_for_selector("text=未出库", timeout=10000)
            except:
                await page.wait_for_selector("text=出库", timeout=5000)

            await asyncio.sleep(2) # 强行给 Vue 2秒渲染缓冲
            
            all_entries = []
            page_num = 1

            while True:
                # 3. 只抓取包含有效状态字的行，彻底过滤干扰
                items = page.locator(".list-item").filter(has_text=re.compile(r"出库|未出库"))
                items_count = await items.count()
                print(f"📄 [SCAN-P{page_num}] 探测到 {items_count} 条有效资产记录")
                
                for i in range(items_count):
                    item = items.nth(i)
                    cols = item.locator("> div")
                    
                    # 索引校准：[1]游戏名, [3]CDK, [4]更新时间, [5]金额, [6]状态
                    gname = (await cols.nth(1).text_content()).strip()
                    cdk = (await cols.nth(3).text_content()).strip()
                    otime = (await cols.nth(4).text_content()).strip()
                    raw_price = (await cols.nth(5).text_content()).strip()
                    status_text = (await cols.nth(6).text_content()).strip()

                    if len(cdk) > 5 and "-" in cdk and ":" not in cdk:
                        exact_price = self._clean_price(raw_price)
                        tag = "SOLD" if "出库" in status_text else "STOCK"
                        
                        all_entries.append({
                            "order_id": f"SPY_{otime.replace('-','').replace(':','').replace(' ','')}",
                            "name": gname, 
                            "price": exact_price, 
                            "cd_key": cdk,
                            "status": status_text, 
                            "order_tag": tag, 
                            "order_time": otime,
                            "sync_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
                        # 加上 cdk[:5]*** 让它重新显示          
                        print(f"      🎯 [精确定位] {gname[:10]} | ¥{exact_price:<6} | {status_text} | Key: {cdk[:5]}***")
                # 1. 保险判定：如果这一页数据不满 10 条，说明肯定是最后一页了
                if items_count < 10:
                    print(f"[AUDIT] ✅ 本页仅 {items_count} 条数据，判定为末页，审计结束。")
                    break

                next_btn = page.locator(".ivu-page-next:visible").first

                # 3. 检查按钮是否点不动了（变灰）
                is_disabled = await next_btn.evaluate('(el) => el.classList.contains("ivu-page-disabled")')
                if is_disabled:
                    print(f"[AUDIT] ✅ 路径终点达成 (共 {page_num} 页)")
                    break

                # 4. 执行拟人化翻页动作
                think_time = random.uniform(2.5, 4.5)
                print(f"[AUDIT] 🧠 模拟真人思考 {think_time:.1f}s 后前往第 {page_num + 1} 页...")
                await asyncio.sleep(think_time)
                
                await next_btn.scroll_into_view_if_needed()
                # 记录翻页前最后一条指纹的前5位，作为对标参照
                last_fingerprint = cdk[:5]
                
                await next_btn.click(force=True)
                page_num += 1
                print(f"[AUDIT] 🔜 翻页信号已发出，正在进行内容指纹验证...")

                # 💡 核心防抖：循环检测，直到第一条数据的 CDK 前 5 位发生变化（或超时）
                for _ in range(10): # 最多等 5 秒
                    await asyncio.sleep(0.5)
                    try:
                        new_first_item = page.locator(".list-item").filter(has_text=re.compile(r"出库|未出库")).first
                        new_cols = new_first_item.locator("> div")
                        new_cdk = (await new_cols.nth(3).text_content()).strip()
                        if new_cdk[:5] != last_fingerprint:
                            break # 验证成功：内容已刷新
                    except: pass

            # ==========================================
            # 💾 [核心持久化：存档与合并逻辑]
            # ==========================================
            if all_entries:
                existing_data = []
                # 如果账本已存在，先读出来
                if os.path.exists(self.sales_file):
                    try:
                        with open(self.sales_file, "r", encoding="utf-8") as f:
                            existing_data = json.load(f)
                    except Exception as e:
                        print(f"⚠️ 读取旧账本失败: {e}")

                # 以 CDKey 为唯一 ID 进行合并
                # 这样即使同一个 Key 被扫到两次，也只会保留最新状态
                # full_map = { item['cd_key']: item for item in existing_data }
                full_map = { item['cd_key']: item for item in existing_data if 'cd_key' in item }
                for new_item in all_entries:
                    full_map[new_item['cd_key']] = new_item
                
                final_list = list(full_map.values())
                
                # 写入文件
                with open(self.sales_file, "w", encoding="utf-8") as f:
                    json.dump(final_list, f, ensure_ascii=False, indent=4)
                
                print(f"[AUDIT] 📈 存档结案！SteamPY 资产库已同步，当前共 {len(final_list)} 条独立记录。")
                return True
            
            print("[AUDIT] ⚠️ 未发现任何有效数据。")
            return False

        except Exception as e:
            print(f"🚨 [AUDIT FATAL] 进程异常中断: {e}")
            import traceback
            traceback.print_exc()
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
                elif cmd == "goto": 
                    # 🚀 先行一步，抵达战场
                    await self.action_verify_and_goto_seller_cdk(page)
                elif cmd == "sync": # 🚀 指挥官，只需输入 sync 即可完成全自动流程
                    await self.action_fetch_seller_ledger(page)
                elif cmd == "shot":
                    await self._log_and_shot(page, "MANUAL_SHOT")
                elif cmd.lower().startswith("tap "):
                    target = cmd[4:].strip()
                    print(f"🎯 尝试打击目标: {target}")
                    try:
                        # 先尝试 Class/ID，再尝试 Text
                        selector = page.locator(target).first
                        if await selector.count() == 0:
                            selector = page.get_by_text(target).first
                        
                        await selector.click()
                        print(f"✅ 点击成功，观察 live.png 变化")
                    except Exception as e:
                        print(f"❌ 打击失败: {e}")

                # --- 核心：强制等待 ---
                elif cmd.lower().startswith("wait "):
                    try:
                        secs = int(cmd.split(" ")[-1])
                        print(f"⏳ 停船观察 {secs} 秒...")
                        await asyncio.sleep(secs)
                    except: pass
                else:
                    print(f"❓ 未知指令: {cmd} (可用指令: sync, shot, exit)")
        finally:
            if not page.is_closed(): await page.close()
            print("🔙 已返回主控制台。") 