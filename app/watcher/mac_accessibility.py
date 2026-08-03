"""macOS アクセシビリティで PC版LINE のトーク一覧を読む常駐ウォッチャー（道A・実読み取り）。

設計の要:
  - 「LINEの画面を読む部分(reader)」と「新着を見分ける部分(diff)」を分離する。
    diff は純関数的でこの環境でもテストできる。reader は macOS + pyobjc + 実LINE が要る。
  - 読み取り専用。送信手段はここにも存在しない(設計原則)。
  - トーク一覧の各行 = {contact, snippet, unread}。unread>0 かつ snippet が変化 → 新着とみなす。
    （自分の送信は unread=0 のままなので、この条件で自分の発言を自然に除外できる。）

既知の限界(正直に):
  - トーク一覧の snippet は末尾が省略されることがある。全文は本人がトークを開いたとき取得する想定。
  - 同一相手が連続送信すると snippet が最新に上書きされ、途中の1通を取りこぼしうる。
  - LINE の画面構造(AXツリー)はバージョンで変わる。reader のセレクタは実機で調整前提。
"""
import threading
import time

from .base import BaseWatcher

LINE_BUNDLE_ID = "jp.naver.line.mac"


class MacAccessibilityWatcher(BaseWatcher):
    """PC版LINEのトーク一覧をポーリングし、新着を on_incoming に流す。

    reader: () -> list[dict]  各要素 {"contact": str, "snippet": str, "unread": int}
            省略時は pyobjc による実LINE読み取り(_default_reader)。
    """

    def __init__(self, on_incoming, on_session_lost=None, reader=None, poll_sec=2.0):
        super().__init__(on_incoming, on_session_lost)
        self.reader = reader or _default_reader
        self.poll_sec = poll_sec
        self._last = {}          # contact -> (snippet, unread)
        self._t = None
        self._stop = threading.Event()
        self._fail_streak = 0

    # ---- 新着検知(純粋・テスト対象) ----
    def diff_rows(self, rows):
        """前回状態と比較し、新着とみなす (contact, snippet) の列を返す。副作用で状態更新。"""
        fresh = []
        for row in rows:
            contact = row.get("contact", "").strip()
            snippet = row.get("snippet", "").strip()
            unread = int(row.get("unread", 0) or 0)
            if not contact:
                continue
            prev = self._last.get(contact)
            prev_snippet = prev[0] if prev else None
            # 新着条件: 未読があり、かつ本文が前回と変わっている
            if unread > 0 and snippet and snippet != prev_snippet:
                fresh.append((contact, snippet))
            self._last[contact] = (snippet, unread)
        return fresh

    # ---- 常駐 ----
    def start(self):
        def run():
            while not self._stop.is_set():
                try:
                    rows = self.reader()
                    self._fail_streak = 0
                    for contact, snippet in self.diff_rows(rows):
                        try:
                            self.on_incoming(contact, snippet)
                        except Exception:
                            pass
                except SessionLostError:
                    self._fail_streak += 1
                    if self._fail_streak >= 3:      # 連続で読めない=セッション切れ扱い
                        self.on_session_lost()
                        return
                except Exception:
                    # reader の一時的な失敗は無視して次のポーリングへ
                    self._fail_streak += 1
                self._stop.wait(self.poll_sec)
        self._t = threading.Thread(target=run, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()


class SessionLostError(RuntimeError):
    """LINEが見つからない/ログアウト等、監視継続不能な状態。"""


# ---------------------------------------------------------------------------
# 実LINE読み取り(macOS専用・pyobjc)。このクラウド環境では動かないため、
# import はこの関数の中で行い、他機能のテストを妨げないようにする。
# セレクタは実機のAXツリーに合わせて調整する(scripts/line_probe.py で採取)。
# ---------------------------------------------------------------------------
def _default_reader():
    rows = read_line_chat_list()
    return rows


def _load_ax():
    try:
        from ApplicationServices import (
            AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
        )
        from AppKit import NSWorkspace
        return AXUIElementCreateApplication, AXUIElementCopyAttributeValue, NSWorkspace
    except Exception as e:  # pragma: no cover - macOS専用
        raise SessionLostError(
            "pyobjc(ApplicationServices/AppKit)が読み込めません。"
            "macOSで `pip install -r requirements-macos.txt` を実行してください。"
        ) from e


def _find_line_pid(NSWorkspace):  # pragma: no cover - macOS専用
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == LINE_BUNDLE_ID:
            return app.processIdentifier()
    return None


def _ax_value(copy, element, attr):  # pragma: no cover - macOS専用
    err, val = copy(element, attr, None)
    return val if err == 0 else None


def _walk(copy, element, depth=0, max_depth=40):  # pragma: no cover - macOS専用
    """AXツリーを深さ優先で走査し、(role, value/title/description, element) を列挙。"""
    if depth > max_depth:
        return
    role = _ax_value(copy, element, "AXRole")
    text = (_ax_value(copy, element, "AXValue")
            or _ax_value(copy, element, "AXTitle")
            or _ax_value(copy, element, "AXDescription"))
    yield role, text, element
    children = _ax_value(copy, element, "AXChildren") or []
    for ch in children:
        yield from _walk(copy, ch, depth + 1, max_depth)


def read_line_chat_list():  # pragma: no cover - macOS専用
    """PC版LINEのトーク一覧を読む(実機向け・要セレクタ調整)。

    現状は AXツリー全体からトーク行らしき要素を拾う素朴版。実機の line_probe.py 出力を見て
    行の正確なrole/階層(例: AXRow/AXCell、名前とsnippetと未読バッジの位置)に合わせて絞り込む。
    """
    create, copy, NSWorkspace = _load_ax()
    pid = _find_line_pid(NSWorkspace)
    if pid is None:
        raise SessionLostError("PC版LINEが起動していません(見つかりません)。")
    app = create(pid)
    if app is None:
        raise SessionLostError("LINEのアクセシビリティ要素を取得できません(権限未許可の可能性)。")

    rows = []
    # TODO(実機調整): 実際のトーク一覧のコンテナを特定して、その配下だけを見る。
    # ここでは骨組みとして AXRow を1行=1トークとみなし、子テキストから名前/本文を推定する。
    for role, text, element in _walk(copy, app):
        if role in ("AXRow", "AXCell"):
            texts = []
            for r2, t2, _ in _walk(copy, element, max_depth=6):
                if t2 and isinstance(t2, str) and t2.strip():
                    texts.append(t2.strip())
            if len(texts) >= 2:
                # 素朴な推定: 先頭=相手名、最長=本文。未読数は数字だけのテキストから拾う。
                contact = texts[0]
                snippet = max(texts[1:], key=len)
                unread = 0
                for t in texts:
                    if t.isdigit():
                        unread = int(t)
                        break
                rows.append({"contact": contact, "snippet": snippet, "unread": unread})
    return rows


def dump_line_tree(max_lines=400):  # pragma: no cover - macOS専用
    """診断用(簡易): LINEのAXツリーを role だけ浅く吐く。詳細は dump_line_detail。"""
    create, copy, NSWorkspace = _load_ax()
    pid = _find_line_pid(NSWorkspace)
    if pid is None:
        return "PC版LINEが起動していません。"
    app = create(pid)
    out, n = [], 0
    for role, text, _ in _walk(copy, app):
        line = f"{role}: {repr(text)[:80]}" if text else f"{role}"
        out.append(line)
        n += 1
        if n >= max_lines:
            out.append(f"...(truncated at {max_lines})")
            break
    return "\n".join(out)


def force_accessibility():  # pragma: no cover - macOS専用
    """一部アプリが『支援技術がいる時だけ』a11yツリーに文字を出す挙動を強制的にオンにする。
    AXEnhancedUserInterface / AXManualAccessibility を app 要素に立てる。
    効くアプリと効かないアプリがある(効かなければ画面OCRに切替判断)。
    """
    from ApplicationServices import (
        AXUIElementCreateApplication, AXUIElementSetAttributeValue,
    )
    from AppKit import NSWorkspace
    pid = _find_line_pid(NSWorkspace)
    if pid is None:
        return "PC版LINEが起動していません。"
    app = AXUIElementCreateApplication(pid)
    results = []
    for attr in ("AXEnhancedUserInterface", "AXManualAccessibility"):
        try:
            err = AXUIElementSetAttributeValue(app, attr, True)
            results.append(f"{attr}: {'OK' if err == 0 else f'err={err}'}")
        except Exception as e:
            results.append(f"{attr}: 例外 {e}")
    return " / ".join(results)


def _all_attrs(element):  # pragma: no cover - macOS専用
    """要素の全属性名と値を {name: value} で返す。"""
    from ApplicationServices import (
        AXUIElementCopyAttributeNames, AXUIElementCopyAttributeValue,
    )
    out = {}
    err, names = AXUIElementCopyAttributeNames(element, None)
    if err != 0 or not names:
        return out
    for name in names:
        e2, val = AXUIElementCopyAttributeValue(element, name, None)
        if e2 == 0:
            out[str(name)] = val
    return out


def dump_line_detail(max_lines=1200):  # pragma: no cover - macOS専用
    """診断用(詳細): LINEウィンドウ内の一覧まわりを、階層(インデント)＋全属性つきで吐く。
    メニューバーのノイズは除外。AXRow/AXStaticText/AXCell 等は属性を全部出す。
    """
    create, copy, NSWorkspace = _load_ax()
    pid = _find_line_pid(NSWorkspace)
    if pid is None:
        return "PC版LINEが起動していません。"
    app = create(pid)

    DETAIL_ROLES = {"AXRow", "AXStaticText", "AXCell", "AXColumn",
                    "AXTextField", "AXButton", "AXImage", "AXGroup"}
    SKIP_ROLES = {"AXMenuBar", "AXMenu", "AXMenuItem", "AXMenuBarItem"}
    out, n = [], 0

    def short(v):
        s = repr(v)
        return s[:100]

    def rec(element, depth):
        nonlocal n
        if n >= max_lines or depth > 30:
            return
        role = _ax_value(copy, element, "AXRole") or "?"
        if role in SKIP_ROLES:
            return
        indent = "  " * depth
        base = _ax_value(copy, element, "AXValue") or _ax_value(copy, element, "AXTitle") \
            or _ax_value(copy, element, "AXDescription")
        head = f"{indent}{role}" + (f"  {short(base)}" if base else "")
        out.append(head)
        n += 1
        # 一覧の中身らしい要素は全属性を出す(本文/名前/未読がどこにあるか探す)
        if role in DETAIL_ROLES:
            for k, v in _all_attrs(element).items():
                if k in ("AXChildren", "AXParent", "AXTopLevelUIElement", "AXWindow",
                         "AXRole", "AXFrame", "AXPosition", "AXSize"):
                    continue
                if v is None or v == "":
                    continue
                out.append(f"{indent}    · {k} = {short(v)}")
                n += 1
        children = _ax_value(copy, element, "AXChildren") or []
        for ch in children:
            rec(ch, depth + 1)

    rec(app, 0)
    if n >= max_lines:
        out.append(f"...(truncated at {max_lines})")
    return "\n".join(out)
