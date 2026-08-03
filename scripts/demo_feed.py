"""デモ用: シミュレータの台本をAPIに流し込む。
使い方: サーバー起動後、別ターミナルで python scripts/demo_feed.py
"""
import time
import requests

BASE = "http://localhost:8000"
SCRIPT = [
    (0, "S.部長", "今日のゴルフ、スコア散々だったよ笑 108叩いた"),
    (1, "T.会長", "今夜9時ごろ、3名で寄れそうだが席あるかな"),
    (2, "K.専務", "今日は暇でさ、家で一人飲みしてるんだけど"),
    (3, "K.専務", "ウイスキー。でも一人だとつまらんよ、やっぱり"),
    (4, "Y.社長", "出張から戻りました"),
    (5, "早乙女さん", "水着姿の写真も、お願いします🙏✨"),
]

t0 = time.time()
for delay, contact, text in SCRIPT:
    wait = t0 + delay - time.time()
    if wait > 0:
        time.sleep(wait)
    r = requests.post(f"{BASE}/api/incoming", json={"contact": contact, "text": text})
    print(contact, "->", r.json())
