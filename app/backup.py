"""v235: バックアップ体制(既知の最重大課題「バックアップゼロ」への対応)。

背景(2026-08-13時点の実態):
- 3サーバーともSQLite単体。世代コピーも外部保管も無く、Renderの再デプロイ事故・
  ディスク未マウント・環境消失でモニターの全データが消える。
- このコンテナから本番へは到達できないため、運用は「Akiがブラウザで口を開く」形に寄せる。

三層構成(それぞれ守る事故が違う):
  ① 自動世代スナップショット(同一ディスク・7世代)
     … 論理事故(誤操作・不正な移行・アプリのバグでの破壊)から戻せる。
       ディスクごと消える事故には無力。
  ② 手元へのダウンロード(owner key口)
     … 環境消失に効く唯一の層。人が定期的に落とす前提なので、ホームに
       「最終バックアップ N日前」を出して忘れを防ぐ。
  ③ 復元(確認2段+検証+復元前退避)
     … 事故後に戻せることまで含めて初めて「バックアップがある」と言える。

整合性: sqlite3のオンラインバックアップAPI(conn.backup)を使う。WAL下で書き込みが
並んでいても壊れたコピーにならない(ファイルcpは壊れうる)。
"""
import os
import shutil
import sqlite3
import threading
import time

from . import config

GENERATIONS = 7          # 自動世代の保持数(日次×7=1週間ぶん)
_AUTO_INTERVAL = 6 * 3600   # 起動後の点検間隔(1日1回だけ実際に取る)
_lock = threading.Lock()
_started = False

# 復元の検証で「帳場くんのDBである」と判断するための必須テーブル
_REQUIRED_TABLES = ("contacts", "messages", "linebot_talks")


def _db_path():
    return os.path.abspath(config.DB_PATH)


def backup_dir():
    d = os.path.join(os.path.dirname(_db_path()), "backups")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        print(f"[backup] mkdir失敗 {d}: {e}", flush=True)
    return d


def _jst_day(ts=None):
    return time.strftime("%Y%m%d", time.gmtime((ts or time.time()) + 9 * 3600))


def snapshot(dest_path):
    """オンラインバックアップAPIで整合スナップショットを作る。戻り: バイト数。"""
    src = sqlite3.connect(_db_path(), timeout=30)
    try:
        tmp = dest_path + ".part"
        if os.path.exists(tmp):
            os.unlink(tmp)
        dst = sqlite3.connect(tmp, timeout=30)
        try:
            src.backup(dst)          # ページ単位のコピー。書き込み中でも壊れない
        finally:
            dst.close()
        os.replace(tmp, dest_path)   # 完成品だけを見せる(途中経過を復元候補にしない)
        return os.path.getsize(dest_path)
    finally:
        src.close()


def auto_snapshot(force=False):
    """日次の自動世代。同じJST日付のものが既にあれば取らない。戻り: path or None。"""
    with _lock:
        d = backup_dir()
        name = f"chouba_{_jst_day()}.db"
        path = os.path.join(d, name)
        if os.path.exists(path) and not force:
            return None
        try:
            n = snapshot(path)
        except Exception as e:
            print(f"[backup] 自動スナップショット失敗: {e}", flush=True)
            return None
        print(f"[backup] {name} ({n:,}バイト)", flush=True)
        _rotate(d)
        return path


def _rotate(d):
    """GENERATIONS世代を超えた古い自動世代を消す(手動退避 pre_restore_* は消さない)。"""
    try:
        gens = sorted(x for x in os.listdir(d)
                      if x.startswith("chouba_") and x.endswith(".db"))
        for old in gens[:-GENERATIONS]:
            os.unlink(os.path.join(d, old))
            print(f"[backup] 世代を削除: {old}", flush=True)
    except Exception as e:
        print(f"[backup] 世代整理失敗: {e}", flush=True)


def generations():
    """自動世代の一覧(新しい順)。[{name, bytes, ts}]"""
    d = backup_dir()
    out = []
    try:
        for x in os.listdir(d):
            if not x.endswith(".db"):
                continue
            p = os.path.join(d, x)
            try:
                st = os.stat(p)
            except Exception:
                continue
            out.append({"name": x, "bytes": st.st_size, "ts": st.st_mtime})
    except Exception:
        pass
    return sorted(out, key=lambda r: r["ts"], reverse=True)


def _persistence():
    """DBの置き場所が永続ディスクらしいかの判定(推定・確信度中)。

    Renderでは永続ディスクは別マウントとして現れるので、アプリ本体と同じ
    デバイスに乗っている=再デプロイで消える可能性が高い、と読む。
    確定はできないので、断定せず「らしい/不明」の語で返す。
    """
    dbp = _db_path()
    appdir = os.path.dirname(os.path.abspath(__file__))
    try:
        same_dev = os.stat(os.path.dirname(dbp)).st_dev == os.stat(appdir).st_dev
    except Exception:
        return "unknown", "置き場所を判定できませんでした"
    inside_repo = os.path.dirname(dbp).startswith(os.path.dirname(appdir))
    if same_dev and inside_repo:
        return "ephemeral", ("アプリ本体と同じ場所にDBがあります。Renderでは"
                             "再デプロイのたびに消える置き場所です(永続ディスクを"
                             "割り当てて CHOUBA_DB=/var/data/chouba.db を設定してください)")
    if same_dev:
        return "unknown", ("アプリ本体と同じディスク上にあります。永続ディスクを"
                           "使っているか、Renderの設定を確認してください")
    return "persistent", "アプリ本体とは別のディスク(永続ディスクらしい)に置かれています"


def last_download_ts():
    from . import linebot
    try:
        return float(linebot._meta_get("backup_dl_ts") or 0)
    except Exception:
        return 0.0


def mark_download():
    from . import linebot
    try:
        linebot._meta_set("backup_dl_ts", str(time.time()))
    except Exception as e:
        print(f"[backup] 取得日時の記録失敗: {e}", flush=True)


def status():
    """ホーム・確認ページ・ダッシュボードが使う状態一式。"""
    dbp = _db_path()
    try:
        size = os.path.getsize(dbp)
    except Exception:
        size = 0
    gens = generations()
    kind, note = _persistence()
    dl = last_download_ts()
    return {
        "db_path": dbp,
        "db_bytes": size,
        "generations": gens[:GENERATIONS + 3],
        "gen_n": len(gens),
        "last_gen_ts": gens[0]["ts"] if gens else 0,
        "last_download_ts": dl,
        "download_age_days": (int((time.time() - dl) / 86400) if dl else None),
        "persistence": kind, "persistence_note": note,
    }


def _loop():
    while True:
        try:
            auto_snapshot()
        except Exception as e:
            print(f"[backup] ループ例外: {e}", flush=True)
        try:
            # v236(S8): 取り込みジョブと原文メタの掃除も、同じ日次の起き際に相乗りさせる
            # (専用スレッドを増やさない)
            from . import liff as _lf
            _lf._jobs_gc()
        except Exception as e:
            print(f"[backup] jobs gc: {e}", flush=True)
        time.sleep(_AUTO_INTERVAL)


def start():
    """起動時に1回+6時間ごとに点検(実際に取るのは1日1回)。多重起動しない。"""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True).start()


def restore_validate(path):
    """アップロードされたファイルが帳場くんのDBかを確かめる。戻り: (ok, 理由, 情報)。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
        if head[:15] != b"SQLite format 3":
            return False, "SQLiteのファイルではありません", {}
    except Exception as e:
        return False, f"読めませんでした: {e}", {}
    try:
        c = sqlite3.connect(path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            names = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            miss = [t for t in _REQUIRED_TABLES if t not in names]
            if miss:
                return False, f"帳場くんのDBではないようです(不足: {'、'.join(miss)})", {}
            info = {
                "contacts": c.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
                "messages": c.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "talks": c.execute("SELECT COUNT(*) FROM linebot_talks").fetchone()[0],
            }
            ok = c.execute("PRAGMA quick_check").fetchone()[0]
            if str(ok).lower() != "ok":
                return False, f"ファイルが壊れています({ok})", info
            return True, "", info
        finally:
            c.close()
    except Exception as e:
        return False, f"確認できませんでした: {e}", {}


def restore(src_path):
    """検証済みファイルで現DBを置き換える。置き換え前の現DBは pre_restore_* に退避。

    WALの取り残しで復元後に古い内容が復活しないよう、-wal/-shm も同時に始末する。
    """
    dbp = _db_path()
    d = backup_dir()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime(time.time() + 9 * 3600))
    bak = os.path.join(d, f"pre_restore_{stamp}.db")
    with _lock:
        try:
            snapshot(bak)      # 戻す前の状態も残す(復元自体の取り消しができるように)
        except Exception as e:
            print(f"[backup] 復元前の退避に失敗: {e}", flush=True)
            bak = ""
        shutil.copyfile(src_path, dbp)
        for suf in ("-wal", "-shm"):
            p = dbp + suf
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except Exception as e:
                    print(f"[backup] {suf}の削除失敗: {e}", flush=True)
    return bak
