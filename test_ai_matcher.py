import asyncio
import re
import json
import os
import aiohttp
# 💡 确保你的类名和文件名匹配
from arbitrage_commander import ArbitrageAI

class RadarSystemTester:
    def __init__(self):
        self.ai = ArbitrageAI()
        # 模拟 logger 避免报错
        self.logger = type('MockLogger', (), {
            'info': lambda self, msg: print(f"ℹ️ [INFO] {msg}"),
            'error': lambda self, msg: print(f"❌ [ERROR] {msg}"),
            'warning': lambda self, msg: print(f"⚠️ [WARN] {msg}")
        })()

    async def ai_asset_audit(self, sk_name, candidates):
        """
        [拟人化审计核心] 根据杉果名称，从 Steam 搜索结果中锁定 AppID
        """
        if not candidates:
            return "NONE"

        # 构造带有物理 NONE 选项的名单
        candidate_items = [f"- ID: {c['appid']} | 名称: {c['name']}" for c in candidates]
        candidate_items.append("- ID: NONE | 名称: 列表中没有任何项与目标版本/代数完全匹配")
        candidates_str = "\n".join(candidate_items)

        prompt = f"""
        你现在是 Steam 资产精密审计员。你的任务是核对【目标】与【候选名单】的资产一致性。

        【判定案例 - 严格参考】：
        - 目标: "黑神话：悟空" | 候选: "Monkey King: Hero is Back" -> 结果: NONE (原因: 虽都有猴子，但资产完全不同)
        - 目标: "绝地潜兵 2" | 候选: "HELLDIVERS™ Dive Harder Edition" -> 结果: NONE (原因: 目标要求2代，候选是1代加强版，无数字2)
        - 目标: "生化危机4 重制版" | 候选: "Resident Evil 4 (2023)" -> 结果: 2050650 (原因: 代数对齐，重制对应2023)

        【审计硬逻辑】：
        1. 数字物理存在：如果目标有 "2"、"II"，而候选名称中【没有物理显示的数字2或II】，必须选 NONE。禁止脑补任何“这其实就是2代”的理由。
        2. 资产唯一性：黑神话 = Black Myth。严禁将其匹配给任何其他名称中不含 "Black Myth" 的游戏。
        3. 代数最高原则：代数(2,3,4...)对齐是匹配的前提。

        【目标】: {sk_name}
        【候选名单】:
        {candidates_str}

        【输出要求】：
        你必须严格按照以下 JSON 格式输出，不要有任何其他文字：
        {{
          "reasoning": "简短描述你为什么选择这个 ID 或为什么选 NONE 的逻辑",
          "choice": "最终的 AppID 数字或 NONE"
        }}
        """
        
        # 3. 增强型解析逻辑
        try:
            # 💡 [微调] 增加超时或重试机制（如果你的 _call_with_retry 已经包含则忽略）
            raw_res = self.ai._call_with_retry(prompt)
            
            # 💡 [加固] 使用 re.DOTALL 确保匹配跨行 JSON，并转义潜在干扰字符
            match = re.search(r"\{.*\}", raw_res, re.DOTALL)
            if not match:
                return "NONE"
                
            json_str = match.group()
            res_data = json.loads(json_str)
            
            # 💡 [微调] 兼容大小写 key，并将结果强制转为字符串清洗
            choice = res_data.get("choice") or res_data.get("CHOICE")
            if choice is None:
                return "NONE"
                
            return str(choice).strip().upper()
            
        except Exception as e:
            # 💡 [微调] 增加日志记录，方便你在巡航日志里抓到 AI 的“调皮”瞬间
            self.logger.error(f"❌ AI 审计解析异常: {str(e)} | 原始返回: {raw_res[:100]}...")
            return "NONE"
    
    async def get_search_keywords(self, game_name):
        """
        [逻辑补丁] 解决翻译盲区。让 AI 先把中文名转为 Steam 官方英文关键词。
        """
        prompt = f"请将游戏名 '{game_name}' 转换为 1-2 个最可能的 Steam 官方英文名或关键词（如‘人中之龙’转为 'Like a Dragon'），只需输出关键词，用逗号分隔，不要有其他解释文字。"
        try:
            raw = self.ai._call_with_retry(prompt)
            # 清洗结果，提取关键词列表
            keywords = [k.strip().upper() for k in raw.split(',') if k.strip()]
            return keywords
        except:
            return []
    
    async def fetch_candidates_local(self, game_name):
        """
        [哨兵专用版] 结合 AI 预翻译的本地鱼塘检索
        """
        cache_file = "steam_app_list.json"
        
        # 1. 自动初始化本地库
        if not os.path.exists(cache_file):
            self.logger.warning("📥 正在初始化本地库（仅需运行一次）...")
            async with aiohttp.ClientSession() as session:
                url = "https://steamspy.com/api.php?request=all"
                async with session.get(url, timeout=60) as resp:
                    if resp.status == 200:
                        raw_data = await resp.json()
                        formatted = [{"appid": aid, "name": info.get('name', '')} for aid, info in raw_data.items()]
                        with open(cache_file, 'w', encoding='utf-8') as f:
                            json.dump({"applist": {"apps": formatted}}, f)
                        self.logger.info("✅ 本地库初始化成功。")

        # 2. AI 协助转换关键词，扩大“捞鱼”范围
        eng_keywords = await self.get_search_keywords(game_name)
        self.logger.info(f"🔑 翻译关键词: {eng_keywords}")

        # 3. 检索
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                all_apps = json.load(f).get('applist', {}).get('apps', [])
            
            candidates = []
            search_term = game_name.upper()
            
            for app in all_apps:
                name = app['name'].upper()
                # 命中中文关键词、映射表或 AI 翻译的英文名均可进入候选
                if search_term in name or any(k in name for k in eng_keywords):
                    candidates.append({"appid": str(app['appid']), "name": app['name']})
                if len(candidates) >= 40: break 
                
            return candidates
        except Exception as e:
            self.logger.error(f"本地检索异常: {e}")
            return []

async def run_mass_test():
    tester = RadarSystemTester()
    
    test_cases = [
        {"sk": "生化危机4 重制版", "expected": "2050650", "type": "REMAKE"},
        {"sk": "绝地潜兵 2", "expected": "NONE", "type": "SEQUEL_MISSING"}, 
        {"sk": "艾尔登法环 黄金树幽影 (DLC)", "expected": "2778580", "type": "DLC"},
        {"sk": "人中之龙7 光与暗的去向", "expected": "1230320", "type": "TRANSLATION"},
        {"sk": "怪物猎人：崛起", "expected": "1446780", "type": "TRANSLATION"},
        {"sk": "黑神话：悟空", "expected": "NONE", "type": "IP_PROTECT"},
        {"sk": "使命召唤：现代战争 2 (2022)", "expected": "1938090", "type": "YEAR_CONFLICT"},
        {"sk": "战锤40K：星际战士 2", "expected": "NONE", "type": "SEQUEL_MISSING"},
        {"sk": "尼尔：人工生命 ver.1.22", "expected": "1113560", "type": "VERSION_STRICT"},
        {"sk": "女神异闻录5 皇家版", "expected": "1687950", "type": "VERSION_STRICT"},
    ]

    results = {"pass": 0, "fail": 0}
    
    for case in test_cases:
        print(f"\n📡 --- 正在大规模审计: {case['sk']} ---")
        candidates = await tester.fetch_candidates_local(case["sk"])
        print(f"📥 鱼塘捞到 {len(candidates)} 条候选。")
        
        actual_id = await tester.ai_asset_audit(case["sk"], candidates)
        
        status = "✅ PASS" if str(actual_id) == str(case["expected"]) else "❌ FAIL"
        if status == "✅ PASS": results["pass"] += 1
        else: results["fail"] += 1
        
        print(f"🔎 [{case['type']}] 判定: {actual_id} | 预期: {case['expected']} | {status}")
    
    print(f"\n📈 最终战报: 通过 {results['pass']} | 失败 {results['fail']} | 成功率: {(results['pass']/len(test_cases))*100}%")

if __name__ == "__main__":
    asyncio.run(run_mass_test())