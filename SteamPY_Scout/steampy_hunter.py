import asyncio
import re
import datetime
import json
import time
from SteamPY_Scout.steampy_scout_core import SteamPyScout
from tabulate import tabulate
import sys
import os


def generate_search_variants(name: str) -> list:
    """
    生成搜索变体，应对不同平台命名差异
    策略：由精确到模糊，命中即停
    """
    variants = []

    # === 第一梯队：原名及标点变换 ===
    variants.append(name)                                    # 原名
    variants.append(re.sub(r'[：:，,。\.·・\-—]', ' ', name))  # 标点→空格
    variants.append(re.sub(r'[：:，,。\.·・\-—]', '', name))   # 标点删除

    # === 第二梯队：副标题截断 ===
    # "生化危机4：重制版" → "生化危机4"
    if re.search(r'[：:\-—]', name):
        base_name = re.split(r'[：:\-—]', name)[0].strip()
        if len(base_name) >= 2:  # 避免截得太短
            variants.append(base_name)

    # === 第三梯队：数字/罗马数字互转 ===
    roman_map = [
        ('1', 'I'), ('2', 'II'), ('3', 'III'), ('4', 'IV'),
        ('5', 'V'), ('6', 'VI'), ('7', 'VII'), ('8', 'VIII'),
        ('9', 'IX'), ('10', 'X')
    ]
    for arabic, roman in roman_map:
        if arabic in name:
            variants.append(name.replace(arabic, roman))
        if roman in name.upper():
            # 保持原大小写风格
            variants.append(re.sub(roman, arabic, name, flags=re.I))

    # === 第四梯队：空格/无空格变体 ===
    # "GTA 5" vs "GTA5"
    variants.append(re.sub(r'\s+', '', name))       # 删除所有空格
    variants.append(re.sub(r'(\D)(\d)', r'\1 \2', name))  # 字母数字间加空格

    # === 第五梯队：常见别名处理 ===
    alias_map = {
        '艾尔登法环': ['Elden Ring', '老头环'],
        '黑神话悟空': ['Black Myth Wukong', '黑神话：悟空'],
        '赛博朋克2077': ['Cyberpunk 2077'],
    }
    clean_name = re.sub(r'[：:，,。\.·・\-—\s]', '', name)
    for key, aliases in alias_map.items():
        if clean_name == key or name in aliases:
            variants.extend(aliases)
            variants.append(key)

    # === 去重保序 ===
    seen = set()
    result = []
    for v in variants:
        v = ' '.join(v.split()).strip()  # 清理多余空格
        if v and v.lower() not in seen:
            seen.add(v.lower())
            result.append(v)

    return result


def extract_version(name: str) -> str:
    """
    提取商品版本标签，归一化为统一标识
    无标签或标准版都归一化为 'STANDARD'
    """
    # 版本映射表（按优先级排序，长标签优先匹配）
    version_map = [
        # 超级豪华版系列（必须在豪华版之前）
        ("超级豪华版", "ULTIMATE"), ("超豪华版", "ULTIMATE"), ("ULTIMATE", "ULTIMATE"),
        # 终极版系列（必须在豪华版之前）
        ("终极版", "ULTIMATE"), ("最终版", "ULTIMATE"),
        # 豪华版系列
        ("豪华版", "DELUXE"), ("DELUXE", "DELUXE"),
        # 黄金版系列
        ("黄金版", "GOLD"), ("GOLD EDITION", "GOLD"), ("GOLD", "GOLD"),
        # 年度版系列
        ("年度版", "GOTY"), ("GOTY", "GOTY"), ("GAME OF THE YEAR", "GOTY"),
        # 完整版系列
        ("完整版", "COMPLETE"), ("完全版", "COMPLETE"), ("COMPLETE", "COMPLETE"),
        # 典藏版系列
        ("典藏版", "COLLECTOR"), ("COLLECTOR", "COLLECTOR"),
        # 标准版系列（放最后）
        ("标准版", "STANDARD"), ("STANDARD", "STANDARD"),
    ]

    name_upper = name.upper()
    for tag, normalized in version_map:
        if tag.upper() in name_upper:
            return normalized

    return "STANDARD"  # 无标签 = 标准版


class SteamPyMonitor(SteamPyScout):
    # --- 缓存配置 ---
    CACHE_FILE = "data/search_cache.json"
    LEDGER_FILE = "data/purchase_ledger.json"
    SALES_FILE = "data/steampy_sales.json"
    CACHE_TTL = 86400  # 缓存有效期 24 小时

    def __init__(self, **kwargs):
        # 💡 先调用父类的初始化
        super().__init__(**kwargs)
        # 💡 显式声明这个成员变量，初始为空
        self.notifier = None
        self._shot_counter = 0 # 顺便初始化你的截图计数器

    # --- 🗄️ 搜索缓存系统 ---
    def _load_json_safe(self, filepath):
        """安全加载 JSON 文件，失败返回空"""
        try:
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            print(f"⚠️ [缓存] 读取 {filepath} 失败: {e}")
        return [] if filepath.endswith("ledger.json") or filepath.endswith("sales.json") else {}

    def _get_name_by_cdkey(self, cd_key):
        """
        第一层缓存：通过 cd_key 从历史匹配中获取 SteamPy 名称
        purchase_ledger + steampy_sales 中 cd_key 相同的记录
        """
        if not cd_key:
            return None

        cd_key_upper = cd_key.strip().upper()

        # 加载两个文件
        ledger = self._load_json_safe(self.LEDGER_FILE)
        sales = self._load_json_safe(self.SALES_FILE)

        # 构建 sales 的 cd_key -> name 映射
        sales_map = {}
        for item in sales:
            key = item.get("cd_key", "").strip().upper()
            if key:
                sales_map[key] = item.get("name", "")

        # 在 sales 中查找相同 cd_key
        if cd_key_upper in sales_map:
            steampy_name = sales_map[cd_key_upper]
            if steampy_name:
                return steampy_name

        return None

    def _get_name_from_cache(self, sk_name):
        """
        第二层缓存：从本地搜索缓存中获取 SteamPy 名称
        """
        cache = self._load_json_safe(self.CACHE_FILE)

        if sk_name in cache:
            entry = cache[sk_name]
            cached_at = entry.get("cached_at", 0)

            # 检查是否过期
            if time.time() - cached_at < self.CACHE_TTL:
                return entry.get("steampy_name")
            else:
                print(f"⚠️ [缓存] 名称缓存已过期: {sk_name}")

        return None

    def _save_to_cache(self, sk_name, steampy_name):
        """
        保存搜索结果到本地缓存
        """
        try:
            cache = self._load_json_safe(self.CACHE_FILE)

            cache[sk_name] = {
                "steampy_name": steampy_name,
                "cached_at": time.time()
            }

            # 确保目录存在
            os.makedirs(os.path.dirname(self.CACHE_FILE), exist_ok=True)

            with open(self.CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)

            print(f"💾 [缓存] 已保存: {sk_name} → {steampy_name}")
        except Exception as e:
            print(f"⚠️ [缓存] 保存失败: {e}")

    async def _get_current_game_name(self):
        """
        从当前详情页获取游戏名称
        """
        try:
            name_el = await self.page.query_selector(".gameName")
            if name_el:
                return (await name_el.text_content()).strip()
        except Exception as e:
            print(f"⚠️ [缓存] 获取当前游戏名失败: {e}")
        return None

    async def search_with_cache(self, sk_name, cd_key=None, original_name=None):
        """
        带缓存的搜索，三层 fallback：
        1. cd_key 精准匹配（历史成交记录）
        2. 名称缓存（本地搜索缓存）
        3. 原始搜索（兜底）
        """
        source_name = original_name or sk_name

        # === 第一层：cd_key 精准匹配 ===
        if cd_key:
            cached_name = self._get_name_by_cdkey(cd_key)
            if cached_name:
                print(f"📦 [缓存] cd_key 命中: {sk_name} → {cached_name}")
                success = await self.action_search(cached_name, original_name=source_name)
                if success:
                    return True
                print(f"⚠️ [缓存] cd_key 缓存名搜索失败，继续 fallback")

        # === 第二层：名称缓存 ===
        cached_name = self._get_name_from_cache(sk_name)
        if cached_name:
            print(f"📦 [缓存] 名称命中: {sk_name} → {cached_name}")
            success = await self.action_search(cached_name, original_name=source_name)
            if success:
                return True
            print(f"⚠️ [缓存] 名称缓存搜索失败，继续 fallback")

        # === 兜底：原始搜索 ===
        print(f"🔍 [搜索] 缓存未命中，走原始流程: {sk_name}")
        success = await self.action_search(sk_name, original_name=source_name)

        # 成功后回写缓存
        if success:
            actual_name = await self._get_current_game_name()
            if actual_name and actual_name != sk_name:
                self._save_to_cache(sk_name, actual_name)

        return success

    # --- 📸 侦察机黑匣子系统 ---
    async def take_screenshot(self, step_name):
        """
        保存最近 10 张截图，文件名循环覆盖
        """
        if not hasattr(self, '_shot_counter'):
            self._shot_counter = 0
            # 确保截图目录存在
            if not os.path.exists("blackbox"):
                os.makedirs("blackbox")

        # 计数器 0-9 循环
        idx = self._shot_counter % 10
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        filename = f"blackbox/step_{idx}_{step_name}_{timestamp}.png"
        
        try:
            await self.page.screenshot(path=filename)
            print(f"📸 [黑匣子] 记录点 {idx}: {step_name}")
            self._shot_counter += 1
        except Exception as e:
            print(f"🚨 截图失败: {e}")
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
            # 🛡️ 最后尝试：直接跳转到正确的卖家中心 URL
            try:
                print("🔄 尝试直接跳转到卖家中心...")
                await self.page.goto("https://steampy.com/pyUserInfo/sellerCDKey", wait_until="commit", timeout=20000)
                await asyncio.sleep(3.0)
                print("✅ 强制跳转成功。")
            except Exception as final_e:
                print(f"❌ 所有尝试均失败：{final_e}")


    async def action_search(self, name, original_name=None):
        """
        [稳定 Work 版] 搜索内核：采用多轮变体重试 + 权重评分决策

        参数:
            name: 搜索词（可能已降噪），用于生成变体去搜索
            original_name: 原始商品名（用于版本校验），不传则用 name
        """
        import asyncio

        # 版本校验用原始名，搜索用降噪后的 name
        source_name = original_name or name

        # 1. 确保在列表页并初始化
        await self.action_goto()

        # 2. 准备搜索变体：应对 SteamPy 数据库命名不一的问题
        search_variants = generate_search_variants(name)

        cards = []
        search_input = None

        # 3. 循环尝试每一个变体，直到搜到结果
        for variant in search_variants:
            if not variant: continue
            
            print(f"📡 [SteamPy] 尝试搜索变体: [{variant}]")
            
            try:
                if not search_input:
                    search_input = await self.page.wait_for_selector(".ivu-input", timeout=5000)
                
                # 强力清空并填入：点击 -> 全选 -> 退格 -> 模拟输入
                await search_input.click()
                await self.page.keyboard.press("Control+A")
                await self.page.keyboard.press("Backspace")
                await search_input.type(variant, delay=50) # type 比 fill 更能触发 Vue 事件
                await self.page.keyboard.press("Enter")
                
                # 给 Vue 渲染留出充足的缓冲（原来的 2.5s 非常稳）
                await asyncio.sleep(2.5) 
                
                cards = await self.page.query_selector_all(".gameblock")
                if cards:
                    print(f"✅ 变体 [{variant}] 命中 {len(cards)} 个结果！")
                    break
            except Exception as e:
                print(f"🚨 搜索变体 [{variant}] 异常: {e}")
                continue

        if not cards:
            print(f"❌ 搜索结果为空，尝试了所有变体仍未找到: {name}")
            return False

        # 4. 权重评分系统：在结果中筛选出最像"本体"的一个
        source_version = extract_version(source_name)  # 用原始名提取版本
        print(f"📋 [评分系统] 源商品: [{source_name}] 版本: {source_version}")

        scored_results = []
        for idx, card in enumerate(cards, 1):
            name_el = await card.query_selector(".gameName")
            if name_el:
                actual_name = (await name_el.text_content()).strip()
                score = 0
                score_details = []  # 记录得分明细

                # A. 版本一致性校验（最高优先级）
                target_version = extract_version(actual_name)
                if source_version != target_version:
                    score -= 200  # 版本不一致，直接判负
                    score_details.append(f"版本不匹配({source_version}!={target_version}): -200")
                else:
                    score_details.append(f"版本匹配({target_version}): +0")

                # B. 基础分：包含即有分，全等满分
                # 先用原始字符串匹配（保持原有逻辑）
                name_lower = name.lower()
                actual_lower = actual_name.lower()

                if actual_name == name:
                    score += 100
                    score_details.append("完全匹配: +100")
                elif name_lower in actual_lower or actual_lower in name_lower:
                    score += 50
                    score_details.append("包含匹配: +50")
                else:
                    # 备选：去除空格后再匹配（解决「无主之地3」vs「无主之地 3」的问题）
                    name_nospace = name_lower.replace(" ", "")
                    actual_nospace = actual_lower.replace(" ", "")
                    if name_nospace in actual_nospace or actual_nospace in name_nospace:
                        score += 50
                        score_details.append("包含匹配(去空格): +50")
                    else:
                        score_details.append("名称不匹配: +0")

                # C. 负向惩罚：自动排除 DLC、原声带、合集等干扰项
                interference_tags = {
                    "DLC": 80, "扩展": 80, "原声": 90, "SOUNDTRACK": 90,
                    "BUNDLE": 40, "合集": 40, "测试": 90, "体验版": 90
                }
                for tag, penalty in interference_tags.items():
                    if tag.upper() in actual_name.upper():
                        score -= penalty
                        score_details.append(f"干扰项[{tag}]: -{penalty}")

                print(f"   [{idx}] {actual_name} | 版本:{target_version} | 得分:{score} | 明细: {', '.join(score_details)}")
                scored_results.append({"score": score, "card": card, "name": actual_name, "version": target_version})

        # 5. 决策与跳转：只要评分最高者 > 0 就点进去，交给 AI 审计最终版本
        scored_results.sort(key=lambda x: x["score"], reverse=True)

        if scored_results and scored_results[0]["score"] > 0:
            target = scored_results[0]
            print(f"🎯 选定最佳匹配: {target['name']} (得分: {target['score']})")
            best_match = target["card"]
        else:
            print(f"⚠️ 搜索结果中无高分匹配目标 (最高分: {scored_results[0]['score'] if scored_results else 'N/A'})")
            return False

        try:
            await best_match.click()
            # 增加对详情页关键元素的等待
            await self.page.wait_for_selector(".game-title, span:has-text('返回')", timeout=10000)
            return True
        except Exception as e:
            print(f"🚨 详情页进入失败: {e}")
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
                elif cmd == "test":
                    if arg:
                        print(f"🔬 [测试接口] 正在模拟巡航调用: {arg}...")
                        res = await self.get_game_market_price_with_name(arg)
                        if res and len(res) == 3:
                            p, n, t5 = res
                            print(f"✅ 接口返回正常！\n🔹 最低价: {p}\n🔹 商品名: {n}\n🔹 价格阵列: {t5}")
                        else:
                            print(f"❌ 接口返回异常或格式不对: {res}")
                    else:
                        print("⚠️ 用法: test 游戏名")
                print("\n" + "-"*40)
            except KeyboardInterrupt:
                break
        await self.stop()

    async def get_game_market_price_with_name(self, name, original_name=None, cd_key=None):
        """
        [巡航核心] 这里的逻辑必须和手动 scan 成功的逻辑完全一致

        参数:
            name: 搜索词（可能已降噪）
            original_name: 原始商品名（用于版本校验），透传给 action_search
            cd_key: 可选，用于缓存匹配
        """
        try:
            # 💡 使用带缓存的搜索
            if cd_key:
                success = await self.search_with_cache(name, cd_key=cd_key, original_name=original_name)
            else:
                success = await self.search_with_cache(name, original_name=original_name)

            if not success: return None

            await asyncio.sleep(2.0) # 确保表格加载

            # 1. 获取名字
            name_el = await self.page.query_selector(".gameName")
            actual_name = (await name_el.text_content()).strip() if name_el else "未知"

            # 2. 💡 搬运 scan 成功的逻辑：抓取前 5 行价格
            rows = await self.page.query_selector_all(".ivu-table-tbody tr.ivu-table-row")
            top5_prices = []

            for row in rows[:5]:
                cells = await row.query_selector_all("td")
                if len(cells) >= 5:
                    p_text = (await cells[4].text_content()).strip().replace("￥", "").strip()
                    # 正则提取数字，防止 ¥ 符号干扰
                    p_match = re.search(r"\d+\.?\d*", p_text)
                    if p_match:
                        top5_prices.append(float(p_match.group()))

            if top5_prices:
                # 返回：最低价, 实际名, 价格阵列
                return top5_prices[0], actual_name, top5_prices

            return None
        except Exception as e:
            print(f"🚨 巡航抓取异常: {e}")
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
        
    async def action_fill_post_form(self, game_name, key_code, price, auto_confirm=False):
        """
        处理三阶段填表：搜索 -> 选定版本 -> 录入 Key/价格 -> 提交
        :param auto_confirm: 是否开启自动模式。如果为 True，将跳过人工输入确认。
        """
        print(f"🚀 [动作] 启动上架流程：{game_name} (自动模式: {auto_confirm})")
        
        try:
            # 1. 触发弹窗并锁定活跃层
            await self.take_screenshot("before_add_click")
            add_btn = await self.page.wait_for_selector("button:has-text('添加CDKey')")
            await add_btn.click(force=True)
            
            await asyncio.sleep(1.0)
            await self.take_screenshot("modal_opened")
            all_modals = await self.page.query_selector_all(".ivu-modal-content")
            active_modal = None
            for modal in reversed(all_modals):
                if await modal.is_visible():
                    active_modal = modal
                    break
            
            if not active_modal:
                print("🚨 未找到活跃弹窗")
                return False, "🚨 上架失败: 未找到活跃弹窗"

            # 2. 搜索阶段
            input_box = await active_modal.wait_for_selector(".addCdkIpt")
            search_btn = await active_modal.wait_for_selector(".addCDKBtn")
            await input_box.fill(game_name)
            await search_btn.click()
            
            # 3. 选择版本阶段
            print(f"⏳ 正在执行严格版本校验，目标: {game_name}")
            
            # 💡 增加缓冲，确保列表加载完成
            await asyncio.sleep(1.5) 
            
            # 获取所有候选项列表
            options = await active_modal.query_selector_all(".c-point")
            
            found_target = None
            for opt in options:
                name_el = await opt.query_selector(".gameNameCDK")
                if name_el:
                    # 💡 这里抓取的是网页上实际显示的名字
                    actual_text = (await name_el.text_content()).strip()
                    
                    # 💡 核心校验：变量对变量，没有任何硬编码
                    if actual_text == game_name:
                        found_target = opt
                        print(f"🎯 命中！找到 100% 匹配项: {actual_text}")
                        break
                    else:
                        # 仅作为调试记录，不影响运行
                        print(f"⏭️  跳过不匹配项: {actual_text}")
            
            if found_target:
                await found_target.click()
                print(f"✅ 已选中版本: {game_name}")
                await self.take_screenshot("version_selected")
            else:
                # 🛡️ 智能熔断：如果搜出来的名字和你传进来的 game_name 不一样，直接报错
                error_log = f"🚨 严格匹配失败：列表中没有名为 '{game_name}' 的项"
                print(error_log)
                await self.take_screenshot("match_failed_stop")
                return False, error_log

            # 4. 录入数据阶段
            key_area = await active_modal.wait_for_selector("textarea.ivu-input")
            await key_area.fill(key_code)
            
            price_input = await active_modal.wait_for_selector("input[placeholder*='价格']")
            await price_input.click(click_count=3)
            await self.page.keyboard.press("Backspace")
            await price_input.fill(str(price))
            print(f"💰 Key 与价格设定完成: {price}")

            # 5. 提交逻辑分流
            should_submit = False

            if auto_confirm:
                # 💡 自动模式：直接判定为需要提交
                print("🤖 [自动模式] 正在跳过人工确认，执行自动提交...")
                should_submit = True
            else:
                # 💡 人工模式：保留原有的终端输入提示
                print("\n" + "⚠️ " * 10)
                print("表单已填好！请检查浏览器。")
                print(f"游戏: {game_name} | 价格: {price} | Key: {key_code}")
                print("输入 'yes' 确认【提交并处理二次确认】，输入其他取消。")
                print("⚠️ " * 10 + "\n")

                user_input = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
                if "yes" in user_input.lower():
                    should_submit = True
            await self.take_screenshot("form_filled")
            # 执行提交动作
            if should_submit:
                # --- A. 点击初步提交按钮（黑色） ---
                print("🚀 正在执行初步提交...")
                submit_btn = await active_modal.wait_for_selector("button.ivu-btn-error")
                await submit_btn.click()
                
                # --- B. 处理“注意！！”二次确认弹窗 ---
                await asyncio.sleep(2.0) # 等待新弹窗动画
                print("🔍 正在捕捉终极确认弹窗...")
                await self.take_screenshot("first_submit_done")
                all_modals_v2 = await self.page.query_selector_all(".ivu-modal-content")
                final_confirm_modal = None
                
                for modal in reversed(all_modals_v2):
                    modal_text = await modal.inner_text()
                    if "注意！！" in modal_text and await modal.is_visible():
                        final_confirm_modal = modal
                        break
                
                if final_confirm_modal:
                    print("⚠️ 发现安全警告弹窗，正在执行【确认出售】...")
                    await self.take_screenshot("final_warning_check")
                    confirm_btn = await final_confirm_modal.wait_for_selector("button.ivu-btn-info")
                    await confirm_btn.click()
                    
                    # --- C. 结果检查 ---
                    await asyncio.sleep(2)
                    captcha = await self.page.query_selector(".captcha-popup")
                    await self.take_screenshot("post_result_final")
                    if captcha:
                        msg = f"🛡️ {game_name} 触发验证码！请去浏览器手动滑动。"
                        print(msg)
                        return False, msg # 💡 明确返回失败状态
                    else:
                        msg = f"✅ {game_name} 已成功挂载，价格: ¥{price}。"
                        print("✨ 上架流程已完整结束！")
                        return True, msg # 💡 成功出口 1
                else:
                    msg = f"🚨 {game_name} 未能触发二次确认弹窗，可能上架受限（如sku禁售）。"
                    print(msg)
                    return False, msg # 💡 失败出口：未见确认弹窗
            else:
                msg = "❌ 已取消提交（人工/手动干预）。"
                print(msg)
                return False, msg # 💡 失败出口：取消操作

        except Exception as e:
            print(f"🚨 [上架流程崩溃]: {e}")
            return False, f"🚨 上架失败: {e}"
        
    async def action_post_flow(self, arg, notifier=None):
        """
        处理远程下达的 post 指令：解析参数并执行上架
        """
        if not notifier:
            notifier = self.notifier
        try:
            game_name, key_code, price = arg.split("|")
            print(f"🛰️ [执行中] 目标: {game_name} | 价格: {price}")
            
            # 1. 确保在卖家中心
            await self.action_goto_seller_post()
            
            # 2. 执行填表逻辑 (这里调用你已有的 action_fill_post_form)
            # 注意：需将 action_fill_post_form 里的 input() 逻辑在全自动模式下跳过
            success, msg = await self.action_fill_post_form(game_name, key_code, price, auto_confirm=True)
            
            # 3. 💡 直接使用传入的 notifier，不再 import，彻底解决报错
            if notifier:
                await notifier.send_text(f"🛰️ 上架回执：\n{msg}")
            else:
                print(f"⚠️ 未接通通知器，上架结果: {msg}")
            
            return success
        except Exception as e:
            print(f"🚨 上架指令执行失败: {e}")
            return False
        
if __name__ == "__main__":
    commander = SteamPyMonitor(headless=True)
    asyncio.run(commander.run_commander())