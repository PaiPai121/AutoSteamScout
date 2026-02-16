import requests
import json
import httpx # 确保文件顶部有这个导入

class FeishuNotifier:
    def __init__(self, webhook_url):
        self.webhook_url = webhook_url

    def send_arbitrage_report(self, games):
        """发送富文本套利报告"""
        post_data = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": "🛰️ 杉果 x SteamPy 套利侦察报告",
                        "content": []
                    }
                }
            }
        }
        
        segments = []
        for i, game in enumerate(games, 1):
            segments.append([
                {"tag": "text", "text": f"{i}. {game['title']}\n"},
                {"tag": "text", "text": f"   💰 进货: ￥{game['sk_price']} | 出货: ￥{game['py_price']}\n"},
                {"tag": "text", "text": f"   🔥 预期纯利: ￥{game['profit']:.2f}\n"},
                {"tag": "a", "text": "🔗 点击进货", "href": game['url']},
                {"tag": "text", "text": "\n------------------\n"}
            ])
        
        post_data["content"]["post"]["zh_cn"]["content"] = segments
        
        response = requests.post(self.webhook_url, json=post_data)
        return response.json()
    
    async def send_text(self, text: str):
        """发送纯文本消息，用于巡航简报和系统通知"""
        payload = {
            "msg_type": "text",
            "content": {"text": text}
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(self.webhook_url, json=payload, timeout=10.0)
                response.raise_for_status()
        except Exception as e:
            # 记录日志，但不让发消息的失败搞崩整个巡航逻辑
            print(f"❌ 飞书文本推送失败: {e}")