"""帳場 設定。環境変数で上書き可能。

リポジトリ直下に .env ファイルがあれば読み込む(素人でも「ファイルに書くだけ」で
APIキー等を設定できるように)。既に設定済みの環境変数は上書きしない。
"""
import os


def _load_dotenv():
    path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v


_load_dotenv()

DB_PATH = os.environ.get("CHOUBA_DB", os.path.join(os.path.dirname(__file__), "..", "chouba.db"))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("CHOUBA_MODEL", "claude-sonnet-5")
# トリアージAI用モデル(既定は本体と同じ=すぐ動く。安価な高速モデルを環境変数で指定するとコスト減)
TRIAGE_MODEL = os.environ.get("CHOUBA_TRIAGE_MODEL", ANTHROPIC_MODEL)
# 検品パス用モデル(未指定なら生成と同じ。速くしたい場合はHaiku系を指定)
REVIEW_MODEL = os.environ.get("CHOUBA_REVIEW_MODEL", ANTHROPIC_MODEL)
# 曖昧な受信のみAIで再判定(1=有効・既定)。失敗時は必ずキーワード判定へフォールバック
TRIAGE_AI = os.environ.get("CHOUBA_TRIAGE_AI", "1") == "1"
# ラリー判定: 同一相手からこの分数以内の連続受信でラリー扱い
RALLY_WINDOW_MIN = int(os.environ.get("CHOUBA_RALLY_WINDOW_MIN", "10"))
# まとめ返信の既定時刻(表示用)
BATCH_TIME = os.environ.get("CHOUBA_BATCH_TIME", "21:30")
# ウォッチャー種別:
#   "sim"    = 台本デモ(既定)
#   "mac"    = PC版LINE実読み取り(道A・macOS専用。※LINEが文字を渡さず行き止まりと判明)
#   "android"= Androidサブ端末の通知を受信(本命)。/api/android/notify で受け取る
# クラウドの「見せる用」インスタンスでダミー顧客を自動投入する(実データは載せない)
DEMO = os.environ.get("CHOUBA_DEMO", "") == "1"
# v157: 表示プロファイル。mizu=夜職(既定・本線) / general=Aki用(表示トグルの解禁スイッチ)。
# generalを設定したサーバー(aki-test)だけLIFFホームに🏮⇄👔トグルが現れる。文言のみ・ロジック分岐禁止
MODE = os.environ.get("CHOUBA_MODE", "mizu")
WATCHER = os.environ.get("CHOUBA_WATCHER", "sim")
# 実読み取りのポーリング間隔(秒)
WATCH_POLL_SEC = float(os.environ.get("CHOUBA_WATCH_POLL_SEC", "2.0"))
# Androidからの通知受信を認証する合言葉(空なら認証なし=開発時のみ)
INGEST_TOKEN = os.environ.get("CHOUBA_INGEST_TOKEN", "")
# 玄関認証のパスワード(空なら認証オフ=開発時のみ)。本番は必ず設定する。
PASSWORD = os.environ.get("CHOUBA_PASSWORD", "")

# v72(9-16): 本番(非デモ)でトークン/パスワード未設定なら、トークン境界の公開APIを
# 開けっ放しにせず塞ぐ(=設定漏れ事故の受け皿)。1=閉じる(既定)/0=あえて開ける(開発)。
STRICT_AUTH = os.environ.get("CHOUBA_STRICT_AUTH", "1") == "1"
