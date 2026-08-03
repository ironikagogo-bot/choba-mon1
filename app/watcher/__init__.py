"""受信ウォッチャー層。

本番: クラウド仮想PC上のPC版LINEを読み取るアダプタ(未実装・技術スパイク対象)。
ここでは差し替え可能なインターフェースと、開発・デモ用のシミュレータのみ提供する。

本番アダプタが満たすべき契約:
  - 新着を検知したら on_incoming(contact_code, text) を呼ぶ
  - 送信機能を一切持たない(構造的に不可能にしておく)
  - セッション切れを検知したら on_session_lost() を呼ぶ(フェイルセーフ通知の起点)
"""
from .base import BaseWatcher
from .simulator import SimulatorWatcher
# MacAccessibilityWatcher は pyobjc に依存するが、import 自体は macOS 以外でも安全
# (pyobjc の読込は実行時=reader呼び出し時に行う)。
from .mac_accessibility import MacAccessibilityWatcher

__all__ = ["BaseWatcher", "SimulatorWatcher", "MacAccessibilityWatcher"]
