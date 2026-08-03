"""Web Push 配信基盤(デスク → 本人スマホへの通知)。

役割:
- VAPID (Voluntary Application Server Identification) 鍵の生成・保管
- PWA (Progressive Web App) からの購読(push subscription)の登録・解除
- トリアージ「即対応」時の通知送出。送信に恒久失敗した購読は自動掃除

設計原則の再確認: これは本人スマホへの「通知」であって LINE への送信ではない。
このモジュールも他のどこも、LINE へ送信する手段をコードに持たない。

iOS の制約: Web Push はホーム画面追加済みの PWA でのみ動く(iOS 16.4+)。
Safari のタブ内では purchase 不可 → UI 側で案内する。
"""
import json
import os
import threading

from . import config, db

try:
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid02
    from py_vapid.utils import b64urlencode
    from cryptography.hazmat.primitives import serialization
    AVAILABLE = True
except ImportError:  # 未インストール環境でも他機能は動かす
    AVAILABLE = False

# 購読先プッシュサービスに提示する連絡先(実運用では運営者の実アドレスに)
VAPID_SUB = os.environ.get("CHOUBA_VAPID_SUB", "mailto:admin@chouba.invalid")

_lock = threading.Lock()


def _key_path() -> str:
    p = os.environ.get("CHOUBA_VAPID")
    if p:
        return p
    base = os.path.dirname(os.path.abspath(config.DB_PATH))
    return os.path.join(base, "vapid_private.pem")


def ensure_keys() -> str:
    """秘密鍵ファイルが無ければ生成して返す(初回起動時に自動生成)。"""
    path = _key_path()
    with _lock:
        if not os.path.exists(path):
            v = Vapid02()
            v.generate_keys()
            v.save_key(path)
    return path


def public_key() -> str:
    """PWA の pushManager.subscribe に渡す applicationServerKey (base64url)。"""
    if not AVAILABLE:
        return ""
    v = Vapid02.from_file(ensure_keys())
    raw = v.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64urlencode(raw)


def _send_one(sub: dict, payload: str) -> bool:
    try:
        webpush(subscription_info=sub, data=payload,
                vapid_private_key=ensure_keys(),
                vapid_claims={"sub": VAPID_SUB})
        return True
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (403, 404, 410):  # 失効・購読解除済み → 掃除
            db.delete_subscription(sub.get("endpoint", ""))
        return False
    except Exception:
        return False


def notify(title: str, body: str, url: str = "/", tag: str | None = None) -> int:
    """登録済みの全端末へ送出。戻り値 = 成功数。"""
    if not AVAILABLE:
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag},
                         ensure_ascii=False)
    sent = 0
    for row in db.list_subscriptions():
        try:
            sub = json.loads(row["subscription_json"])
        except (ValueError, TypeError):
            db.delete_subscription(row["endpoint"])
            continue
        if _send_one(sub, payload):
            sent += 1
    return sent


def notify_async(title: str, body: str, url: str = "/", tag: str | None = None) -> None:
    """API 応答をブロックしないための fire-and-forget 送出。"""
    threading.Thread(target=notify, args=(title, body, url, tag), daemon=True).start()
