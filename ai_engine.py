import os
import time
import re
from zhipuai import ZhipuAI
from dotenv import load_dotenv

load_dotenv()

class ArbitrageAI:
    def __init__(self):
        api_key = os.getenv("ZHIPU_API_KEY")
        self.model = os.getenv("ZHIPU_MODEL", "glm-4-flash")
        self.client = ZhipuAI(api_key=api_key)

    def _call_with_retry(self, prompt, max_retries=3):
        """通用 API 调用包装器，处理指数退避重试"""
        for i in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=10 # 增加超时控制
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "1305" in err_msg:
                    wait_time = (i + 1) * 3 # 第一次3s, 第二次6s, 第三次9s
                    print(f"⏳ 触发频率限制，正在进行第 {i+1} 次指数退避，等待 {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                print(f"⚠️ AI 调用异常: {err_msg}")
                break
        return None

    def get_search_keyword(self, raw_name):
        """核心能力 1：降噪提取（死命令版）"""
        prompt = (
            "你是一个精通 Steam 数据库的游戏专家。任务是提取用于搜索的【完整核心名】。\n\n"
            "【钢铁律令】：\n"
            "1. 严禁进行任何形式的分词缩减！游戏名必须是完整的实体。\n"
            "2. 错误示范：把 '空洞骑士' 提取为 '空洞' 是致命错误！必须保留 '空洞骑士'。\n"
            "3. 错误示范：把 '生化危机' 提取为 '生化' 是致命错误！必须保留 '生化危机'。\n"
            "4. 必须删除的干扰词：'标准版'、'豪华版'、'Steam版'、'券后价'、'激活码'、'现货'。\n\n"
            "【示例】：\n"
            "输入：'【特惠】Hollow Knight 空洞骑士 标准版'\n"
            "输出：Hollow Knight 空洞骑士\n\n"
            f"输入标题：{raw_name}\n"
            "仅输出结果（核心名称），禁止任何解释或标点："
        )
        result = self._call_with_retry(prompt)
        
        # 💡 [逻辑保险]：如果 AI 还是抽风把长词变短词，强制回退
        if result and len(raw_name) > 4 and len(result) < 3:
            print(f"⚠️ [AI 抽风警告] 提取结果过短({result})，强制回退原名。")
            return raw_name[:10] # 截取前10位保证搜索精度
            
        print(f"====AI 思考结果===: [{result}]")
        return result if result else raw_name

    def verify_version(self, sk_name, py_name):
        """核心能力 2：版本比对（智能分流版）"""
        # --- 策略 1：物理层对齐（直接放过，不花钱） ---
        # 1. 除去空格和标点后完全一致
        def strict_clean(s): return re.sub(r'[：:，,。\.·・\-\s]', '', s).lower()
        
        if strict_clean(sk_name) == strict_clean(py_name):
            print(f"✅ 字符串物理匹配，直接通过。")
            return True

        # --- 策略 2：AI 语义层对齐（处理 XCOM 2 vs 幽浮2） ---
        prompt = (
            "任务：判断商品A和商品B是否为完全相同的游戏版本。\n"
            "判定规则：\n"
            "1. 【别名宽容】：中英文对照（如 'XCOM 2' 与 '幽浮2'）视为 [YES]。\n"
            "2. 【保护长名】：注意区分系列作品，'太阳帝国的原罪 2' 绝不等于其他任何带 '原罪' 的游戏。\n"
            "3. 【版本锁死】：若一方含'豪华版/DLC'而另一方是'标准版'，必须返回 [NO]。\n"
            f"商品A：{sk_name}\n"
            f"商品B：{py_name}\n"
            "仅回复 [YES] 或 [NO]。"
        )
        
        result = self._call_with_retry(prompt)
        if result:
            return "[YES]" in result.upper()
        
        # 失败兜底
        return True
    
    # ai_engine.py 内部

    def quick_call(self, prompt):
        """
        极速审计模式：不进行任何逻辑加工，直接获取 AI 的原话。
        用于 MATCH / VERSION_ERROR / ENTITY_ERROR 的判定。
        """
        try:
            # 💡 假设你类中已有的调用方法是 _call_with_retry 或类似
            # 如果你的方法名不同，请修改这里
            result = self._call_with_retry(prompt)
            
            if result:
                # 简单清洗，只保留大写字母，防止 AI 多嘴带标点
                import re
                clean_res = re.sub(r'[^A-Z_]', '', result.strip().upper())
                return clean_res
            return "ERROR"
        except Exception as e:
            print(f"🚨 AI 审计调用失败: {e}")
            return "ERROR"