import asyncio
import os
import datetime
import sys
from sonkwo_scout_core import SonkwoScout
from tabulate import tabulate

from difflib import SequenceMatcher

def is_similar(a, b, threshold=0.6):
    # 计算两个名字的相似度，0.6 是个平衡点
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= threshold

class SonkwoCNMonitor(SonkwoScout):
    # --- 1. 杉果雷达逻辑 ---
    async def get_current_state(self):
        """
        全区雷达：自动适配 CN/HK 页面，通过 URL 特征精准判定状态
        """
        try:
            url = self.page.url
            # 基础区域判定
            region = "HK" if "sonkwo.hk" in url else "CN"
            
            # 1. 登录状态识别 (仅保留一个最稳的选择器：头像)
            # 杉果登录后通常会有 user_avatar 或包含 ID 的头像框
            is_logged_in = await self.page.query_selector(".avatar, .user-avatar, .new-avatar-block")
            login_flag = " [已登录]" if is_logged_in else " [未登录]"

            # 2. 页面类型识别 (实事求是：URL 路由匹配)
            page_type = "UNKNOWN"
            # 判定结算页：只要包含 oneclick 或 orders/confirm 就是结算页
            if "type=oneclick" in url or "/orders/confirm" in url:
                page_type = f"{region}_CONFIRM"
            if "/sku/" in url:
                # 详情页特征：URL 包含 /sku/
                page_type = f"{region}_DETAIL"
            elif "/search" in url:
                # 列表页特征：URL 包含 /search
                # 同时识别参数
                params = []
                if "key_type=steam_key" in url: params.append("STEAM")
                if "price_status=lowest" in url: params.append("史低")
                flag = f" [{'+'.join(params)}]" if params else ""
                page_type = f"{region}_LIST{flag}"
            elif url.strip("/").endswith(("sonkwo.cn", "sonkwo.hk")):
                # 首页特征
                page_type = f"{region}_HOME"

            # 3. 最终汇总
            return f"页面:{page_type}{login_flag} | URL: ...{url[-40:]}"
            
        except Exception as e:
            return f"雷达干扰中... ({str(e)[:10]})"

    # --- 2. 新标签页自动接管 ---
    async def handle_new_page(self):
        def on_page(new_page):
            async def setup_page():
                await new_page.wait_for_load_state("domcontentloaded")
                self.page = new_page
                print(f"\n[SYSTEM] 🛡️ 雷达切入新标签: {new_page.url[-20:]}")
                new_page.on("close", lambda p: self.switch_to_last_page())
            asyncio.create_task(setup_page())
        self.context.on("page", on_page)

    def switch_to_last_page(self):
        if self.context.pages:
            self.page = self.context.pages[-1]
            print(f"\n[SYSTEM] 🔙 返回上一页。")

    async def radar_task(self):
        while True:
            try:
                if self.page and not self.page.is_closed():
                    state = await self.get_current_state()
                    now = datetime.datetime.now().strftime("%H:%M:%S")
                    sys.stdout.write(f"\r[{now}] 🛰️  {state} | 指令 >> ")
                    sys.stdout.flush()
            except: pass
            await asyncio.sleep(1)

    async def get_search_results(self, keyword):
        """
        原子动作：仅负责搜索并返回结构化数据清单
        """
        url = f"https://www.sonkwo.cn/store/search?keyword={keyword}&key_type=steam_key&price_status=lowest"
        await self.page.goto(url)
        
        try:
            await self.page.wait_for_selector(".sku-list-item", timeout=5000)
            items = await self.page.query_selector_all(".sku-list-item")
            
            data_list = []
            for i, item in enumerate(items):
                t_el = await item.query_selector(".title")
                p_el = await item.query_selector(".SKC-sale-price")
                is_lowest = await item.query_selector(".lowest") is not None
                
                if t_el and p_el:
                    data_list.append({
                        "index": i + 1,
                        "title": (await t_el.text_content()).strip(),
                        "price": (await p_el.text_content()).strip(),
                        "is_lowest": is_lowest,
                        "handle": t_el # 存下这个句柄，方便待会儿直接点
                    })
            return data_list
        except:
            return []

    async def click_item(self, index, current_list):
        """
        原子动作：根据索引进入特定游戏详情页
        """
        if 0 < index <= len(current_list):
            target = current_list[index-1]
            print(f"🚀 正在切入目标：{target['title']}")
            await target['handle'].click()
            return True
        print("❌ 索引越界，目标不存在。")
        return False

    async def action_search(self, name):
        """硬核搜索：URL 优先，实事求是诊断结果"""
        print(f"\n[COMMAND] 🔍 正在检索 [Steam+史低] 目标: {name}")
        
        # 1. 构造“最终态”URL
        target_url = f"https://www.sonkwo.cn/store/search?keyword={name}&key_type=steam_key&price_status=lowest"
        await self.page.goto(target_url)
        
        try:
           # 2. 关键：等待列表加载。只要这个出来了，就说明“有货”
            await self.page.wait_for_selector(".sku-list-item", timeout=5000)
            
            # 3. 抓取当前所有可见的游戏卡片
            items = await self.page.query_selector_all(".sku-list-item")
            
            print(f"\n📡 侦察报告：在当前页面发现 {len(items)} 个匹配目标：")
            print("-" * 60)
            
            for i, item in enumerate(items, 1):
                # 适配你提供的最新 HTML 结构
                t_el = await item.query_selector(".title")
                p_el = await item.query_selector(".SKC-sale-price")
                lowest_tag = await item.query_selector(".lowest")
                
                if t_el and p_el:
                    title = (await t_el.text_content()).strip()
                    price = (await p_el.text_content()).strip()
                    status = " [史低]" if lowest_tag else ""
                    print(f"[{i}] {title} | {price}{status}")
            
            print("-" * 60)
            
            # 成功完成任务，直接返回，不再往下跑那些会导致报错的诊断逻辑
            return True
                    
        except Exception as e:
            # 即使超时，我们也看一眼当前的 URL 状态
            if "price_status=lowest" in self.page.url:
                print(f"📌 超时诊断：未能在时限内加载出 [史低] 结果，判定为：当前无史低。")
            else:
                print(f"🚨 搜索异常: {e}")

    # --- 4. 启动主循环 ---
    async def run_sonkwo(self):
        await self.start()
        await self.handle_new_page()
        asyncio.create_task(self.radar_task())

        print("\n" + "🇨🇳 " * 15 + "\n杉果侦察兵已启动（原子化架构）\n" + "🇨🇳 " * 15 + "\n")

        try:
            while True:
                cmd_raw = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
                cmd = cmd_raw.strip()
                if not cmd: continue
                if cmd == "exit": break

                # 1. 结构化搜索：搜到结果立即展示列表
                elif cmd.startswith("search ") or cmd.startswith("scan "):
                    name = cmd.replace("search ", "").replace("scan ", "")
                    print(f"\n[COMMAND] 🔍 正在检索 [Steam+史低] 目标: {name}")
                    self.current_results = await self.get_search_results(name)
                    
                    if self.current_results:
                        print(f"\n📡 发现 {len(self.current_results)} 个匹配目标：")
                        for item in self.current_results:
                            tag = "[史低]" if item['is_lowest'] else ""
                            print(f"[{item['index']}] {item['title']} | {item['price']} {tag}")
                    else:
                        print(f"📌 结果：'{name}' 目前无 [Steam+史低] 商品。")

                # 2. 索引跳转：输入数字点进详情
                elif cmd.isdigit():
                    if hasattr(self, 'current_results'):
                        await self.click_item(int(cmd), self.current_results)
                    else:
                        print("⚠️ 请先 search [游戏名]")

                # 3. 详情解析：s 专门用于详情页深度提取（券后价、倒计时）
                # 逻辑 A：通用扫描指令 's'
                elif cmd == "s" or cmd == "scan":
                    state = await self.get_current_state()
                    
                    if "DETAIL" in state:
                        # 1. 如果在详情页，执行深度数据提取
                        await self.action_scan_detail()
                    
                    elif "CONFIRM" in state:
                        # 2. 如果在结算页，先做【风险评估】，再做【订单核对】
                        # 自动调用你想要的两个函数
                        await self.action_check_region_risk() 
                        await self.action_scan_confirm()
                    else:
                        print("💡 当前页面无需扫描，若需看列表请用 search。")

                # 逻辑 B：通用动作指令 'buy' 或 'submit'
                elif cmd == "buy" or cmd == "submit":
                    state = await self.get_current_state()
                    
                    if "DETAIL" in state:
                        # 1. 在详情页，buy 代表“立即购买”跳转结算
                        await self.action_buy() 
                    
                    elif "CONFIRM" in state:
                        # 2. 在结算页，buy/submit 代表“提交订单”临门一脚
                        # 调用最终提交函数
                        await self.action_submit_order()
                    else:
                        print("❌ 当前状态无法执行购买/提交动作。")

        finally:
            await self.stop()
    async def action_scan_detail(self):
        """精准提取详情页套利情报 (修复游戏名抓取错误)"""
        print("\n[ANALYSIS] 🧐 正在深度解析详情页...")
        try:
            # 1. 标题：精准锁定 .sku-cn-name，绝不误抓昵称
            title_el = await self.page.query_selector(".sku-cn-name")
            title = (await title_el.text_content()).strip() if title_el else "未知游戏"
            
            # 2. 价格：锁定右侧侧边栏的价格容器，避免抓到下方推荐位的价格
            # 在侧边栏中，券后价是 .coupon_price 或 .SKC-sale-price
            # 我们直接锁定右侧价格栏的 class
            price_container = await self.page.query_selector(".sku-price-info-box")
            if price_container:
                coupon_price_el = await price_container.query_selector(".coupon_price")
                sale_price_el = await price_container.query_selector(".SKC-sale-price")
                final_price = (await coupon_price_el.text_content()).strip() if coupon_price_el else \
                              (await sale_price_el.text_content()).strip()
            else:
                final_price = "获取价格失败"

            # 3. 史低状态：检查是否存在 lowest 类
            is_lowest = await self.page.query_selector(".lowest") is not None
            lowest_tag = "🔥 [官方认证史低]" if is_lowest else "⚠️ [非史低]"

            print("-" * 50)
            print(f"📦 目标游戏：{title}")
            print(f"💰 最终进货价：{final_price}")
            print(f"📉 价格状态：{lowest_tag}")
            print("-" * 50)
            
        except Exception as e:
            print(f"🚨 详情页解析发生错误: {e}")
    async def action_check_region_risk(self):
        """[风险判定] 检查 HK 环境买 CN 商品的风险"""
        print("\n[SECURITY] 🛡️ 区域风险评估...")
        url = self.page.url
        # 寻找 HTML 中标记区域的类名
        is_cn_sku = await self.page.query_selector(".region-cn") is not None
        if "sonkwo.hk" in url and is_cn_sku:
            print("🚨 警告：检测到【港区环境】正在购买【国区商品】！")
            print("   请确保你有大陆 IP 节点用于 Steam 激活，否则可能报错。")
        else:
            print("✅ 区域校验：商品与环境匹配。")

    async def action_scan_confirm(self):
        """[数据核对] 修复：精准定位【已勾选】的优惠券及套利提醒"""
        print("\n[CONFIRM] 🧾 正在核对订单详情...")
        try:
            # 1. 提取最终实付金额 (这个最准)
            total_el = await self.page.query_selector(".totalPrice .num")
            price = await total_el.text_content() if total_el else "未知"

            # 2. 定位【已勾选】的优惠券 (寻找那个 fa-check 图标所在的父容器)
            selected_coupon_box = await self.page.query_selector(".new-cart-confirm-item:has(.SK-express-border-layer)")
            if selected_coupon_box:
                coupon_name_el = await selected_coupon_box.query_selector(".coupon-name")
                coupon_name = await coupon_name_el.text_content()
                coupon_status = f"✅ 已勾选：{coupon_name.strip()}"
            else:
                coupon_status = "❌ 未检测到勾选优惠券"

            # 3. 捕捉极致套利机会 (再买￥0.9减￥10)
            # 只要这个 reach-minimum-hint 出现，说明有负成本凑单机会
            arbitrage_hint = await self.page.query_selector(".reach-minimum-block")
            arbitrage_msg = ""
            if arbitrage_hint:
                hint_text = await arbitrage_hint.inner_text()
                arbitrage_msg = f"\n🔥 [套利提醒] {hint_text.replace('去凑单', '').strip()}"
                arbitrage_msg += "\n   策略：随便买个 1 元游戏，总价还能再降 5 元！"

            print("-" * 50)
            print(f"💰 实际支付：{price.strip()}")
            print(f"🎫 优惠券态：{coupon_status}")
            if arbitrage_msg:
                print(arbitrage_msg)
            print("-" * 50)
            print("💡 输入 'buy' 提交订单，或去 '凑单' 拿更高折扣。")

        except Exception as e:
            print(f"🚨 结算页解析异常: {e}")
    async def action_submit_order(self):
        """[最终动作] 提交订单"""
        print("\n[ACTION] 🚀 正在提交订单并跳转支付...")
        btn = await self.page.query_selector("text=提交订单")
        if btn:
            await btn.click()
            print("✅ 提交成功！请在浏览器手动完成扫码支付。")
        else:
            print("❌ 没找到提交按钮，可能页面未加载完。")
    async def action_buy(self):
        """执行跳转：从详情页进入结算页"""
        print("\n[ACTION] 🛒 尝试发起下单流程...")
        try:
            # 锁定购买按钮
            buy_btn = await self.page.query_selector(".one-click") or \
                      await self.page.query_selector("text='立即购买'")
            
            if buy_btn:
                print("⚡ 点击购买按钮...")
                await buy_btn.click()
                
                # 关键：改为等待结算页特有的容器元素，而不是死等 URL 字符串
                try:
                    # 你的 HTML 显示结算页容器是 .new-cart-confirm-container
                    await self.page.wait_for_selector(".new-cart-confirm-container", timeout=5000)
                    print("✅ 成功到达结算确认页。")
                except:
                    # 此时雷达如果已经显示 CONFIRM，说明其实跳到了，只是元素加载慢
                    print("📌 正在等待结算页元素渲染...")
            else:
                print("❌ 未找到购买按钮。")
        except Exception as e:
            print(f"🚨 跳转异常: {e}")

if __name__ == "__main__":
    monitor = SonkwoCNMonitor(headless=False)
    asyncio.run(monitor.run_sonkwo())