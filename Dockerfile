# 帳場 クラウド配信用イメージ(Hugging Face Spaces / Render / Railway / Fly いずれも可)
FROM python:3.12-slim

WORKDIR /app

# http-ece(pywebpush依存)のビルド失敗を避ける
ENV SETUPTOOLS_USE_DISTUTILS=stdlib \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 本番: ダミー投入なし。Androidサブ端末の通知受信モードを既定にする。
# 認証・トークン・APIキー・DB保存先は Render の環境変数で設定すること:
#   CHOUBA_PASSWORD(玄関パスワード) / CHOUBA_INGEST_TOKEN(Android/ショートカット用)
#   ANTHROPIC_API_KEY / CHOUBA_DB(永続ディスク使用時 例 /var/data/chouba.db)
ENV CHOUBA_WATCHER=android

# HF Spaces は 7860 を期待。Render/Railway/Fly は $PORT を渡してくるのでそれを優先。
EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
