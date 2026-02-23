import asyncio
import json
import os
import datetime

class SyncManager:
    def __init__(self, commander):
        self.commander = commander
        self.sonkwo = commander.finance
        self.steampy = commander.steampy_center

    async def run_full_sync(self):
        """🚀 执行全量同步任务 (双上下文安全版)"""
        print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] 🔄 启动跨平台一键同步...")
        
        # --- 阶段 A: 杉果订单同步 (使用杉果自己的上下文) ---
        sync_page = await self.commander.sonkwo.context.new_page()
        
        try:
            # --- 阶段 A: 杉果订单同步 ---
            print("📍 [1/2] 正在提取杉果采购成本...")
            # await self.sonkwo.action_fetch_ledger(sync_page)
            is_ready = await self.sonkwo.action_verify_and_goto_orders(sync_page)
            if is_ready:
                print("✅ 杉果着陆成功，开始全息抓取...")
                await self.sonkwo.action_fetch_ledger(sync_page)
            else:
                print("❌ 杉果着陆失败（可能登录失效或网络波动），跳过此步")
        except Exception as e:
            print(f"❌ 杉果同步异常: {e}")
        finally:
            await sync_page.close()

        await asyncio.sleep(2) # 避开并发冲突

        # --- 阶段 B: SteamPY 挂单同步 (使用 SteamPY 自己的上下文) ---
        # 💡 核心修复：这里必须从 steampy 的 context 开新页面，否则没有登录状态
        py_page = await self.commander.steampy.context.new_page()
        try:
            print("📍 [2/2] 正在扫描 SteamPY 货架状态...")
            # 💡 增加着陆检查，确保跳转到卖家后台
            is_py_ready = await self.commander.steampy_center.action_verify_and_goto_seller_cdk(py_page)
            if is_py_ready:
                print("✅ SteamPY 着陆成功，开始抓取货架...")
                await self.commander.steampy_center.action_fetch_seller_ledger(py_page)
                print("✨ 同步任务圆满完成！")
                
                # --- 阶段 C: 立即刷新财务快照 (可选) ---
                # 为了让你点完按钮立刻能在网页看到变化，建议捅一下重算
                from Finance_Center.auditor import FinanceAuditor
                await FinanceAuditor().run_detailed_audit(silent=True)
                
                return {"status": "success", "msg": "同步完成"}
            else:
                print("❌ SteamPY 同步失败：无法进入卖家后台")
                return {"status": "error", "msg": "SteamPY 登录失效"}
        except Exception as e:
            print(f"❌ SteamPY 同步异常: {e}")
            return {"status": "error", "msg": str(e)}
        finally:
            await py_page.close()

    def get_summary_report(self):
        """📊 生成汇总对账数据 (用于前端展示)"""
        # 读取两份 JSON 并根据游戏名匹配计算利润
        # 这一部分可以在后续专门写对账逻辑时细化
        pass