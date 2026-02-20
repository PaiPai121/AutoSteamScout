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
            
            if is_logged_in:
                print("✅ 探测到登录态，执行瞬移...")
                await page.goto("https://www.sonkwo.cn/setting/orders", wait_until="networkidle", timeout=30000)
            else:
                print("🚨 未探测到头像，尝试点击登录刷新...")
                # 修复点：分步查找，避免 Unexpected token 报错
                login_btn = await page.get_by_text("登录").first
                if await login_btn.is_visible():
                    await login_btn.click()
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
        """[全息抓取] 穿透解析父子结构 + 黑名单拦截"""
        try:
            print("\n" + "📊 " * 20)
            print(f"{'订单号':<10} | {'下单时间':<18} | {'商品明细':<25} | {'状态':<10} | {'均摊成本'}")
            print("-" * 105)

            # 强行等待订单容器加载
            await page.wait_for_selector(".self-order-item", timeout=10000)
            order_blocks = await page.query_selector_all(".self-order-item")
            
            all_entries = []

            for block in order_blocks:
                # 1. 基本信息
                id_el = await block.query_selector(".msg-box.order-id span")
                time_el = await block.query_selector(".msg-box.time span")
                oid = (await id_el.text_content()).strip() if id_el else "0"
                otime = (await time_el.text_content()).strip() if time_el else "Unknown"

                # 2. 拦截黑名单
                if oid in self.blacklist:
                    print(f"⏩ {oid:<10} | {otime:<18} | {'[自用订单-已拦截]':<25} | {'-'*10} | 🔒 排除")
                    continue

                # 3. 价格穿透 (锁定实付总额)
                price_box = await block.query_selector(".msg-small-box:not(.handle-box)")
                price_text = await price_box.text_content() if price_box else "0"
                total_paid = float(re.sub(r'[^\d.]', '', price_text))

                # 4. 子游戏明细
                sub_items = await block.query_selector_all(".img-hover-container")
                count = len(sub_items) if sub_items else 1

                for item in sub_items:
                    name_el = await item.query_selector("p.name")
                    tag_el = await item.query_selector(".tag")
                    
                    if name_el:
                        gname = (await name_el.text_content()).strip()
                        gstatus = (await tag_el.text_content()).strip() if tag_el else "已完成"
                        avg_cost = round(total_paid / count, 2)

                        # 终端打印
                        status_ico = "✅" if "发货" in gstatus else "⚠️ "
                        bundle_ico = " [合]" if count > 1 else ""
                        print(f"{oid:<10} | {otime:<18} | {gname + bundle_ico:<27} | {status_ico + gstatus:<10} | ¥{avg_cost}")

                        all_entries.append({
                            "order_id": oid, "order_time": otime, "name": gname,
                            "cost": avg_cost, "total_paid": total_paid, "status": gstatus,
                            "is_bundle": count > 1, "sync_at": datetime.datetime.now().strftime("%H:%M:%S")
                        })

            print("-" * 105)
            with open(self.ledger_file, "w", encoding="utf-8") as f:
                json.dump(all_entries, f, ensure_ascii=False, indent=4)
            print(f"📈 账本已更新: {len(all_entries)} 条记录入库")
            return all_entries

        except Exception as e:
            print(f"❌ [FETCH-ERROR] 抓取失败: {str(e)}")
            await self._log_and_shot(page, "FETCH_ERROR")
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
                elif cmd == "shot": await self._log_and_shot(debug_page, "MANUAL")
                elif cmd.startswith("ignore "):
                    oid = cmd.split(" ")[-1]
                    self.blacklist.add(oid)
                    self._save_blacklist()
                    print(f"🚫 订单 {oid} 已拉黑")
            await debug_page.close()
        except Exception as e: print(f"🚨 交互崩溃: {e}")
        finally: print("🔙 已返回主巡航。")