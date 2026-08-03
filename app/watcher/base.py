"""ウォッチャー基底クラス(契約の明文化)。"""
from abc import ABC, abstractmethod
from typing import Callable


class BaseWatcher(ABC):
    """受信の見張り役。読み取り専用 — 送信APIは定義しない(意図的)。"""

    def __init__(self, on_incoming: Callable[[str, str], None],
                 on_session_lost: Callable[[], None] | None = None):
        self.on_incoming = on_incoming
        self.on_session_lost = on_session_lost or (lambda: None)

    @abstractmethod
    def start(self):
        ...

    @abstractmethod
    def stop(self):
        ...
