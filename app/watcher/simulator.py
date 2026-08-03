"""デモ・開発用シミュレータ。台本のメッセージを順に流し込む。"""
import threading
import time

from .base import BaseWatcher

DEFAULT_SCRIPT = [
    (0,  "S.部長", "今日のゴルフ、スコア散々だったよ笑 108叩いた"),
    (2,  "T.会長", "今夜9時ごろ、3名で寄れそうだが席あるかな"),
    (4,  "K.専務", "今日は暇でさ、家で一人飲みしてるんだけど"),
    (6,  "K.専務", "ウイスキー。でも一人だとつまらんよ、やっぱり"),
    (8,  "Y.社長", "出張から戻りました"),
]


class SimulatorWatcher(BaseWatcher):
    def __init__(self, on_incoming, on_session_lost=None, script=None, speed=1.0):
        super().__init__(on_incoming, on_session_lost)
        self.script = script or DEFAULT_SCRIPT
        self.speed = speed
        self._t = None
        self._stop = threading.Event()

    def start(self):
        def run():
            t0 = time.time()
            for delay, contact, text in self.script:
                wait = t0 + delay * self.speed - time.time()
                if wait > 0 and self._stop.wait(wait):
                    return
                self.on_incoming(contact, text)
        self._t = threading.Thread(target=run, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
