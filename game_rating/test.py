import os, sys
current_dir = os.path.dirname(os.path.abspath(__file__))
root_path = os.path.dirname(current_dir)

if root_path not in sys.path:
    sys.path.append(root_path)
import asyncio
from game_rating.LocalGameMatcher import SpyGameMatcher
from game_rating.AssetAuditor import AssetAuditor

async def run_stress_test():
    # 1. 启动引擎
    matcher = SpyGameMatcher()
    if not matcher.initialize():
        return
    
    # 2. 复用 AI 实例给审计官
    auditor = AssetAuditor(ai_handler=matcher.ai)

    # 3. 极具挑战性的测试用例
    # [中文名, 预期 AppID, 挑战点]
    stress_cases = [
        ["生化危机4 重制版", "2050650", "排除2005年旧版和DLC"],
        ["最后生还者 第一部", "1888140", "排除第二部和旧版"],
        ["荒野大镖客：救赎 2", "1174180", "多重子标题翻译"],
        ["对马岛之魂", "2215430", "译名完全不同 (Ghost of Tsushima)"],
        ["巫师3：狂猎", "292030", "排除年度版/DLC组合包"],
        ["使命召唤：现代战争 II", "1938090", "罗马数字与重名冲突"]
    ]

    print("\n" + "═"*60)
    print(f"🚀 资产识别系统：压力测试模式 (共 {len(stress_cases)} 个用例)")
    print("═"*60)

    results = []
    for target, expected_id, challenge in stress_cases:
        print(f"\n🔎 正在处理: 【{target}】")
        print(f"🎯 挑战类型: {challenge}")
        
        # 第一步：捞鱼
        fish = await matcher.fetch_candidates(target)
        if not fish:
            print(f"❌ 检索失败：渔网未捞到任何资产")
            results.append((target, "FAIL", "No candidates"))
            continue
            
        # 第二步：审判
        final_id, reason = await auditor.audit(target, fish)
        
        # 结果判定
        status = "✅ 成功" if final_id == expected_id else "⚠️ 偏差"
        print(f"{status} -> 锁定 ID: {final_id}")
        print(f"📝 AI 理由: {reason}")
        results.append((target, status, final_id))

    # 4. 最终战报
    print("\n" + "═"*60)
    print("📊 最终战报汇总")
    print("═"*60)
    for res in results:
        print(f"目标: {res[0]:<15} | 状态: {res[1]:<10} | ID: {res[2]}")
    print("═"*60)

if __name__ == "__main__":
    asyncio.run(run_stress_test())