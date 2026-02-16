import requests
import json
import httpx # 确保文件顶部有这个导入

class FeishuNotifier:
    def __init__(self, webhook_url):
        self.webhook_url = webhook_url

    def send_arbitrage_report(self, games):
        """发送富文本报告"""
        post_data = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": "🛰️ 杉果 x SteamPy 侦察报告",
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
        """
        升级版：发送富文本消息。
        即便传入的是普通字符串，也会包装成富文本，确保链接、换行完美渲染。
        """
        # 1. 自动处理文本中的 URL，将其转换为可点击的 a 标签（如果需要）
        # 但最简单的办法是直接发 POST 格式
        payload = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": "🛰️ 侦察回报",
                        "content": [
                            [
                                {"tag": "text", "text": text}
                            ]
                        ]
                    }
                }
            }
        }
        
        try:
            async with httpx.AsyncClient() as client:
                # 注意：这里改用异步 httpx 保持一致性
                response = await client.post(self.webhook_url, json=payload, timeout=10.0)
                response.raise_for_status()
                return response.json()
        except Exception as e:
            print(f"❌ 飞书推送失败: {e}")