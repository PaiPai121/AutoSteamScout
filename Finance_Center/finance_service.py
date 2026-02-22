import asyncio
import sys
import os
import re
import json
import datetime
import config
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
        if not getattr(config, "ENABLE_SCREENSHOTS", False):
            return
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
        """[全息抓取] 注入 UID 机制，解决一单多购与重复购买对账问题"""
        try:
            if "setting/orders" not in page.url:
                await page.goto("https://www.sonkwo.hk/setting/orders", wait_until="networkidle", timeout=30000)

            all_entries = []
            page_num = 1

            while True:
                print(f"\n📄 [第 {page_num} 页] 扫描中...")
                
                try:
                    await page.wait_for_selector(".self-order-item", timeout=10000)
                except: break

                order_blocks = await page.query_selector_all(".self-order-item")
                for block in order_blocks:
                    # 1. 提取基础订单信息
                    id_el = await block.query_selector(".msg-box.order-id span")
                    time_el = await block.query_selector(".msg-box.time span")
                    oid = (await id_el.text_content()).strip() if id_el else "0"
                    otime = (await time_el.text_content()).strip() if time_el else "Unknown"

                    if oid in self.blacklist:
                        continue

                    # 2. 提取订单总额
                    price_box = await block.query_selector(".msg-small-box:not(.handle-box)")
                    price_text = await price_box.text_content() if price_box else "0"
                    total_paid = float(re.sub(r'[^\d.]', '', price_text))

                    # 3. 核心穿透：处理该订单下的所有子商品
                    sub_items = await block.query_selector_all(".img-hover-container")
                    count = len(sub_items) if sub_items else 1
                    avg_cost = round(total_paid / count, 2)

                    # 🚀 引入枚举序号，生成唯一 UID
                    for idx, item in enumerate(sub_items):
                        name_el = await item.query_selector("p.name")
                        tag_el = await item.query_selector(".tag")
                        
                        if name_el:
                            gname = (await name_el.text_content()).strip()
                            gstatus = (await tag_el.text_content()).strip() if tag_el else "已完成"
                            
                            # 🎯 生成唯一标识符：SK_订单号_序号
                            # 即使 order_id 相同，idx 也能区分出同一单里的不同商品
                            unique_id = f"SK_{oid}_{idx}"

                            all_entries.append({
                                "uid": unique_id,        # 👈 新增：财务对账的唯一索引
                                "order_id": oid,
                                "order_time": otime,
                                "name": gname,
                                "cost": avg_cost,
                                "total_paid": total_paid,
                                "status": gstatus,
                                "is_bundle": count > 1,
                                "sync_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            })

                # --- 翻页判定逻辑 ---
                next_btn = await page.query_selector(".ivu-page-next")
                if not next_btn or await page.evaluate('(el) => el.classList.contains("ivu-page-disabled")', next_btn):
                    break

                await next_btn.click()
                page_num += 1
                await asyncio.sleep(3) 

            # 保存结果
            with open(self.ledger_file, "w", encoding="utf-8") as f:
                json.dump(all_entries, f, ensure_ascii=False, indent=4)
            
            print(f"📈 审计完成！全量 UID 记录: {len(all_entries)} 条")
            return all_entries

        except Exception as e:
            print(f"❌ [FETCH-ERROR] {str(e)}")
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
                elif cmd == "back":
                    await debug_page.go_back()
                    print("⬅️ 正在退回上一页...")
                elif cmd == "list": 
                    entries = await self.action_fetch_ledger(debug_page)
                    if entries:
                        print("\n📋 [抓取快照预览]")
                        print("-" * 65)
                        # 仅显示最近的 5 条，避免刷屏
                        for e in entries:
                            print(f"  ID: {e['order_id']} | UID: {e['uid']} | {e['name'][:15]}... | ¥{e['cost']}")
                        print("-" * 65)
                elif cmd.startswith("click "):
                    oid = cmd.replace("click ", "").strip()
                    print(f"🖱️ 尝试通过【查看详情】按钮进入订单: {oid}")
                    try:
                        # 1. 先定位包含该订单号的那个大方块 (.self-order-item)
                        # 2. 在方块内部寻找 .see-detail 按钮并点击
                        order_item = debug_page.locator(f".self-order-item:has-text('{oid}')")
                        detail_btn = order_item.locator(".see-detail")
                        
                        if await detail_btn.count() > 0:
                            await detail_btn.click()
                            await debug_page.wait_for_load_state("networkidle")
                            print(f"🎯 成功进入详情页。请检查状态，若正常请执行 [shot]")
                        else:
                            print(f"❌ 列表页当前页没找到订单 {oid} 的详情按钮")
                    except Exception as e:
                        print(f"🚨 点击动作崩溃: {e}")
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
                # --- 核心：通用点击 (Tap) ---
                elif cmd.lower().startswith("tap "):
                    target = cmd[4:].strip()
                    print(f"🎯 尝试通用点击: {target}")
                    try:
                        # 逻辑：先尝试作为 CSS 选择器，如果找不到，尝试作为文本
                        selector = debug_page.locator(target).first
                        if await selector.count() == 0:
                            # 尝试文本匹配
                            selector = debug_page.get_by_text(target).first
                        
                        await selector.click()
                        print(f"✅ 点击完成 (Target: {target})")
                    except Exception as e:
                        print(f"❌ 点击失败: {str(e)}")
            await debug_page.close()
        except Exception as e: print(f"🚨 交互崩溃: {e}")
        finally: print("🔙 已返回主巡航。")