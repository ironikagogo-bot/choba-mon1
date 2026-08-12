"""仮想デスク常駐サービス(デモ盤の中核)。

本番仕様と同じ流れをこのプロセス内で再現する:

  開設(provision) → LINEログイン承認 → ACTIVE → ウォッチャーが受信を検知
  → トリアージ → 即対応なら下書きを先に生成 → 本人スマホへプッシュ通知

本物と違うのは「LINEを読む部分が台本(SimulatorWatcher)」という一点だけ。
フェーズ2ではウォッチャーを RealProvisioner + 実LINE読取アダプタに差し替える。

このモジュールにも送信機能は存在しない(設計原則)。

プライバシー原則(シミュレーションで確定済み):
  プッシュ通知の本文に客のメッセージ原文は載せない(ロック画面の覗き見対策)。
"""
import re
import threading
import time
from collections import deque

from . import config, db, drafts, push, triage
from .notify_ingest import parse_line_notification, NotifyDedup
from .provisioning import MockProvisioner, LocalMacProvisioner, DeskState
from .watcher import SimulatorWatcher, MacAccessibilityWatcher

CAT_LABEL = {"urgent": "即対応", "rally": "ラリー", "batch": "まとめ"}

# デモの台本: (開始からの秒, 相手, 本文)。約90秒で1日のダイジェストが流れる。
# 即対応2件(来店・同伴)、ラリー1組、まとめ2〜3件になるよう設計。
DEMO_SCRIPT = [
    (4,  "S.部長", "今日のゴルフ、スコア散々だったよ笑 108叩いた"),
    (18, "T.会長", "今夜9時ごろ、3名で寄れそうだが席あるかな"),
    (40, "K.専務", "このあいだ話してた取引先の件だけどさ"),
    (52, "K.専務", "今度その人を連れて行くよ、面白い人だから"),
    (70, "Y.社長", "出張から戻りました"),
    (85, "M.先生", "同伴の件、木曜あたりでどうかな"),
    (30, "たけし", "明日って空いてる？久しぶりに行きたい"),   # 未登録(仕事型)
    (60, "ミカ", "今度ごはん行こー！みんなで集まらない？"),  # 未登録(私用型)
    # 対応モードのデモ(架空): いなしON済み客 / 未設定客(下ネタアラームが出る) / ガチ恋客
    (95, "宮下さん", "この前言ってた温泉の写真、際どいやつお願いします笑"),
    (108, "早乙女さん", "水着姿の写真も、お願いします🙏✨"),
    (120, "神楽さん", "今日もずっと考えてました。俺のこと、好き？"),
]


def ingest(contact: str, text: str, log=None, predraft: bool = False, src_ts: float = 0.0):
    """受信1件を本番と同じ順で処理する(APIエンドポイントとウォッチャー共用)。

    predraft=True (デスク経由):
      即対応は「下書きを先に生成 → 完成後に通知」の順(本番仕様)。別スレッドで行う。
    predraft=False (手動API):
      従来どおり即時に通知だけ送る(下書きは開いたときに生成)。
    戻り値: (message_id, category, reason)
    """
    log = log or (lambda m: None)
    _unknown = False
    try:
        from . import crm
        _res = crm.resolve_incoming(contact)
        if _res["action"] == "muted":
            log(f"私用として除外: {contact}")
            return None, "muted", "muted"
        if _res["action"] == "unknown":
            _unknown = True
            crm.record_pending(contact, text)
        elif _res.get("contact"):
            contact = _res["contact"]
    except Exception as _e:
        log(f"CRM解決スキップ: {_e}")
    if not db.get_contact(contact):
        db.upsert_contact(contact, "B")
        log(f"未登録の相手: {contact}(ランクB)")
    if _unknown:
        try:
            from . import crm as _crm
            _crm.mark_unlinked(contact)
        except Exception:
            pass

    preview = text if len(text) <= 18 else text[:18] + "…"
    log(f"受信検知: {contact}「{preview}」")

    cat, reason = triage.classify(contact, text)
    # v175: 通知の元時刻(src_ts)があればそれを保存する。従来はサーバ受信時刻だったため、
    # 電波復帰などでAPKがまとめて再送すると「3時間前のメッセージが今来た」ことになり、
    # 並び順・ラリー判定・「過去メッセージの再出現」誤認の原因になっていた(監査で発見)。
    mid = db.add_message(contact, text, cat, reason, ts=(src_ts or None))
    log(f"判定: {CAT_LABEL[cat]} — {reason}")

    # v191その2(#9): 同一相手の未対応旧midに残る下書きキャッシュは、この新着で文脈落ちする
    # (ラリー連結: 開いたカードに後続到着前の古い下書きが即返しされる)。捨てて、開いた時に
    # 最新の連結文脈で再生成させる。
    try:
        _stale = [m["id"] for m in (db.open_for_contact(contact) or []) if m["id"] != mid]
        if _stale:
            with db.conn() as c:
                c.execute("DELETE FROM drafts WHERE message_id IN (%s)"
                          % ",".join("?" * len(_stale)), _stale)
    except Exception:
        pass

    # エロ/ガチ恋フラグの相手は、カテゴリに関わらず受信時に裏で下書きを事前生成
    # (検品パスで生成が遅くなるぶん、開いた時には出来ている状態にする)
    # v191その2(#9/#11): customer限定(v187§10)+生成先はキュー表示と同じアンカーmid
    # (urgent優先→最古open)1本だけ。従来は受信毎に当該midへ生成し、開いたカードには出ない
    # 非表示midの下書きを通数分(API課金つきで)裏生成していた。
    if not _unknown:
        try:
            _c = db.get_contact(contact) or {}
            if ((_c.get("kind") or "customer") == "customer"
                    and (int(_c.get("flag_koi") or 0) == 1 or int(_c.get("flag_ero") or 0) == 1)):
                _opens = db.open_for_contact(contact) or []
                _urg = [m for m in _opens if (m.get("category") or "") == "urgent"]
                _pool = _urg or _opens
                _anchor = (min(_pool, key=lambda m: (m.get("ts") or 0))["id"] if _pool else mid)
                threading.Thread(target=lambda a=_anchor: drafts.generate(a), daemon=True).start()
        except Exception:
            pass
    # v190(#12): 店内(staff)は通知無音(キューには残る)。
    _kind_n = (db.get_contact(contact) or {}).get("kind") or "customer"
    if cat == "urgent" and not _unknown and _kind_n == "staff":
        log("通知しない(店内の連絡は無音)")
    # v192: グループの他人宛て発言・システム行では鳴らさない(キュー表示と同じ判定)
    _grp_ok = True
    if cat == "urgent" and not _unknown:
        try:
            from . import linebot as _lbg
            _grp_ok = _lbg.group_visible(text, contact)
            if not _grp_ok:
                log("通知しない(グループ内の他人宛て発言)")
        except Exception:
            _grp_ok = True
    # v190(d-3): 同一相手への緊急通知は15分に1回(ラリー内昇格と併せて枠を守る)
    _dedup_ok = True
    if cat == "urgent" and not _unknown and _kind_n != "staff" and _grp_ok:
        try:
            from . import linebot as _lb0
            _last = float(_lb0._meta_get(f"upush_{contact}") or 0)
            _dedup_ok = (time.time() - _last) >= 900
            if _dedup_ok:
                _lb0._meta_set(f"upush_{contact}", str(time.time()))
            else:
                log("通知しない(15分以内に通知済み)")
        except Exception:
            _dedup_ok = True
    if cat == "urgent" and not _unknown and _kind_n != "staff" and _grp_ok and _dedup_ok:
        # v191(#18): 通知時刻を記録(urgent精度=通知→応答時間の計測用)
        try:
            with db.conn() as c:
                c.execute("UPDATE messages SET notified_ts=? WHERE id=?", (time.time(), mid))
        except Exception:
            pass
        # v190(#2): 匿名化。ロック画面に客名・用件を載せない(覗き見は毎晩の環境条件)
        title = "帳場｜1件届いています"
        # v123: LINEチャットにも1通(Web Push購読が無くても届く経路。月間上限つき)
        def _lp():
            try:
                from . import linebot as _lb
                _lb.push_urgent(contact, reason)
            except Exception as e:
                log(f"LINE緊急通知に失敗: {e}")
        threading.Thread(target=_lp, daemon=True).start()
        if predraft:
            def work():
                ds = drafts.generate(mid)
                log(f"下書き{len(ds)}案を生成")
                n = push.notify(title, "下書きあり・タップして確認",
                                url="/", tag=f"msg-{mid}")
                log(f"スマホへ通知送出({n}端末)")
            threading.Thread(target=work, daemon=True).start()
        else:
            # 通知本文に原文は載せない(覗き見対策)
            push.notify_async(title, "タップして確認", url="/", tag=f"msg-{mid}")
    elif cat == "rally":
        log("通知しない(ラリー継続中)")
    elif cat == "batch":
        log("通知しない(まとめ箱へ)")
    return mid, cat, reason


class DeskService:
    """1サーバー1デスク(パイロット規模)の常駐サービス。"""

    def __init__(self, mode=None):
        self._lock = threading.Lock()
        self.mode = mode or config.WATCHER   # "sim" or "mac"
        self.provisioner = LocalMacProvisioner() if self.mode == "mac" else MockProvisioner()
        self.desk = None
        self.watcher = None
        self.console = deque(maxlen=200)
        self.user_id = None
        self.speed = 1.0
        self.watch_started_ts = None
        self.dedup = NotifyDedup()       # android通知の重複除去
        self.android_count = 0           # 取り込んだ通知の累計

    # ---- コンソール(裏側ログ) ----
    def log(self, msg: str):
        self.console.append({"ts": time.time(), "text": msg})

    # ---- ライフサイクル ----
    def start(self, user_id: str = "demo", speed: float = 1.0):
        """デスク開設: 仮想PC起動(模擬) → LINEログインQR提示待ちへ。"""
        with self._lock:
            self._stop_watcher()
            self.user_id = user_id
            self.speed = max(0.0, speed)
            self.console.clear()
            self.log("デスク開設を受け付けました")
            if self.mode == "android":
                self.log("Androidのサブ端末からの通知を待ち受けます(本命構成)")
                self.log("送信元: /api/android/notify(転送アプリからPOST)")
                self.desk = self.provisioner.provision(user_id)
                self.log("承認で受け付けを開始します")
            elif self.mode == "mac":
                self.log("このMacのPC版LINEに接続します(道A・実読み取り)")
                self.log("LINEの起動とアクセシビリティ許可を確認中…")
                self.desk = self.provisioner.provision(user_id)
                detail = getattr(self.desk, "_probe_detail", "")
                if detail:
                    self.log(detail)
                if self.desk.state == DeskState.FAILED:
                    self.log("接続不可: 手順書(REAL_LINE_SETUP.md)の確認事項を見てください")
                else:
                    self.log("読み取り疎通OK。承認で監視を開始できます")
            else:
                self.log("仮想PCを起動中…(デモ: 模擬)")
                self.desk = self.provisioner.provision(user_id)
                self.log("PC版LINEを起動、ログインQRを取得(デモ: 模擬)")
            return self.desk

    def approve_login(self):
        """本人が②QRをLINEアプリで承認した(デモではタップで再現)。"""
        with self._lock:
            if not self.desk:
                raise RuntimeError("desk not started")
            if self.desk.state == DeskState.FAILED:
                raise RuntimeError("desk not ready")
            state = self.provisioner.poll_login(self.desk)
            if state == DeskState.ACTIVE and not self.watcher:
                if self.mode == "android":
                    self.log("受信の待ち受けを開始(Androidの通知を受け付けます・送信機能なし)")
                    # androidは受動受信。ポーリングするウォッチャーは動かさない
                elif self.mode == "mac":
                    self.log("監視を開始(PC版LINEを読み取りのみ・送信機能なし)")
                    self._start_watcher()
                else:
                    self.log("LINEログインの承認を確認")
                    self.log("監視セッション開始(読み取りのみ・送信機能なし)")
                    self._start_watcher()
            return state

    def android_ingest(self, package: str, title: str, text: str,
                       ticker: str = "", big_text: str = "", sub_text: str = "",
                       text_lines: str = "", key: str = "", ts: str = "",
                       sender: str = "", group: str = "") -> dict:
        """Androidの通知1件を取り込む。/api/android/notify から呼ばれる。

        長文などで title/text が空のとき、他の欄(big_text=大きな文字, text_lines=テキスト行,
        ticker=概要, sub_text=サブテキスト)から本文・相手名を補う。
        戻り値: {status: ingested|ignored|duplicate, contact?, message?, category?}
        """
        with self._lock:
            # 診断: 空でない欄を全部コンソールに出す(どの欄に本文が入るか実データで特定)
            fields = {"title": title, "text": text, "big_text": big_text,
                      "text_lines": text_lines, "ticker": ticker, "sub_text": sub_text}
            nonempty = {k: (v or "").strip() for k, v in fields.items() if (v or "").strip()}
            if nonempty:
                self.log("通知の中身: " + ", ".join(
                    f"{k}={v[:30]!r}" for k, v in nonempty.items()))
            else:
                self.log("通知の中身: すべて空(この通知は本文を持たない=まとめ通知の可能性)")

            # 空欄を他の欄で補完(長文=大きな文字/テキスト行、概要="送信者: 本文"形式)
            rtitle = (title or "").strip() or (sub_text or "").strip()
            # v175: text_lines(まとめ通知のInboxStyle)は「その会話の直近数通」が複数行で入る。
            # 丸ごと採用すると取り込み済みの過去行が混ざり、毎回本文が微妙に違う重複カードが
            # 生まれる(監査で発見)。使う場合は最終行=最新の1通だけを採る。
            _tl = (text_lines or "").strip()
            if _tl and "\n" in _tl:
                _tl = [x for x in _tl.split("\n") if x.strip()][-1].strip()
            rtext = ((text or "").strip() or (big_text or "").strip() or _tl)
            tk = (ticker or "").strip()
            if (not rtitle or not rtext) and tk:
                m = re.match(r"^(?P<s>[^:：]{1,40})[:：]\s*(?P<m>.+)$", tk, re.S)
                if m:
                    rtitle = rtitle or m.group("s").strip()
                    rtext = rtext or m.group("m").strip()
                else:
                    rtext = rtext or tk

            # ★C5本格版: リーダー(v0.3+)が sender/group を別項目で送ってきた場合は
            # ヒューリスティック解析を使わず確定情報として採用(グループ3形式すべて根治)
            _sender = (sender or "").strip()
            _group = (group or "").strip()
            # v199: group欄が空でも、sub_text(Androidのグループ通知でグループ名が入ることがある)
            # から「グループらしさ」を推定する。確定情報ではないため 【?名前】 の疑い印にする:
            # 画面には👥タグと注記が出るが、「自分宛てだけ」フィルタでは隠さない(誤検知でDMを
            # 消す事故の方が重い)。実測で精度が確認できたら確定印に昇格する。
            _gguess = ""
            if _sender and not _group:
                _st = (sub_text or "").strip()
                if (_st and _st != _sender and _st.upper() not in ("LINE", "LINE LITE")
                        and 1 <= len(_st) <= 30 and _st != (title or "").strip()):
                    _gguess = _st
                    self.log(f"グループの疑い(sub_textより): {_st!r}")
            if _sender:
                parsed = {"contact": _sender,
                          "message": (f"【{_group}】" + rtext) if _group
                          else (f"【?{_gguess}】" + rtext) if _gguess else rtext}
                # 通話系はここでも除外
                from .notify_ingest import is_call_notice, _CALL_TITLE
                if _CALL_TITLE.search(rtitle) or is_call_notice(rtext):
                    parsed = None
            else:
                # v177: 旧リーダー(sender欄なし)でも、titleが既存カード/エイリアスに
                # 完全一致するなら確実に1:1。本文の「集合: 19時に」等をグループ発言と
                # 誤認して偽カードを湧かせない。crm.resolve_incomingは自動紐付けの
                # 副作用があるため使わず、読み取り専用チェックに留める。
                _known = False
                if rtitle:
                    try:
                        if db.get_contact(rtitle):
                            _known = True
                        else:
                            with db.conn() as _c0:
                                _r0 = _c0.execute(
                                    "SELECT contact FROM contact_aliases WHERE line_name=?",
                                    (rtitle,)).fetchone()
                            _known = bool(_r0)
                    except Exception as _e:
                        print(f"[known_title] {_e}", flush=True)
                parsed = parse_line_notification(rtitle, rtext, package, known_title=_known)
            if not parsed:
                self.log("→ 取り込まない(LINE以外/要約/相手不明)")
                return {"status": "ignored"}
            # v131: グループ/一斉配信の混入対策。送信者不明のグループ通知や、
            # グループ名らしき相手名(「A & B」「A、B」等)はカード化しない(ぐちゃっと問題)
            import re as _reg
            _cn = parsed.get("contact") or ""
            if (_group and not _sender) or _reg.search(r"[,、]| & |＆", _cn):
                self.log(f"→ グループ/一斉配信とみなして取り込まない({_cn[:24]})")
                return {"status": "ignored-group"}
            # v141: 旧リーダー(〜v0.5)のグループ通知はタイトルが「グループ名: 送信者」。
            # そのままだとグループ名がカード名に付いて回る(実例: 焼肉大好き: Yuji Tsuboi)。
            # 人名だけをカード名にし、グループ名は本文頭の【】印として文脈に残す。
            if not _sender:
                from . import crm as _crm
                _g, _p = _crm.group_split(_cn)
                if _g and _p:
                    parsed["contact"] = _p
                    parsed["message"] = f"【{_g}】" + (parsed.get("message") or "")
                    self.log(f"→ グループ「{_g}」内の{_p}さんとして取り込み")

            contact, message = parsed["contact"], parsed["message"]
            # v154: 帳場くん同士の共鳴を遮断。別インスタンス(「Michiの帳場」等)のOA通知や
            # 帳場くん自身の押し通知文がリーダー経由で再取り込みされ、「未登録の相手:
            # Michiの帳場・急ぎの気配(急ぎの気配)」という意味不明カードが生まれていた(2026-08-08実例)
            _BOT_SIGS = ("メニューの📨返信から", "から急ぎの気配", "今月の緊急通知が上限",
                         "ひも付けが完了しました", "わたしの帳場くんにする",
                         "1件届いています",    # v190: 匿名通知文言(同時更新必須=a-4)
                         "対応済みになりました")   # v205: ✓全消化通知(同上)
            if ("帳場" in contact) or any(sig in (message or "") for sig in _BOT_SIGS):
                # v168: 接続テスト(ループチェック)の折り返し検出。ダッシュボードのテストは
                # 本人のLINEへ「🔧接続テスト {id}」をpushし、それがリーダー経由でここへ
                # 戻ってくるかを見る(戻ってきた=通知表示→APK捕捉→送信の経路全部が今生きている証拠)。
                # 検出して記録した後は従来通り取り込まない=カードは汚れない。
                if "🔧接続テスト" in (message or ""):
                    try:
                        import json as _json
                        from . import linebot as _lb
                        cur = _json.loads(_lb._meta_get("loopcheck") or "{}")
                        if cur.get("status") == "waiting" and cur.get("id") and cur["id"] in message:
                            cur["status"] = "ok"
                            cur["echo_ts"] = time.time()
                            _lb._meta_set("loopcheck", _json.dumps(cur))
                            self.log("→ 🔧接続テストの折り返しを確認(経路は正常)")
                    except Exception as _e:
                        self.log(f"[loopcheck] {_e}")
                self.log(f"→ 帳場くん系の通知なので取り込まない({contact[:24]})")
                return {"status": "ignored-bot"}
            msg_id = f"{key}|{ts}" if (key or "").strip() else None
            if not self.dedup.should_process(contact, message, msg_id=msg_id):
                self.log(f"→ 重複のためスキップ: {contact}(同一通知の再掲)")
                return {"status": "duplicate", "contact": contact}
            # v175: 通知IDの重複判定をDBに永続化。従来はプロセスメモリ60秒だけだったため、
            # サーバ再起動(再デプロイ・スリープ復帰)後にAPKが通知を再スキャンすると
            # 全通が新規openとして再挿入され「対応済みの過去メッセージが再出現」していた(監査で発見)。
            if msg_id:
                try:
                    with db.conn() as _c:
                        _c.execute("CREATE TABLE IF NOT EXISTS ingest_keys"
                                   "(k TEXT PRIMARY KEY, ts REAL)")
                        _c.execute("DELETE FROM ingest_keys WHERE ts < ?", (time.time() - 7 * 86400,))
                        _cur = _c.execute("INSERT OR IGNORE INTO ingest_keys(k,ts) VALUES(?,?)",
                                          (msg_id, time.time()))
                        if _cur.rowcount == 0:
                            self.log(f"→ 重複のためスキップ: {contact}(取り込み済みの通知ID)")
                            return {"status": "duplicate", "contact": contact}
                except Exception as _e:
                    self.log(f"[ingest_keys] {_e}")

            # v175: 通知の元時刻を正規化(ミリ秒→秒、7日以上前・10分以上未来は不採用=サーバ時刻)
            _sts = 0.0
            try:
                _sts = float(ts or 0)
                if _sts > 1e12:
                    _sts /= 1000.0
                _now = time.time()
                if not (_now - 7 * 86400 <= _sts <= _now + 600):
                    _sts = 0.0
            except Exception:
                _sts = 0.0

            self.android_count += 1
        # v72(9-9): AIトリアージ(最大12秒)を含む取り込みはロックの外で実行。
        # 通知バースト時に解析・重複判定まで直列化されてリーダー側がタイムアウトする問題の修正。
        # (重複判定は上のロック内で完了済みなので、ここが並行しても二重取り込みは起きない)
        mid, cat, reason = ingest(contact, message, log=self.log, predraft=True, src_ts=_sts)
        if mid is None:
            return {"status": "muted", "contact": contact}
        return {"status": "ingested", "contact": contact, "message": message,
                "category": cat, "reason": reason, "id": mid}

    def replay(self, clear_messages: bool = True):
        """受信デモをもう一度流す(台本モードのみ。実読み取りでは無効)。"""
        with self._lock:
            if self.mode != "sim":
                raise RuntimeError("replay is demo-only")
            if not self.desk or self.desk.state != DeskState.ACTIVE:
                raise RuntimeError("desk not active")
            self._stop_watcher()
            if clear_messages:
                db.clear_demo_messages()
                self.log("受信箱をリセット(デモ)")
            self.log("受信デモを最初から再生します")
            self._start_watcher()

    def stop(self):
        with self._lock:
            self._stop_watcher()
            if self.desk:
                self.provisioner.destroy(self.desk)
                self.log("デスクを停止し、環境を破棄しました")
                self.desk = None

    def status(self) -> dict:
        state = self.desk.state.value if self.desk else "none"
        return {
            "desk_state": state,
            "mode": self.mode,           # "sim"=台本デモ / "mac"=実読み取り / "android"=通知受信
            "watching": self.watcher is not None,
            "speed": self.speed,
            "android_count": self.android_count,
            "console": list(self.console)[-12:],
        }

    # ---- 内部 ----
    def _start_watcher(self):
        on_incoming = lambda contact, text: ingest(contact, text, log=self.log, predraft=True)
        if self.mode == "mac":
            self.watcher = MacAccessibilityWatcher(
                on_incoming,
                on_session_lost=self._on_session_lost,
                poll_sec=config.WATCH_POLL_SEC)
        else:
            self.watcher = SimulatorWatcher(
                on_incoming, script=DEMO_SCRIPT, speed=self.speed)
        self.watcher.start()
        self.watch_started_ts = time.time()

    def _on_session_lost(self):
        self.log("警告: LINEが読めなくなりました(セッション切れ/LINE終了)")
        self.log("フェイルセーフ: LINEアプリの通知を一時的にオンに戻してください")
        if self.desk:
            self.desk.state = DeskState.SESSION_LOST

    def _stop_watcher(self):
        if self.watcher:
            self.watcher.stop()
            self.watcher = None


SERVICE = DeskService()
