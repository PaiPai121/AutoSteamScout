import json
import os
import re
import sys
import asyncio
import datetime
from playwright.async_api import Page

class FinanceService:
    def __init__(self, context, ledger_file="data/purchase_ledger.json"):
        self.context = context
        self.ledger_file = ledger_file
        self.blacklist_file = "data/audit_blacklist.json"
        self.blacklist = self._load_blacklist()
        
        # 🛡️ 反反爬虫配置
        self.SCAN_DELAY = 2.0  # 基础扫描间隔（秒）
        self.RANDOM_JITTER = 1.0  # 随机抖动范围（秒）
        self.PAGE_DELAY = 5.0  # 翻页后等待时间（秒）
        self.MAX_CONCURRENT_REQUESTS = 1  # 最大并发请求数（衫果单页限制）

    def _load_blacklist(self):
        if os.path.exists(self.blacklist_file):
            try:
                with open(self.blacklist_file, 'r', encoding='utf-8') as f:
                    return set(json.load(f))
            except:
                return set()
        return set()

    def _save_blacklist(self):
        with open(self.blacklist_file, 'w', encoding='utf-8') as f:
            json.dump(list(self.blacklist), f, ensure_ascii=False, indent=2)

    async def action_verify_and_goto_orders(self, page):
        """[深度审计] 导航至杉果订单列表页"""
        try:
            print("🔍 正在验证并导航至订单列表页...")
            await page.goto("https://www.sonkwo.hk/setting/orders", wait_until="networkidle", timeout=30000)
            print("✅ 已到达订单列表页")
            return True
        except Exception as e:
            print(f"❌ [ERROR] 导航失败：{str(e)}")
            return False
    
    def _clean_price(self, price_str):
        """将带有货币符号的字符串转为浮点数"""
        if not price_str: return 0.0
        try:
            # 正则匹配数字和小数点
            cleaned = re.sub(r'[^\d.]', '', str(price_str))
            return float(cleaned) if cleaned else 0.0
        except:
            return 0.0

    async def _extract_unit_keys(self, page):
        """核心：从详情页激活码弹窗中收割所有 Key"""
        keys = []
        try:
            # ⏳ 1. 等待弹窗和 Key 代码容器挂载
            await page.wait_for_selector(".key-code", timeout=5000)
            
            # 🚀 2. 抓取该弹窗下所有的激活码 (考虑到可能存在多个 Key)
            key_elements = await page.query_selector_all(".key-code")
            for el in key_elements:
                k_text = await el.text_content()
                if k_text:
                    keys.append(k_text.strip())
            
            # ❌ 3. 必须关掉弹窗，否则会遮挡下一个操作
            close_btn = await page.query_selector(".SKC-modal-close")
            if close_btn:
                await close_btn.click()
                await asyncio.sleep(1) # 给弹窗动画消失一点时间
                
        except Exception as e:
            print(f"      ⚠️ Key 提取超时或失败：{str(e)}")
            keys = []  # 返回空列表，上层会标记为 KEY_UNAVAILABLE
            
        return keys

    async def _goto_next_page(self, page):
        """判定并执行翻页"""
        try:
            next_btn = await page.query_selector(".ivu-page-next")
            if not next_btn: return False
            
            # 检查按钮是否带有 'disabled' 类
            is_disabled = await page.evaluate(
                '(el) => el.classList.contains("ivu-page-disabled")', 
                next_btn
            )
            
            if is_disabled:
                return False
                
            await next_btn.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2) # 衫果列表页加载较重，留出缓冲
            return True
        except Exception as e:
            print(f"⚠️ 翻页失败：{str(e)}")
            return False

    async def _process_order_detail(self, page, oid):
        """🚀 深度穿透函数（优化价格匹配与容错）"""
        results = []
        try:
            # 1. 提取总支付金额基准 (建议 3 修复)
            total_paid_val = 0.0
            try:
                total_paid_el = page.locator(".total-price").first
                total_paid_val = self._clean_price(await total_paid_el.text_content())
            except:
                print(f"      ⚠️ 订单 {oid} 无法提取总实付金额，平账校验将仅供参考")

            # 2. 提取下单时间（确保数据完备性，建议 4 检查点）
            time_el = page.locator(".row-msg:has-text('下单时间') .msg-desc")
            otime = (await time_el.text_content()).strip() if await time_el.count() > 0 else "Unknown"

            # 3. 定位明细块
            item_blocks = await page.query_selector_all(".new-order-details")
            sum_of_sub_prices = 0.0

            for idx, block in enumerate(item_blocks):
                # 语义化查找价格行 (建议 2 修复)
                price_val = 0.0
                rows = await block.query_selector_all(".row")
                for row in rows:
                    text = await row.text_content()
                    if "¥" in text or "￥" in text:
                        price_val = self._clean_price(text)
                        break
                
                name_el = await block.query_selector(".sku-name")
                gname = (await name_el.text_content()).strip() if name_el else "Unknown Game"
                sum_of_sub_prices += price_val

                # 解锁激活码 (建议 5 修复)
                cd_keys = []
                status_box = await block.query_selector(".row-dark")
                status_text = await status_box.text_content() if status_box else ""
                unlock_trigger = await block.query_selector(".view-activation-code")
                
                if "退款" in status_text:
                    cd_keys = ["REFUNDED"]
                elif unlock_trigger:
                    await unlock_trigger.click()
                    cd_keys = await self._extract_unit_keys(page)
                    # 如果返回空列表，标记为提取失败
                    if not cd_keys:
                        cd_keys = ["KEY_EXTRACTION_FAILED"]
                
                results.append({
                    "uid": f"SK_{oid}_{idx}",
                    "order_id": oid,
                    "order_time": otime,  # 确保时间字段不丢失
                    "name": gname,
                    "cost": price_val,
                    "cd_key": cd_keys[0] if cd_keys else "KEY_UNAVAILABLE",  # 统一标记
                    "all_keys": cd_keys,
                    "sync_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                
                # 3. 封装数据
                entry_data = {
                    "uid": f"SK_{oid}_{idx}",
                    "order_id": oid,
                    "order_time": otime,
                    "name": gname,
                    "cost": price_val,
                    "cd_key": cd_keys[0] if cd_keys else "KEY_UNAVAILABLE",
                    "all_keys": cd_keys,
                    "sync_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                results.append(entry_data)

                # 🚀 增加战果实时反馈
                # 这里的格式：[子单入库] 游戏名 | 价格 | Key的前5位
                mask_key = f"{entry_data['cd_key'][:5]}***" if len(entry_data['cd_key']) > 5 else entry_data['cd_key']
                print(f"      ✅ [子单入库] {gname[:15]} | ¥{price_val} | Key: {mask_key}")

            # 财务平账校验 (亮点 2)
            if total_paid_val > 0 and abs(sum_of_sub_prices - total_paid_val) > 0.1:
                print(f"    🚨 [对账异常] 订单 {oid}: 详情汇总 ¥{sum_of_sub_prices} != 总额 ¥{total_paid_val}")

        except Exception as e:
            print(f"    🚨 详情页解析中断 (OID: {oid}): {e}")
        return results
        
    async def action_fetch_ledger(self, page):
        """🚀 生产级：全量增量审计任务总控 (修复死循环漏洞 + 反反爬虫)"""
        try:
            # 1. 加载旧账本与 UID 索引
            old_ledger = []
            if os.path.exists(self.ledger_file):
                try:
                    with open(self.ledger_file, 'r', encoding='utf-8') as f:
                        old_ledger = json.load(f)
                except: pass
            
            # 已同步过且带 KEY 的 UID 集合
            synced_uids = {e['uid'] for e in old_ledger if e.get('cd_key') and len(e['cd_key']) > 5}
            all_entries = {e['uid']: e for e in old_ledger}
            
            if "setting/orders" not in page.url:
                await page.goto("https://www.sonkwo.hk/setting/orders", wait_until="networkidle", timeout=30000)

            page_num = 1
            total_scanned = 0  # 📊 统计：本轮扫描了多少订单
            total_skipped = 0  # 📊 统计：本轮跳过了多少订单
            
            while True:
                print(f"\n📄 [母舰巡航] 第 {page_num} 页扫描中...")
                await page.wait_for_selector(".self-order-item", timeout=50000)
                
                # 🚀 关键：获取当前页面所有订单的数量
                order_count = await page.locator(".self-order-item").count()
                page_scanned = 0
                page_skipped = 0

                # 💡 方案 B：使用索引号遍历，不保存 block 句柄，防止 goto 导致失效
                for i in range(order_count):
                    # 即时定位当前索引的订单块
                    current_block = page.locator(".self-order-item").nth(i)
                    
                    # 提取 OID 进行判定
                    oid_el = current_block.locator(".msg-box.order-id span")
                    oid = (await oid_el.text_content()).strip()
                    
                    if oid in self.blacklist: continue

                    # 探测该订单包含多少子商品 (列表页小图数量)
                    # 🚨 修复点 1：衫果列表页单品可能没有 .img-hover-container 类
                    sub_count = await current_block.locator(".img-hover-container").count()
                    if sub_count == 0:
                        # 尝试用其他方式探测商品数量
                        sub_count = await current_block.locator(".sku-name").count()
                    if sub_count == 0:
                        # 最后 fallback：假设为 1（防止跳过）
                        sub_count = 1
                    
                    # 判定：这一单是否所有子项都已同步过 KEY？
                    needs_audit = any(f"SK_{oid}_{j}" not in synced_uids for j in range(sub_count))

                    if needs_audit:
                        page_scanned += 1
                        print(f"  [{i+1}/{order_count}] 🔍 发现未审计订单 {oid}，进入详情...")
                        
                        # 点击 [查看详情] 按钮
                        detail_btn = current_block.locator(".see-detail")
                        await detail_btn.click()
                        await page.wait_for_load_state("networkidle")
                        await asyncio.sleep(2)

                        # 🚀 执行：详情页全息剥皮
                        new_items = await self._process_order_detail(page, oid)
                        for item in new_items:
                            all_entries[item['uid']] = item
                            # 抓完立刻加入已同步集合，防止本轮循环重复扫描
                            if len(item['cd_key']) > 5:
                                synced_uids.add(item['uid'])
                        
                        # 🔙 稳定撤离：重新回到列表页（🚨 修复点 2：带上当前的 page_num，防止迷航）
                        target_list_url = f"https://www.sonkwo.hk/setting/orders?page={page_num}"
                        print(f"      🔙 返回第 {page_num} 页：{target_list_url}")
                        await page.goto(target_list_url, wait_until="networkidle")
                        # 重新等待元素加载，确保下一轮 i 的定位准确
                        await page.wait_for_selector(".self-order-item", timeout=50000)
                        
                        # 🛡️ 反反爬虫：随机延迟（模拟人类行为）
                        import random
                        delay = self.SCAN_DELAY + random.uniform(0, self.RANDOM_JITTER)
                        print(f"      😴 扫描间隔：{delay:.1f}秒 (防封策略)")
                        await asyncio.sleep(delay)
                    else:
                        page_skipped += 1
                        # print(f"  [{i+1}/{order_count}] ⏩ 订单 {oid} 已同步，跳过")

                total_scanned += page_scanned
                total_skipped += page_skipped
                print(f"  ✅ 本页完成：扫描 {page_scanned} 个，跳过 {page_skipped} 个")

                # 🧭 翻页逻辑：本页 i 循环结束后再翻页
                if not await self._goto_next_page(page):
                    break
                    
                # 🛡️ 反反爬虫：翻页后额外等待
                print(f"  📑 翻页后等待 {self.PAGE_DELAY} 秒...")
                await asyncio.sleep(self.PAGE_DELAY)
                
                page_num += 1

            # 💾 结算归档
            final_data = list(all_entries.values())
            final_data.sort(key=lambda x: x.get('order_time', ''), reverse=True)
            
            with open(self.ledger_file, "w", encoding="utf-8") as f:
                json.dump(final_data, f, ensure_ascii=False, indent=4)
            
            print(f"\n📈 审计圆满结案！")
            print(f"   ├── 扫描统计：{total_scanned} 个订单被审计")
            print(f"   ├── 跳过统计：{total_skipped} 个订单已同步")
            print(f"   └── 账本库存：{len(final_data)} 条记录")
            return final_data

        except Exception as e:
            print(f"🚨 审计任务中断：{e}")
            return []

    async def enter_interactive_mode(self):
        """交互主循环"""
        print("\n" + "💰 " * 12)
        print("【财务审计分机】v2.0 完整版就绪")
        print("指令：[goto] 探测/瞬移 | [list] 抓取账本 | [ignore 订单号] 拉黑 | [shot] 强行快照 | [exit] 退出")
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
                        for e in entries[-5:]:
                            print(f"  ID: {e['order_id']} | UID: {e['uid']} | {e['name'][:15]}... | ¥{e['cost']}")
                        print("-" * 65)
                elif cmd.startswith("click "):
                    oid = cmd.replace("click ", "").strip()
                    print(f"🖱️ 尝试通过【查看详情】按钮进入订单：{oid}")
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
                        print(f"🚨 点击动作崩溃：{e}")
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
                    # 💡 用法：detail 16284976
                    order_id = cmd.replace("detail ", "").strip()
                    if order_id:
                        print(f"🕵️ 正在尝试进入订单详情页：{order_id}")
                        target_url = f"https://www.sonkwo.cn/setting/orders/{order_id}"
                        try:
                            await debug_page.goto(target_url, wait_until="networkidle", timeout=30000)
                            print(f"🎯 已到达详情页，请查看直播并输入 [shot] 落地 HTML")
                            await self._log_and_shot(debug_page, f"detail_{order_id}")
                        except Exception as e:
                            print(f"❌ 穿透失败：{e}")
                # --- 核心：通用点击 (Tap) ---
                elif cmd.lower().startswith("tap "):
                    target = cmd[4:].strip()
                    print(f"🎯 尝试通用点击：{target}")
                    try:
                        # 逻辑：先尝试作为 CSS 选择器，如果找不到，尝试作为文本
                        selector = debug_page.locator(target).first
                        if await selector.count() == 0:
                            # 尝试文本匹配
                            selector = debug_page.get_by_text(target).first

                        await selector.click()
                        print(f"✅ 点击完成 (Target: {target})")
                    except Exception as e:
                        print(f"❌ 点击失败：{str(e)}")
            await debug_page.close()
        except Exception as e: print(f"🚨 交互崩溃：{e}")
        finally: print("🔙 已返回主巡航。")

    async def _log_and_shot(self, page, label="snapshot"):
        """[内部] 保存当前页 HTML 源码 + 截图"""
        try:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            html = await page.content()
            shot_path = f"data/debug_{label}_{ts}.html"
            with open(shot_path, 'w', encoding='utf-8') as f:
                f.write(html)
            print(f"💾 HTML 源码已保存：{shot_path}")
        except Exception as e:
            print(f"❌ 存档失败：{e}")
