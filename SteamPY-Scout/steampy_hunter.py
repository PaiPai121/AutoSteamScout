import asyncio
import re
import datetime
from steampy_scout_core import SteamPyScout
from tabulate import tabulate
import sys

class SteamPyMonitor(SteamPyScout):
    async def get_current_state(self):
        # --- 原有的页面判断逻辑 ---
        url = self.page.url
        is_detail = await self.page.query_selector("span:has-text('返回')")
        has_table = await self.page.query_selector(".ivu-table-row")
        
        page_type = "UNKNOWN"
        if is_detail and has_table: page_type = "DETAIL"
        elif await self.page.query_selector(".searchCDK"): page_type = "LIST"
        elif "home" in url: page_type = "HOME"

        # --- 新增：侧边栏组件状态探测 ---
        # 1. 探测“CDKey市场”一级菜单是否展开
        # 逻辑：查找包含“CDKey市场”的 submenu，看它是否有 'opened' 类
        menu_submenu = await self.page.query_selector("li.ivu-menu-submenu:has-text('CDKey市场')")
        menu_opened = False
        if menu_submenu:
            cls = await menu_submenu.get_attribute("class")
            if "ivu-menu-opened" in cls:
                menu_opened = True

        # 2. 探测“国区”是否被选中
        # 逻辑：查找包含“国区”的菜单项，看它是否有 'selected' 类
        china_item = await self.page.query_selector("li.ivu-menu-item:has-text('国区')")
        china_selected = False
        if china_item:
            cls = await china_item.get_attribute("class")
            if "ivu-menu-item-selected" in cls:
                china_selected = True

        # 组合状态报告
        menu_status = "【展开】" if menu_opened else "【折叠】"
        selection_status = " -> (已选中国区)" if china_selected else ""
        
        return f"页面:{page_type} | 菜单:{menu_status}{selection_status}"
    async def action_goto(self):
        print("\n[COMMAND] 启动全自适应导航流程...")
        
        try:
            # 1. 目的地检查
            state = await self.get_current_state()
            if "页面:LIST" in state and "已选中国区" in state:
                print("✅ 已在目的地，无需操作。")
                return

            # 2. 判断是否有“地标”（一级菜单）
            menu_header_selector = "li.ivu-menu-submenu:has-text('CDKey市场')"
            menu_exists = await self.page.query_selector(menu_header_selector)
            
            # 只有当连菜单都搜不到了，才认为是彻底迷路，需要重置 URL
            if not menu_exists:
                print("🚨 核心菜单组件丢失，正在强制回航首页...")
                await self.page.goto("https://steampy.com/home", timeout=15000)
                await asyncio.sleep(1.5)
                # 回航后重新获取状态
                state = await self.get_current_state()
            else:
                print("📡 虽处于未知页面或状态，但地标菜单尚在，尝试执行操作...")

            # 3. 展开菜单
            # 只要不是明确的【展开】，或者我们要确保它开了，就执行点击
            if "【折叠】" in state or "UNKNOWN" in state:
                print("🖱️ 步骤 1: 尝试展开一级菜单...")
                try:
                    # 增加 visible 检查，确保真的能点
                    menu_header = await self.page.wait_for_selector(menu_header_selector, state="visible", timeout=3000)
                    await menu_header.click()
                    await asyncio.sleep(0.8) # 动画缓冲
                except:
                    print("⚠️ 一级菜单点击未响应，可能已是展开状态。")
            
            # 4. 点击二级菜单
            print("⏳ 步骤 2: 定位二级菜单...")
            china_btn_selector = "li.ivu-menu-item:has-text('CDKey市场-国区')"
            try:
                china_btn = await self.page.wait_for_selector(china_btn_selector, state="visible", timeout=5000)
                print("🖱️ 步骤 3: 发现二级菜单，执行点击...")
                await china_btn.click()
            except Exception:
                # 最后的兜底：如果 wait_for 没等到，尝试暴力文本点击
                print("⚠️ 未发现可见二级菜单，尝试文本暴力点击...")
                await self.page.click("text=CDKey市场-国区")

            # 5. 落地验证
            print("⏳ 步骤 4: 最终定位确认...")
            await self.page.wait_for_selector(".ivu-input", state="visible", timeout=8000)
            print("🎯 导航成功。")

        except Exception as e:
            print(f"🚨 导航异常终止: {e}")

    async def action_search(self, name):
        """
        [重构版] 简单直接的搜索逻辑：废除变体干扰，信任主控审计
        """
        import re
        
        # 1. 确保在列表页
        await self.action_goto()
        
        # 2. 准备最核心的搜索词：原名 + 简单的标点纠正
        # 💡 不再搞多种变体循环，只搜最稳的那个
        clean_variant = re.sub(r'[：:，,。\.·・\-]', ' ', name).strip()
        print(f"📡 [SteamPy] 正在执行硬核搜索: [{clean_variant}]")
        
        try:
            # 定位并填入搜索框
            search_input = await self.page.wait_for_selector(".ivu-input", timeout=5000)
            await search_input.fill("") 
            await search_input.fill(clean_variant)
            await self.page.keyboard.press("Enter")
            
            # 给 Vue 渲染留出缓冲 (保持原有的 2.5s 确保加载)
            await asyncio.sleep(2.5) 
            
            cards = await self.page.query_selector_all(".gameblock")
            if not cards:
                print(f"❌ SteamPy 搜索结果为空: {clean_variant}")
                return False

            # 3. 简单的初筛逻辑 (不再使用复杂的评分)
            best_match = None
            for card in cards:
                name_el = await card.query_selector(".gameName")
                if name_el:
                    actual_name = (await name_el.text_content()).strip()
                    
                    # 💡 只要包含了核心词（比如“耻辱2”在结果里），就直接冲！
                    # 后续版本对不对，交给 Commander 里的 AI 审计去头疼
                    if clean_variant.lower() in actual_name.lower() or actual_name.lower() in clean_variant.lower():
                        print(f"✅ 找到语义匹配目标: {actual_name}")
                        best_match = card
                        break # 抓到第一个匹配的就走，效率最高

            if not best_match:
                print(f"⚠️ 列表页无语义关联目标，放弃跳转。")
                return False

            # 4. 执行跳转
            await best_match.click()
            # 增加对详情页特有元素的等待，确保跳转成功
            await self.page.wait_for_selector(".game-title, span:has-text('返回')", timeout=10000)
            return True

        except Exception as e:
            print(f"🚨 SteamPy 搜索/跳转异常: {e}")
            return False

    async def action_scan(self):
        print("\n[COMMAND] 正在执行深度扫描（含平台比价）...")
        
        # 1. 确保在详情页
        state = await self.get_current_state()
        if "DETAIL" not in state:
            print("⚠️ 探测到不在详情页，请先搜索进入游戏。")
            return

        try:
            # --- A. 提取页面顶部概览信息 ---
            # 获取完整游戏名
            full_name = await (await self.page.query_selector(".gameName")).text_content()
            
            # 获取平台最低价 (那个大红字数字)
            price_box = await self.page.query_selector(".f50-rem")
            platform_low_price = await price_box.text_content() if price_box else "未知"
            
            # 获取折扣比例
            discount_box = await self.page.query_selector(".game_discount")
            discount = await discount_box.text_content() if discount_box else "无"

            print(f"📦 目标：{full_name.strip()}")
            print(f"💰 平台参考最低价：¥{platform_low_price.strip()} (折扣: {discount.strip()})")
            print("-" * 40)

            # --- B. 提取卖家表格数据 ---
            rows = await self.page.query_selector_all(".ivu-table-tbody tr.ivu-table-row")
            
            full_data = []
            low_price_val = float(platform_low_price.strip()) if platform_low_price.replace('.','').isdigit() else 0

            for row in rows[:10]:
                cells = await row.query_selector_all("td")
                if len(cells) >= 5:
                    seller = (await cells[2].text_content()).strip()
                    stock = (await cells[3].text_content()).strip()
                    price_str = (await cells[4].text_content()).strip().replace("￥", "")
                    
                    # 自动比价逻辑：如果卖家价格低于平台最低价，标记为“捡漏”
                    current_price = float(price_str) if price_str.replace('.','').isdigit() else 9999
                    tag = "🔥 捡漏" if current_price < low_price_val else ""
                    
                    full_data.append([seller, stock, f"¥{price_str}", tag])

            if full_data:
                print(tabulate(full_data, headers=["卖家名", "库存", "卖家报价", "建议"], tablefmt="grid"))
            else:
                print("⚠️ 表格内暂无有效报价。")

        except Exception as e:
            print(f"🚨 扫描异常: {e}")

    # --- 后台雷达任务 ---
    async def radar_task(self):
        while True:
            state = await self.get_current_state()
            now = datetime.datetime.now().strftime("%H:%M:%S")
            # 使用 \r 和 sys.stdout 保持在同一行滚动，不干扰输入
            sys.stdout.write(f"\r[{now}] 🛰️ 雷达实时位置: {state} | 请输入指令 >> ")
            sys.stdout.flush()
            await asyncio.sleep(1)

    async def run_commander(self):
        self.page = await self.start()
        if not self.page: return
        asyncio.create_task(self.radar_task())

        print("\n" + "🍬 " * 15)
        print("语法糖已添加！")
        print("新增指令: [scan 游戏名] -> 自动搜索并打印报价单")
        print("🍬 " * 15 + "\n")

        while True:
            try:
                cmd_raw = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
                line = cmd_raw.strip()
                if not line: continue
                
                parts = line.split(maxsplit=1)
                cmd = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else None

                if cmd == "exit":
                    break
                    
                elif cmd == "goto":
                    await self.action_goto()

                elif cmd == "search":
                    if arg: await self.action_search(arg)
                    else: print("⚠️ 请输入游戏名，例如: search 艾尔登")

                elif cmd == "scan":
                    # --- 语法糖核心逻辑 ---
                    if arg:
                        # 情况 A: scan 游戏名 (先搜再扫)
                        print(f"🍭 快捷指令：搜索并扫描 [{arg}]...")
                        success = await self.action_search(arg)
                        if success:
                            await asyncio.sleep(1) # 给页面一点点加载缓冲
                            await self.action_scan()
                    else:
                        # 情况 B: 单独输入 scan (扫描当前页)
                        await self.action_scan()
                elif cmd == "post":
                    print(f"DEBUG: 收到 post 指令, 参数为: {arg}") # 新增调试行
                    if arg and "|" in arg:
                        try:
                            game_name, key_code, price = arg.split("|")
                            print(f"🚀 [控制台] 正在下达指令：目标={game_name}, 价格={price}")
                            
                            # 执行动作
                            nav_res = await self.action_goto_seller_post()
                            if nav_res:
                                await self.action_fill_post_form(game_name, key_code, price)
                                print("✅ 填报脚本执行完毕")
                            else:
                                print("❌ 无法抵达发布页")
                        except Exception as e:
                            print(f"🚨 解析参数或执行失败: {e}")
                    else:
                        print("⚠️ 格式不符。正确用法: post 游戏名|Key|价格")
                elif cmd == "list":
                    print("📋 [控制台] 正在下达指令：扫描当前库存...")
                    # 直接调用刚才写好的扫描函数
                    await self.action_scan_inventory()
                print("\n" + "-"*40)
            except KeyboardInterrupt:
                break
        await self.stop()

    async def get_game_market_price_with_name(self, name):
        """
        相比之前的版本，这个函数会返回 (价格, 实际搜到的商品名)
        """
        try:
            success = await self.action_search(name)
            if not success:
                return None

            await asyncio.sleep(1.5)
            
            # 获取名字
            name_el = await self.page.query_selector(".gameName")
            actual_name = (await name_el.text_content()).strip() if name_el else "未知"

            # 获取价格
            price_box = await self.page.query_selector(".f50-rem")
            if price_box:
                price_str = await price_box.text_content()
                price_match = re.search(r"\d+\.?\d*", price_str)
                if price_match:
                    return float(price_match.group()), actual_name
            
            return None
        except:
            return None
    async def action_goto_seller_post(self):
        """
        导航至卖家中心-CDK看板（查账、看库存的终点）
        """
        print("🖱️ [动作] 正在从折叠页查找卖家中心入口...")
        await self.action_goto()
        
        try:
            seller_menu = await self.page.wait_for_selector(
                "li.ivu-menu-submenu:has(span:has-text('卖家中心'))", timeout=5000
            )
            is_opened = await seller_menu.evaluate("node => node.classList.contains('ivu-menu-opened')")
            if not is_opened:
                await (await seller_menu.query_selector(".ivu-menu-submenu-title")).click()
                await asyncio.sleep(0.5)

            cdk_item = await self.page.wait_for_selector("li.ivu-menu-item:has(span:has-text('卖家中心-CDK'))")
            await cdk_item.click()
            
            # 只要看到这个按钮，就说明导航到了看板页
            await self.page.wait_for_selector("button:has-text('添加CDKey')", timeout=10000)
            print("✅ [成功] 已抵达【卖家中心-CDK】看板。")
            return True
        except Exception as e:
            print(f"🚨 [导航异常]: {e}")
            return False
    async def action_scan_inventory(self):
        """
        [库存扫描] 解析看板表格，获取当前所有挂单的实时状态
        """
        print("🕵️ [动作] 正在启动库存扫描仪...")
        
        # 1. 确保在卖家中心-CDK 看板页
        # 如果当前 URL 不对，自动调用导航函数
        if "sell/cdkTrade" not in self.page.url:
            success = await self.action_goto_seller_post()
            if not success:
                print("❌ [扫描] 无法抵达看板页，放弃扫描。")
                return []

        try:
            # 2. 等待挂单列表加载
            # orderOne 是表格容器，flex-row 是行
            print("⏳ 正在读取挂单列表...")
            await self.page.wait_for_selector(".orderOne.bg-white", timeout=5000)
            
            # 获取所有非表头的行 (排除带有 bg-black 的标题行)
            rows = await self.page.query_selector_all(".orderOne.bg-white .flex-row:not(.bg-black)")
            
            if not rows:
                print("📭 [结果] 当前挂单列表为空。")
                return []

            inventory_data = []
            
            # 3. 遍历每一行提取数据
            for row in rows:
                # 在每一行内寻找对应的列块
                # 根据 HTML 结构：w25 是游戏名，w10 是库存/价格
                cells = await row.query_selector_all("div")
                
                # 预警：如果行结构不符合预期则跳过
                if len(cells) < 8: continue
                
                # 提取并清理文本
                # 索引位置根据 HTML 标签顺序：
                # 0:时间, 1:库存, 2:图片, 3:游戏名, 4:Steam链接, 5:最新成交价, 6:出售金额, 8:状态
                game_name = (await cells[3].text_content()).strip()
                stock_num = (await cells[1].text_content()).strip()
                sell_price = (await cells[6].text_content()).strip().replace("¥", "").strip()
                current_status = (await cells[8].text_content()).strip()
                
                # 只有真实的游戏名才记录
                if game_name and game_name != "暂无数据":
                    inventory_data.append({
                        "name": game_name,
                        "stock": stock_num,
                        "price": sell_price,
                        "status": current_status
                    })

            # 4. 终端可视化输出
            if inventory_data:
                print("\n" + "📦 " * 3 + "当前卖家库存大盘" + " 📦" * 3)
                print(f"{'游戏名称':<25} | {'库存':<5} | {'价格':<8} | {'状态'}")
                print("-" * 60)
                for item in inventory_data:
                    print(f"{item['name'][:24]:<25} | {item['stock']:<5} | {item['price']:<8} | {item['status']}")
                print("-" * 60 + "\n")
            
            return inventory_data

        except Exception as e:
            print(f"🚨 [扫描异常]: {e}")
            return []
    async def action_fill_post_form(self, game_name, key_code, price):
        """
        处理三阶段填表：搜索 -> 选定版本 -> 录入 Key/价格 -> 提交
        """
        print(f"🚀 [动作] 启动全自动上架：{game_name}")
        
        try:
            # 1. 触发弹窗并锁定活跃层
            add_btn = await self.page.wait_for_selector("button:has-text('添加CDKey')")
            await add_btn.click(force=True)
            
            await asyncio.sleep(1.0)
            all_modals = await self.page.query_selector_all(".ivu-modal-content")
            active_modal = None
            for modal in reversed(all_modals):
                if await modal.is_visible():
                    active_modal = modal
                    break
            
            if not active_modal:
                print("🚨 未找到活跃弹窗")
                return False

            # 2. 搜索阶段
            input_box = await active_modal.wait_for_selector(".addCdkIpt")
            search_btn = await active_modal.wait_for_selector(".addCDKBtn")
            await input_box.fill(game_name)
            await search_btn.click()
            
            # 3. 选择版本阶段
            print("⏳ 等待搜索结果列表...")
            target_selection = await active_modal.wait_for_selector(
                f".c-point:has(.gameNameCDK:has-text('{game_name}'))", 
                timeout=8000
            )
            await target_selection.click()
            print(f"🎯 已选中版本: {game_name}")

            # 4. 录入数据阶段
            key_area = await active_modal.wait_for_selector("textarea.ivu-input")
            await key_area.fill(key_code)
            
            price_input = await active_modal.wait_for_selector("input[placeholder*='价格']")
            await price_input.click(click_count=3)
            await self.page.keyboard.press("Backspace")
            await price_input.fill(str(price))
            print(f"💰 Key 与价格设定完成: {price}")

            # 5. 人工干预：第一次确认（决定是否点击“提交”）
            print("\n" + "⚠️ " * 10)
            print("表单已填好！请检查浏览器。")
            print(f"游戏: {game_name} | 价格: {price} | Key: {key_code}")
            print("输入 'yes' 确认【提交并处理二次确认】，输入其他取消。")
            print("⚠️ " * 10 + "\n")

            user_input = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            
            if "yes" in user_input.lower():
                # --- A. 点击初步提交按钮（黑色） ---
                print("🚀 正在执行初步提交...")
                submit_btn = await active_modal.wait_for_selector("button.ivu-btn-error")
                await submit_btn.click()
                
                # --- B. 处理“注意！！”二次确认弹窗 ---
                await asyncio.sleep(1.5) # 等待新弹窗动画
                print("🔍 正在捕捉终极确认弹窗...")
                
                all_modals_v2 = await self.page.query_selector_all(".ivu-modal-content")
                final_confirm_modal = None
                
                # 再次利用倒序法锁定最上层的“注意！！”弹窗
                for modal in reversed(all_modals_v2):
                    modal_text = await modal.inner_text()
                    if "注意！！" in modal_text and await modal.is_visible():
                        final_confirm_modal = modal
                        break
                
                if final_confirm_modal:
                    print("⚠️ 发现安全警告弹窗，正在执行【确认出售】...")
                    # 锁定蓝色背景的大按钮
                    confirm_btn = await final_confirm_modal.wait_for_selector("button.ivu-btn-info")
                    await confirm_btn.click()
                    
                    # --- C. 结果检查 ---
                    await asyncio.sleep(2)
                    # 检查是否有验证码或成功提示
                    captcha = await self.page.query_selector(".captcha-popup")
                    if captcha:
                        print("🛡️ 触发验证码！请在浏览器手动完成滑动。")
                    else:
                        print("✨ 上架流程已完整结束！请检查看板列表。")
                else:
                    print("🚨 未能触发二次确认弹窗，请手动检查浏览器。")
            else:
                print("❌ 指令撤回，已取消提交。")
            
            return True

        except Exception as e:
            print(f"🚨 [上架流程崩溃]: {e}")
            return False
        
if __name__ == "__main__":
    commander = SteamPyMonitor(headless=True)
    asyncio.run(commander.run_commander())