import requests

url = "https://open.feishu.cn/open-apis/bot/v2/hook/70423ec9-8744-40c2-a3af-c94bbbd0990a"
payload = {
    "msg_type": "text",
    "content": {
        "text": "报告！指挥官，套利系统连接测试成功。关键词：报告"
    }
}
res = requests.post(url, json=payload)
print(res.json()) # 如果看到 {"code":0,"msg":"success"} 就成了！