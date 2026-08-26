"""單一實例：讓第二次雙擊 .md 開在既有視窗的新分頁。

流程：
  1. 新行程啟動時先試著連上具名管道。
  2. 連得上 -> 已經有實例在跑，把檔案路徑送過去，然後自己結束（不開視窗）。
  3. 連不上 -> 自己就是第一個實例，改成監聽端，之後負責接收別人送來的路徑。

【為什麼不會拖慢啟動】
連不上時 Windows 的具名管道會立刻回報失敗，不會等到逾時，因此第一個實例
幾乎不需要額外時間（實測 0.5 ms 以內）。

【不要用 waitForBytesWritten】
Windows 的 QLocalSocket 上，flush() 不保證清空待寫緩衝區，而 bytesWritten
訊號也不會在這種小量寫入後可靠地發出——waitForBytesWritten 因此會一路空等到
逾時。改成交給 disconnectFromServer()：它會先把待寫資料送完（期間狀態是
ClosingState），送完才真正關閉，waitForDisconnected() 等的就是這個完成點。

【訊息以換行結尾】
收方靠換行判斷「整條路徑都到齊了」，不必猜一次 readyRead 是否剛好帶來全部
內容。路徑本身不可能含有換行，所以拿它當結束符是安全的。

【殘留的管道】
Windows 的具名管道會隨行程消失，所以強制結束不會留下垃圾。真正需要清理的是
Unix 的 socket 檔案，listen() 的重試是為了那個平台而寫，在 Windows 上不會走到。
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

from . import config

_ENCODING = "utf-8"
_TERMINATOR = b"\n"


def send_to_existing(path: str | None) -> bool:
    """試著把路徑交給既有實例。成功回傳 True，代表本行程可以直接結束。"""
    socket = QLocalSocket()
    socket.connectToServer(config.IPC_SERVER_NAME)
    if not socket.waitForConnected(config.IPC_CONNECT_TIMEOUT_MS):
        return False

    # 沒有檔案時送空字串，對方只要把視窗叫到前景就好
    socket.write((path or "").encode(_ENCODING) + _TERMINATOR)
    # flush() 不保證清空緩衝區，但會立刻試著把資料推進管道，讓對方能在我們
    # 還沒關閉前就收到——少了它，收方有機會拿到一個空連線。
    socket.flush()

    # disconnectFromServer() 會先送完待寫資料再關閉，等它完成即可；
    # 對方讀完也會立刻關自己那端，所以正常情況下這裡只花幾毫秒。
    socket.disconnectFromServer()
    if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        socket.waitForDisconnected(config.IPC_DISCONNECT_TIMEOUT_MS)

    # 等不到對方關閉時，要分清楚是哪一種：
    #   bytesToWrite() == 0 → 資料已經在管道裡，對方只是暫時忙（正在跳對話框、
    #     正在渲染大檔），等它有空就會讀到，我們照樣安心結束。
    #   bytesToWrite() > 0  → 資料根本沒送出去，這時若回報成功，使用者雙擊的
    #     檔案就會無聲無息地消失。回報失敗，讓本行程自己開一個視窗。
    return socket.bytesToWrite() == 0


class InstanceServer(QObject):
    """監聽端。收到別的行程送來的路徑時發出 pathReceived。"""

    pathReceived = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)

    def listen(self) -> bool:
        if self._server.listen(config.IPC_SERVER_NAME):
            return True
        # 只有 Unix 會因為殘留的 socket 檔案而失敗，清掉再試一次。
        # Windows 反而是「同名可以多重監聽」，第一次就會成功。
        QLocalServer.removeServer(config.IPC_SERVER_NAME)
        return self._server.listen(config.IPC_SERVER_NAME)

    def close(self) -> None:
        """停止監聽。可以重複呼叫，也允許 C++ 物件已經先被銷毀。"""
        try:
            self._server.close()
        except RuntimeError:
            # 呼叫端把這個物件掛錯了 parent、C++ 端已先一步被銷毀。
            # 這是呼叫端的問題（見 main.py 的註解），但關閉流程不該因此中斷。
            pass
        QLocalServer.removeServer(config.IPC_SERVER_NAME)

    def _on_connection(self) -> None:
        """讀取對方送來的路徑。

        送方寫完就馬上要求關閉，因此「資料到達」和「連線中斷」哪個先發生並不
        固定，甚至可能在這個函式執行前就都發生完了。三個入口都接上，用旗標
        確保只送出一次結果。
        """
        socket = self._server.nextPendingConnection()
        if socket is None:
            return

        buffer = bytearray()
        state = {"done": False}

        def drain() -> None:
            buffer.extend(bytes(socket.readAll()))

        def deliver() -> None:
            if state["done"]:
                return
            state["done"] = True
            # 讀完立刻關閉，送方的 waitForDisconnected 才會馬上返回而不是空等。
            socket.disconnectFromServer()
            socket.deleteLater()
            text = bytes(buffer).decode(_ENCODING, errors="replace").strip()
            # 【不要在 socket 的訊號裡直接做重活】
            # readyRead 是從 Qt 的管道讀取回呼（QWindowsPipeReader）發出來的。
            # 在這個呼叫框架裡直接 emit -> 開分頁 -> 渲染大檔要花數百毫秒，
            # 期間其他排隊連線的管道回呼會與上面正在拆除的 socket 狀態交錯，
            # 實測三條連線背靠背時必定以 0xC0000005 崩潰在原生層
            # （faulthandler 停在 on_ready）。用 singleShot(0) 把工作推回
            # 事件迴圈，讓 socket 的回呼框架先乾淨退場再處理路徑。
            QTimer.singleShot(0, lambda: self.pathReceived.emit(text))

        def on_ready() -> None:
            drain()
            if _TERMINATOR in buffer:
                deliver()

        def on_disconnected() -> None:
            # 對方關了就不會再有資料，手上有多少就用多少
            drain()
            deliver()

        socket.readyRead.connect(on_ready)
        socket.disconnected.connect(on_disconnected)

        if socket.bytesAvailable():
            on_ready()
        elif socket.state() == QLocalSocket.LocalSocketState.UnconnectedState:
            # 對方已經關了，但管道裡的內容可能還沒被拉進 Qt 的讀取緩衝區。
            # 少了這一步就會讀到空字串——「分頁開出來卻是空白」就是這樣來的。
            socket.waitForReadyRead(config.IPC_READ_TIMEOUT_MS)
            on_disconnected()
