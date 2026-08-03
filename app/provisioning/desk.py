"""仮想デスクのライフサイクル管理(②のLINEログインを扱う層)。

ここは Control Plane 側の状態機械とインターフェースのみ。
実際のVM起動・PC版LINE操作・QR取得は DeskProvisioner の実装に委ねる。
本番実装(RealProvisioner)が道4のスパイク対象。ここではモックのみ動く。

重要: このモジュールにも「送信」は存在しない。デスクは読むだけ。
"""
import time
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class DeskState(str, Enum):
    REQUESTED = "requested"
    PROVISIONING = "provisioning"
    AWAITING_LINE_LOGIN = "awaiting_line_login"  # ②QRを本人に提示中
    ACTIVE = "active"
    SESSION_LOST = "session_lost"                # フェイルセーフ発火
    REAUTH_REQUIRED = "reauth_required"
    HIBERNATED = "hibernated"
    FAILED = "failed"


@dataclass
class Desk:
    user_id: str
    desk_id: str = field(default_factory=lambda: secrets.token_urlsafe(10))
    state: DeskState = DeskState.REQUESTED
    line_login_qr: str | None = None    # ②のQR(LINEが発行した内容)。本人に中継する
    last_seen_ts: float = 0.0
    created_ts: float = field(default_factory=time.time)
    _probe_detail: str = ""             # 道A: 疎通確認の結果メッセージ

    def to_dict(self):
        d = self.__dict__.copy()
        d["state"] = self.state.value
        return d


class DeskProvisioner(ABC):
    """VM/コンテナ + PC版LINE を1ユーザー1台で用意する契約。"""

    @abstractmethod
    def provision(self, user_id: str) -> Desk:
        """VMを起動し、PC版LINEを立ち上げ、②ログインQRを取得して
        state=AWAITING_LINE_LOGIN の Desk を返す。"""

    @abstractmethod
    def poll_login(self, desk: Desk) -> DeskState:
        """本人が②QRを承認したか確認。承認済みなら ACTIVE。"""

    @abstractmethod
    def health(self, desk: Desk) -> DeskState:
        """セッション生存確認。切れていたら SESSION_LOST。"""

    @abstractmethod
    def destroy(self, desk: Desk) -> None:
        """退会・停止時に環境を破棄(データ消去)。"""


class MockProvisioner(DeskProvisioner):
    """デモ用。QR取得やログインを即成功させる張りぼて。"""

    def provision(self, user_id: str) -> Desk:
        d = Desk(user_id=user_id, state=DeskState.AWAITING_LINE_LOGIN)
        d.line_login_qr = "MOCK-LINE-QR:" + secrets.token_urlsafe(8)
        return d

    def poll_login(self, desk: Desk) -> DeskState:
        desk.state = DeskState.ACTIVE
        desk.last_seen_ts = time.time()
        return desk.state

    def health(self, desk: Desk) -> DeskState:
        return DeskState.ACTIVE

    def destroy(self, desk: Desk) -> None:
        desk.state = DeskState.FAILED  # 破棄済み扱い


# --- 道A: ローカルMac実装 ---
# クラウドVMではなく「本人のMac上で、既にログイン済みのPC版LINEを読む」構成。
# LINEログイン(QR)は本人が普段どおりMacのLINEにログインしている前提なので、
# ここでの provision は「LINEが起動していて、アクセシビリティで読めるか」の確認に相当する。
class LocalMacProvisioner(DeskProvisioner):
    """本人のMac上の PC版LINE を前提にしたプロビジョナ(道A)。

    provision: LINEプロセスの起動確認 + アクセシビリティ読取の疎通確認。
      読める → ACTIVE 手前(AWAITING_LINE_LOGIN=「開始の確認待ち」として再利用)。
      読めない → FAILED(理由をログに)。
    poll_login: 本人が「監視開始」に同意 → ACTIVE。
    health: 直近で読めているか(DeskServiceが実測フラグを更新)。
    destroy: 監視停止のみ(ローカルなのでVM破棄は無い)。
    """

    def provision(self, user_id: str) -> Desk:
        d = Desk(user_id=user_id, state=DeskState.AWAITING_LINE_LOGIN)
        ok, detail = self._probe()
        d.line_login_qr = None
        d._probe_detail = detail  # DeskServiceがログに出す
        if not ok:
            d.state = DeskState.FAILED
        return d

    def poll_login(self, desk: Desk) -> DeskState:
        desk.state = DeskState.ACTIVE
        desk.last_seen_ts = time.time()
        return desk.state

    def health(self, desk: Desk) -> DeskState:
        return desk.state

    def destroy(self, desk: Desk) -> None:
        desk.state = DeskState.FAILED

    @staticmethod
    def _probe():
        """LINEが起動していてアクセシビリティで読めるかを軽く確認する。"""
        try:
            from ..watcher.mac_accessibility import read_line_chat_list, SessionLostError
        except Exception as e:
            return False, f"読み取りモジュールの読込に失敗: {e}"
        try:
            rows = read_line_chat_list()
            return True, f"LINEを検出。トーク一覧 {len(rows)} 行を読めました。"
        except SessionLostError as e:
            return False, str(e)
        except Exception as e:
            return False, f"読み取り時にエラー: {e}"


# クラウドVM版(フェーズ2で中身を作る。当面は道Aのローカル実装を先に検証する)
class RealProvisioner(DeskProvisioner):
    """クラウド隔離VM上で PC版LINE を動かす本番構成(将来)。"""

    def provision(self, user_id: str) -> Desk:
        raise NotImplementedError("クラウドVM版は将来実装。まずは LocalMacProvisioner(道A)で検証")

    def poll_login(self, desk: Desk):
        raise NotImplementedError

    def health(self, desk: Desk):
        raise NotImplementedError

    def destroy(self, desk: Desk):
        raise NotImplementedError
