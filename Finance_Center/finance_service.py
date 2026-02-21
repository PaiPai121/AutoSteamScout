import asyncio
import sys
import os
import re
import json
import datetime

class FinanceService:
    def __init__(self, context):
        self.context = context
        self.live_shot = "blackbox/finance_live.png"
        self.live_html = "blackbox/finance_debug.html"
        self.save_dir = "blackbox/finance_service"
        self.ledger_file = "data/purchase_ledger.json"
        self.blacklist_file = "data/finance_blacklist.json"
        
        os.makedirs(self.save_dir, exist_ok=True)
        os.makedirs("data", exist_ok=True)
        
        self.step_idx = 0
        self.blacklist = self._load_blacklist()

    def _load_blacklist(self):
        if os.path.exists(self.blacklist_file):
            try:
                with open(self.blacklist_file, 'r', encoding='utf-8') as f:
                    return set(json.load(f))
            except: return set()
        return set()

    def _save_blacklist(self):
        with open(self.blacklist_file, 'w', encoding='utf-8') as f:
            json.dump(list(self.blacklist), f, ensure_ascii=False, indent=4)

    async def _log_and_shot(self, page, action_name):
        """📸 视觉存档：截图 + 源码落地"""
        self.step_idx += 1
        try:
            await page.screenshot(path=self.live_shot)
            content = await page.content()
            with open(self.live_html, "w", encoding="utf-8") as f:
                f.write(content)
            # 物理存档
            archive_prefix = f"{self.save_dir}/step_{self.step_idx}_{action_name}"
            await page.screenshot(path=f"{archive_prefix}.png")
            print(f"📺 [LIVE] {action_name} 已同步至 blackbox")
        except: pass

    async def action_verify_and_goto_orders(self, page):
        """🚀 探测登录态并跳转 (修复了选择器报错问题)"""
        try:
            print("[FINANCE] 🕵️ 启动探测程序...")
            await page.goto("https://www.sonkwo.cn", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3) # 给 Vue 组件留出挂载时间

            # 1. 尝试多种方式探测“已登录”标志（头像或用户名链接）
            # 修复点：不再混合使用 CSS 和 Text 语法，改用纯 CSS
            login_selectors = [".avatar-block", ".user_avatar_component", "a[href*='/users/']"]
            is_logged_in = False
            for selector in login_selectors:
                if await page.query_selector(selector):
                    is_logged_in = True
                    break
            if not is_logged_in and ("orders" in page.url or "setting" in page.url):
                is_logged_in = True
            if is_logged_in:
                print("✅ 探测到登录态，执行瞬移...")
                await page.goto("https://www.sonkwo.cn/setting/orders", wait_until="networkidle", timeout=30000)
            else:
                print("🚨 未探测到头像，尝试点击登录刷新...")
                # 修复点：分步查找，避免 Unexpected token 报错
                login_locator = page.get_by_text("登录").first
                if await login_locator.count() > 0:
                    await login_locator.click()
                    await asyncio.sleep(3)
            
            # 2. 最终位置校验
            if "orders" in page.url or "club" in page.url:
                print(f"🎯 到达目标: {page.url}")
                await self._log_and_shot(page, "ARRIVE_SUCCESS")
                return True
            
            print(f"⚠️ 偏航或未登录: {page.url}")
            return False

        except Exception as e:
            print(f"❌ [ERROR] 导航失败: {str(e)}")
            return False

    async def action_fetch_ledger(self, page):
        """🚀 [杉果全量审计] 自动跨页抓取所有历史订单"""
        try:
            print("\n" + "📊 " * 15)
            print(f"{'订单号':<10} | {'下单时间':<18} | {'商品名称':<25} | {'状态':<10} | {'均摊成本'}")
            print("-" * 105)

            all_entries = []
            page_num = 1

            while True:
                print(f"📄 正在扫描杉果第 {page_num} 页...")
                # 等待订单块加载
                await page.wait_for_selector(".self-order-item", timeout=10000)
                order_blocks = await page.query_selector_all(".self-order-item")
                
                for block in order_blocks:
                    # 1. 提取订单号
                    id_el = await block.query_selector(".msg-box.order-id span")
                    oid = (await id_el.text_content()).strip() if id_el else "0"
                    
                    # 🚫 黑名单拦截
                    if oid in self.blacklist:
                        print(f"⏩ {oid:<10} | {'-'*18} | {'[已拦截-自用订单]':<25} | {'-'*10} | 🔒 排除")
                        continue

                    # 2. 基本元数据
                    time_el = await block.query_selector(".msg-box.time span")
                    otime = (await time_el.text_content()).strip() if time_el else "Unknown"
                    
                    price_box = await block.query_selector(".msg-small-box:not(.handle-box)")
                    price_text = await price_box.text_content() if price_box else "0"
                    total_paid = float(re.sub(r'[^\d.]', '', price_text))

                    # 3. 穿透子商品
                    sub_items = await block.query_selector_all(".img-hover-container")
                    count = len(sub_items) if sub_items else 1

                    for item in sub_items:
                        name_el = await item.query_selector("p.name")
                        tag_el = await item.query_selector(".tag")
                        if name_el:
                            gname = (await name_el.text_content()).strip()
                            gstatus = (await tag_el.text_content()).strip() if tag_el else "已完成"
                            avg_cost = round(total_paid / count, 2)
                            
                            status_ico = "✅" if "发货" in gstatus else "⚠️ "
                            print(f"{oid:<10} | {otime:<18} | {gname:<27} | {status_ico + gstatus:<10} | ¥{avg_cost}")

                            all_entries.append({
                                "order_id": oid, "order_time": otime, "name": gname,
                                "cost": avg_cost, "total_paid": total_paid, "status": gstatus,
                                "is_bundle": count > 1, "source": "Sonkwo", "sync_at": datetime.datetime.now().strftime("%H:%M:%S")
                            })

                # 4. 翻页逻辑：寻找下一页按钮
                next_btn = await page.query_selector(".ivu-page-next")
                if not next_btn:
                    print("🏁 未发现分页器，单页扫描结束。")
                    break
                
                # 检查是否已到最后一页
                btn_class = await next_btn.get_attribute("class")
                if "ivu-page-disabled" in btn_class:
                    print(f"🏁 已到达杉果末页 (共 {page_num} 页)")
                    break
                
                # 执行翻页
                await next_btn.click()
                page_num += 1
                await asyncio.sleep(3) # 等待 Vue 重新渲染列表
                await self._log_and_shot(page, f"SONKWO_PAGE_{page_num}")

            # 5. 落盘保存
            with open(self.ledger_file, "w", encoding="utf-8") as f:
                json.dump(all_entries, f, ensure_ascii=False, indent=4)
            
            print("-" * 105)
            print(f"📈 杉果全量同步完成：共存入 {len(all_entries)} 条原始记录")
            return all_entries

        except Exception as e:
            print(f"❌ 杉果抓取崩溃: {e}")
            return []


    async def enter_interactive_mode(self):
        """交互主循环"""
        print("\n" + "💰 " * 12)
        print("【财务审计分机】v2.0 完整版就绪")
        print("指令: [goto] 探测/瞬移 | [list] 抓取账本 | [ignore 订单号] 拉黑 | [shot] 强行快照 | [exit] 退出")
        print("💰 " * 12 + "\n")

        debug_page = await self.context.new_page()
        try:
            while True:
                sys.stdout.write(f"\r[财务分机] >> ")
                sys.stdout.flush()
                cmd_raw = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
                cmd = cmd_raw.strip().lower()

                if not cmd or cmd == "exit": break
                elif cmd == "goto": await self.action_verify_and_goto_orders(debug_page)
                elif cmd == "list": await self.action_fetch_ledger(debug_page)
                elif cmd == "shot":
                    # 💡 增加当前 URL 标识，方便区分是列表页还是详情页
                    page_type = "detail" if "orders/" in debug_page.url else "list"
                    await self._log_and_shot(debug_page, f"manual_{page_type}")
                    print(f"📸 {page_type} 页面快照与源码已同步。")
                elif cmd.startswith("ignore "):
                    oid = cmd.split(" ")[-1]
                    self.blacklist.add(oid)
                    self._save_blacklist()
                    print(f"🚫 订单 {oid} 已拉黑")
                elif cmd.startswith("detail "):
                    # 💡 用法: detail 16284976
                    order_id = cmd.replace("detail ", "").strip()
                    if order_id:
                        print(f"🕵️ 正在尝试进入订单详情页: {order_id}")
                        target_url = f"https://www.sonkwo.cn/setting/orders/{order_id}"
                        try:
                            await debug_page.goto(target_url, wait_until="networkidle", timeout=30000)
                            print(f"🎯 已到达详情页，请查看直播并输入 [shot] 落地 HTML")
                            await self._log_and_shot(debug_page, f"detail_{order_id}")
                        except Exception as e:
                            print(f"❌ 穿透失败: {e}")
            await debug_page.close()
        except Exception as e: print(f"🚨 交互崩溃: {e}")
        finally: print("🔙 已返回主巡航。")