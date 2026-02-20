import requests
import json
import os
import time

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(CURRENT_DIR, "steamspy_all.json")

def sync_all_pages(max_pages=65):
    """
    SteamSpy 每一页大约 1000 条数据。
    循环抓取所有页面并合并成一个大的 json 文件。
    """
    full_library = {}
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    print(f"📡 开始全量同步计划，预计抓取 {max_pages} 个数据分片...")

    for page in range(max_pages):
        url = f"https://steamspy.com/api.php?request=all&page={page}"
        try:
            print(f"🔄 正在抓取第 {page} 页...", end='\r')
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                page_data = response.json()
                if not page_data: # 如果某一页没数据了，提前停止
                    break
                full_library.update(page_data)
                # 稍微缓一下，避免被 SteamSpy 封 IP
                time.sleep(0.5) 
            else:
                print(f"\n❌ 第 {page} 页抓取失败，状态码: {response.status_code}")
                break
        except Exception as e:
            print(f"\n❌ 抓取异常: {e}")
            break

    if full_library:
        with open(DATA_PATH, 'w', encoding='utf-8') as f:
            json.dump(full_library, f, ensure_ascii=False, indent=4)
        
        file_size = os.path.getsize(DATA_PATH) / (1024 * 1024)
        print(f"\n✅ 同步完成！")
        print(f"📊 最终资产总数: {len(full_library)}")
        print(f"💾 文件大小: {file_size:.2f} MB")
        print(f"📂 存储路径: {DATA_PATH}")
    else:
        print("\n❌ 未抓取到任何有效数据。")

if __name__ == "__main__":
    sync_all_pages()