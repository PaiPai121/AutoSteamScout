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
        """核心能力 1：降噪提取（增强保护版）"""
        prompt = (
            "你是一个游戏搜索专家。请从商品标题中提取出核心名称。\n"
            "【强制准则】：\n"
            "1. 绝不能删除游戏本身名字的一部分！例如 '空洞骑士' 不能缩减为 '空洞'，'赛博朋克 2077' 不能缩减为 '赛博朋克'。\n"
            "2. 仅删除以下无关后缀：'标准版'、'豪华版'、'Steam版'、'数字版'、'SKU'、'激活码'、'券后价'。\n"
            "3. 如果标题包含中英文，请同时保留（如：Hollow Knight 空洞骑士）。\n"
            f"输入标题：{raw_name}\n"
            "仅输出结果（核心名称）："
        )
        result = self._call_with_retry(prompt)
        print(result)
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