import json
import os
import re
import asyncio
import sys
from collections import defaultdict

# 路径修复：确保能找到根目录的 arbitrage_commander
root_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_path not in sys.path:
    sys.path.append(root_path)

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JSON = os.path.join(CURRENT_DIR, "steamspy_all.json")

class SpyGameMatcher:
    def __init__(self, ai_handler=None, spy_json_path=DEFAULT_JSON):
        # 弹性初始化
        if ai_handler is None:
            from arbitrage_commander import ArbitrageAI
            self.ai = ArbitrageAI()
            print("🤖 未检测到外部 AI 实例，已自动初始化内部 AI 引擎。")
        else:
            self.ai = ai_handler
            
        self.spy_json_path = spy_json_path
        self.index = defaultdict(list)
        self.apps = {}
        self.is_ready = False

    def initialize(self):
        """载入 6 万条 SteamSpy 数据并构建倒排索引"""
        if not os.path.exists(self.spy_json_path):
            print(f"❌ 错误: 未找到 {self.spy_json_path}。请先运行同步脚本。")
            return False
        
        try:
            with open(self.spy_json_path, 'r', encoding='utf-8') as f:
                self.apps = json.load(f)

            for appid, info in self.apps.items():
                name = str(info.get('name', '')).upper()
                # 仅索引字母和数字
                tokens = set(re.findall(r'[A-Z0-9]+', name))
                for token in tokens:
                    self.index[token].append(appid)
            
            self.is_ready = True
            print(f"✅ 索引构建完成！当前库内资产: {len(self.apps)} 条。")
            return True
        except Exception as e:
            print(f"❌ 索引构建异常: {e}")
            return False

    async def fetch_candidates(self, game_name, limit=30):
        """
        [漏斗第一层] 广撒网检索
        """
        if not self.is_ready:
            return []

        # 1. AI 提取纯净核心词 (严格约束 Prompt)
        prompt = f"""
        请将游戏名 '{game_name}' 翻译成 Steam 商店中的英文核心单词。
        要求：仅输出 1-2 个核心单词，用逗号分隔。不要输出任何解释，不要带数字。
        示例：'人中之龙7' -> 'Yakuza, Dragon'
        示例：'绝地潜兵 2' -> 'Helldivers'
        示例：'生化危机' -> 'Resident, Evil'
        """
        try:
            raw_keywords = self.ai._call_with_retry(prompt)
            keywords = [k.strip().upper() for k in re.split(r'[,，\s]', raw_keywords) if len(k.strip()) > 1]
            print(f"🔑 AI 提取关键词: {keywords}")
        except:
            keywords = set(re.findall(r'[A-Z]+', game_name.upper()))

        # 2. 倒排索引碰撞 (OR 逻辑)
        hit_ids = set()
        for kw in keywords:
            if kw in self.index:
                hit_ids.update(self.index[kw])

        # 3. 筛选逻辑 (放松限制)
        candidates = []
        target_digits = set(re.findall(r'\d+', game_name))

        for aid in hit_ids:
            app = self.apps[aid]
            app_name = app['name'].upper()
            app_digits = set(re.findall(r'\d+', app_name))
            
            # --- 核心改进：冲突剔除法 ---
            # 只有当两边都有数字，且数字完全不重合时才剔除（比如 4代 vs 6代）
            # 如果一边有一边没有，我们选择保留，交给下游 AI 判定
            if target_digits and app_digits:
                if not (target_digits & app_digits):
                    continue
            
            # 计算基本分：命中的关键词越多排名越靠前
            match_score = sum(1 for kw in keywords if kw in app_name)
            if match_score == 0: continue

            pos = app.get('positive', 0)
            neg = app.get('negative', 1)
            score = int((pos / (pos + neg)) * 100) if (pos + neg) > 0 else 0
            
            candidates.append({
                "appid": str(aid),
                "name": app['name'],
                "info": f"Rating: {score}% | Reviews: {pos + neg}",
                "review_count": pos + neg,
                "match_score": match_score
            })

        # 排序策略：匹配度第一，热度第二
        candidates.sort(key=lambda x: (x['match_score'], x['review_count']), reverse=True)
        return candidates[:limit]

# ==========================================
# 🚀 最终测试入口
# ==========================================
if __name__ == "__main__":
    async def main_test():
        matcher = SpyGameMatcher()
        if not matcher.initialize():
            return

        test_queries = [
            "人中之龙7",          # 挑战：名称无数字匹配
            "生化危机4 重制版",    # 挑战：数字冲突过滤
            "绝地潜兵 2",         # 挑战：翻译准确度
            "艾尔登法环"          # 挑战：大热门 IP
        ]

        print("\n" + "="*50)
        print("📡 离线雷达 (弹性数字逻辑版)")
        print("="*50)

        for q in test_queries:
            print(f"\n🔎 检索目标: [{q}]")
            res = await matcher.fetch_candidates(q)
            if res:
                print(f"   ✅ 成功捞到 {len(res)} 条鱼:")
                for r in res[:5]: # 看前5个，确认“真身”在不在
                    print(f"      - {r['appid']}: {r['name']} ({r['info']})")
            else:
                print(f"   ❌ 搜索落空")

    asyncio.run(main_test())