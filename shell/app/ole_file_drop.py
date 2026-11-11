"""Real Windows OLE file drag from a Qt widget's actual mouse-move handler."""
import ctypes as C
from ctypes import wintypes as W
import time
import threading
from PySide6.QtCore import QMimeData,QUrl,Qt,QTimer
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QApplication,QLabel

_APP=None

def drag_file(hwnd,path):
    global _APP
    _APP=QApplication.instance() or QApplication([])
    app=_APP
    started=[False]; result=[None]; step=[0]; reached=[False]; released=[False]; timed_out=[False]
    class Source(QLabel):
        def mouseMoveEvent(self,event):
            if event.buttons() & Qt.MouseButton.LeftButton and not started[0]:
                started[0]=True
                mime=QMimeData(); mime.setUrls([QUrl.fromLocalFile(str(path))])
                drag=QDrag(self); drag.setMimeData(mime)
                result[0]=drag.exec(Qt.DropAction.CopyAction).value
            super().mouseMoveEvent(event)
    source=Source('P3 drag fixture'); source.setWindowFlags(Qt.WindowType.Tool|Qt.WindowType.FramelessWindowHint)
    source.resize(160,40); source.move(20,20); source.show(); source.activateWindow(); source.raise_(); app.processEvents()
    user=C.WinDLL('user32'); user.GetWindowRect.argtypes=[W.HWND,C.POINTER(W.RECT)]
    rect=W.RECT(); source_rect=W.RECT()
    assert user.GetWindowRect(hwnd,C.byref(rect)); assert user.GetWindowRect(int(source.winId()),C.byref(source_rect))
    sx=(source_rect.left+source_rect.right)//2; sy=(source_rect.top+source_rect.bottom)//2
    assert user.SetCursorPos(sx,sy); user.mouse_event(2,0,0,0,0)
    for _ in range(5): time.sleep(.05); app.processEvents()
    assert app.mouseButtons() & Qt.MouseButton.LeftButton, 'source did not receive native press'
    def move():
        step[0]+=1
        if step[0]==1: user.SetCursorPos(sx+10,sy+5)
        elif 2<=step[0]<=6:
            progress=(step[0]-1)/5
            user.SetCursorPos(int(sx+(rect.left+450-sx)*progress),int(sy+(rect.top+250-sy)*progress))
            if step[0]==6: reached[0]=True
        elif not released[0]: user.mouse_event(4,0,0,0,0); released[0]=True
    def drive():
        for _ in range(7): time.sleep(.1); move()
    mover=threading.Thread(target=drive); mover.start()
    timeout=QTimer(); timeout.setSingleShot(True)
    def cancel(): timed_out[0]=True; QDrag.cancel()
    timeout.timeout.connect(cancel); timeout.start(4000)
    try:
        end=time.monotonic()+5
        while result[0] is None and time.monotonic()<end: app.processEvents();time.sleep(.02)
        assert started[0] and reached[0] and released[0] and not timed_out[0], f'OLE drag failed: started={started[0]}, step={step[0]}, release={released[0]}, timeout={timed_out[0]}, effect={result[0]}'
        return {'format':'CF_HDROP','platform':'Qt Windows OLE','effect':result[0],'target_reached':True,'released':True}
    finally:
        mover.join(timeout=2);timeout.stop();user.mouse_event(4,0,0,0,0);source.close();app.processEvents()
