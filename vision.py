"""Screen-only WeChat layout recognition. No UIA, database or WeChat API."""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import re

def enable_dpi_awareness():
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError,OSError):
        ctypes.windll.user32.SetProcessDPIAware()

def list_windows():
    user=ctypes.windll.user32; kernel=ctypes.windll.kernel32
    user.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user.GetClassNameW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes=[wintypes.HWND]
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    windows=[]
    callback=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    def visit(hwnd,_):
        if not user.IsWindowVisible(hwnd):return True
        name=ctypes.create_unicode_buffer(512); kind=ctypes.create_unicode_buffer(512)
        user.GetWindowTextW(hwnd,name,512); user.GetClassNameW(hwnd,kind,512)
        pid=wintypes.DWORD(); user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        handle=kernel.OpenProcess(0x1000,False,pid.value)
        executable=''
        if handle:
            try:
                path=ctypes.create_unicode_buffer(4096); size=wintypes.DWORD(4096)
                if kernel.QueryFullProcessImageNameW(handle,0,path,ctypes.byref(size)):
                    executable=path.value.rsplit('\\',1)[-1].lower()
            finally:kernel.CloseHandle(handle)
        if executable in ('wechat.exe','weixin.exe') or kind.value=='WeChatMainWndForPC':
            if name.value:windows.append((hwnd,pid.value,name.value,kind.value))
        return True
    user.EnumWindows(callback(visit),0)
    return windows

def validate_window(hwnd,pid):
    user=ctypes.windll.user32
    user.IsWindow.argtypes=[wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    actual=wintypes.DWORD(); user.GetWindowThreadProcessId(hwnd,ctypes.byref(actual))
    if not user.IsWindow(hwnd) or actual.value!=pid:raise ValueError('锁定的窗口已关闭，请重新选择窗口')

def ensure_point(hwnd,point):
    user=ctypes.windll.user32
    user.WindowFromPoint.argtypes=[wintypes.POINT]; user.WindowFromPoint.restype=wintypes.HWND
    user.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]; user.GetAncestor.restype=wintypes.HWND
    target=user.WindowFromPoint(wintypes.POINT(int(point[0]),int(point[1])))
    if user.GetAncestor(target,2)!=hwnd:raise ValueError('发送位置被其他窗口遮挡，已取消操作')

def client_rect(hwnd):
    user=ctypes.windll.user32
    user.GetClientRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
    user.ClientToScreen.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.POINT)]
    bounds=wintypes.RECT(); point=wintypes.POINT(0,0)
    if not user.GetClientRect(hwnd,ctypes.byref(bounds)) or not user.ClientToScreen(hwnd,ctypes.byref(point)):
        raise ValueError('无法获取指定窗口')
    w,h=bounds.right,bounds.bottom
    if w<400 or h<350:raise ValueError('指定窗口太小，请扩大窗口')
    return point.x,point.y,w,h

def capture_window(hwnd,pid):
    """PrintWindow captures only the locked client, not overlapping desktop pixels."""
    from PIL import Image
    validate_window(hwnd,pid)
    user=ctypes.windll.user32; gdi=ctypes.windll.gdi32
    user.GetDC.argtypes=[wintypes.HWND]; user.GetDC.restype=wintypes.HDC
    user.ReleaseDC.argtypes=[wintypes.HWND,wintypes.HDC]
    user.PrintWindow.argtypes=[wintypes.HWND,wintypes.HDC,wintypes.UINT]
    gdi.CreateCompatibleDC.argtypes=[wintypes.HDC]; gdi.CreateCompatibleDC.restype=wintypes.HDC
    gdi.CreateCompatibleBitmap.argtypes=[wintypes.HDC,ctypes.c_int,ctypes.c_int]; gdi.CreateCompatibleBitmap.restype=wintypes.HBITMAP
    gdi.SelectObject.argtypes=[wintypes.HDC,wintypes.HANDLE]; gdi.SelectObject.restype=wintypes.HANDLE
    gdi.GetDIBits.argtypes=[wintypes.HDC,wintypes.HBITMAP,wintypes.UINT,wintypes.UINT,ctypes.c_void_p,ctypes.c_void_p,wintypes.UINT]
    gdi.DeleteObject.argtypes=[wintypes.HANDLE]; gdi.DeleteDC.argtypes=[wintypes.HDC]
    rect=client_rect(hwnd); x,y,w,h=rect
    dc=user.GetDC(hwnd); memory=gdi.CreateCompatibleDC(dc); bitmap=gdi.CreateCompatibleBitmap(dc,w,h)
    old=gdi.SelectObject(memory,bitmap)
    class Header(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('width',wintypes.LONG),('height',wintypes.LONG),('planes',wintypes.WORD),('bits',wintypes.WORD),('compression',wintypes.DWORD),('image_size',wintypes.DWORD),('xppm',wintypes.LONG),('yppm',wintypes.LONG),('used',wintypes.DWORD),('important',wintypes.DWORD)]
    try:
        ok=user.PrintWindow(hwnd,memory,3)
        gdi.SelectObject(memory,old)
        info=Header(40,w,-h,1,32,0,0,0,0,0,0); data=ctypes.create_string_buffer(w*h*4)
        rows=gdi.GetDIBits(memory,bitmap,0,h,data,ctypes.byref(info),0) if ok else 0
        if rows==h:
            image=Image.frombytes('RGB',(w,h),data.raw,'raw','BGRX')
            from PIL import ImageStat
            if max(ImageStat.Stat(image).stddev)>3:return image,rect
    finally:
        gdi.SelectObject(memory,old); gdi.DeleteObject(bitmap); gdi.DeleteDC(memory); user.ReleaseDC(hwnd,dc)
    # GPU-rendered windows may not implement PrintWindow. Never capture an obscuring app.
    if not foreground(hwnd):raise ValueError('此窗口不支持后台截图，请将所选微信窗口置于前台')
    for point in ((x+10,y+10),(x+w-10,y+10),(x+10,y+h-10),(x+w-10,y+h-10),(x+w//2,y+h//2)):
        ensure_point(hwnd,point)
    import pyautogui
    return pyautogui.screenshot(region=rect),rect

def confirmed_point(rect,saved):
    if not saved or list(rect[2:])!=saved['size']:raise ValueError('请在窗口预览中设置输入框位置；窗口缩放后需重新确认')
    x,y,w,h=rect; px,py=saved['point']
    if not 0<px<w or not 0<py<h:raise ValueError('发送位置超出所选窗口')
    return x+px,y+py

@dataclass(frozen=True)
class Text:
    text: str
    x: float
    y: float
    width: float
    height: float

@dataclass
class View:
    hwnd: int
    rect: tuple
    title: str
    message: str
    input_point: tuple
    unread: list

def find_wechat():
    user=ctypes.windll.user32
    user.GetForegroundWindow.restype=wintypes.HWND
    user.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user.GetClassNameW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user.IsWindowVisible.argtypes=[wintypes.HWND]
    found=[]
    callback=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    def visit(hwnd,_):
        name=ctypes.create_unicode_buffer(512); kind=ctypes.create_unicode_buffer(512)
        user.GetWindowTextW(hwnd,name,512); user.GetClassNameW(hwnd,kind,512)
        if user.IsWindowVisible(hwnd) and (kind.value=='WeChatMainWndForPC' or name.value.strip() in ('微信','WeChat','Weixin')):
            found.append(hwnd)
        return True
    user.EnumWindows(callback(visit),0)
    if len(found)!=1:raise ValueError('未找到唯一微信主窗口，请打开并登录一个微信客户端')
    return found[0]

def activate(hwnd):
    user=ctypes.windll.user32
    user.ShowWindow.argtypes=[wintypes.HWND,ctypes.c_int]
    user.SetForegroundWindow.argtypes=[wintypes.HWND]
    user.IsIconic.argtypes=[wintypes.HWND]
    if user.IsIconic(hwnd):user.ShowWindow(hwnd,9)
    user.SetForegroundWindow(hwnd)

def window_rect(hwnd):
    user=ctypes.windll.user32
    user.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
    bounds=wintypes.RECT()
    if not user.GetWindowRect(hwnd,ctypes.byref(bounds)):raise ValueError('微信窗口已关闭')
    x,y,w,h=bounds.left,bounds.top,bounds.right-bounds.left,bounds.bottom-bounds.top
    if x<0 or y<0 or w<650 or h<450:raise ValueError('微信窗口太小或位于屏幕边缘，请完整显示微信窗口')
    return x,y,w,h

def foreground(hwnd):
    user=ctypes.windll.user32
    user.GetForegroundWindow.restype=wintypes.HWND
    return user.GetForegroundWindow()==hwnd

def flatten(result):
    lines=[]
    for line in result.lines:
        words=list(line.words)
        if not words:continue
        rects=[word.bounding_rect for word in words]
        left=min(r.x for r in rects); top=min(r.y for r in rects)
        right=max(r.x+r.width for r in rects); bottom=max(r.y+r.height for r in rects)
        lines.append(Text(line.text,left,top,right-left,bottom-top))
    return lines

def divider(image):
    # Find a long, thin vertical border separating conversation list and chat.
    im=image.convert('RGB'); w,h=im.size; pixels=im.load(); candidates=[]
    for x in range(int(w*.20),int(w*.48)):
        score=0
        for y in range(100,h-100,5):
            center=pixels[x,y]; left=pixels[x-3,y]; right=pixels[x+3,y]
            diff=sum(abs(left[i]-right[i]) for i in range(3))
            edge=min(sum(abs(center[i]-left[i]) for i in range(3)),sum(abs(center[i]-right[i]) for i in range(3)))
            if diff>=9 or edge>=9:score+=1
        candidates.append((score,x))
    score,x=max(candidates)
    if score<max(15,(h-200)/5*.50):raise ValueError('未识别到聊天列表分隔线，请显示微信主聊天界面')
    return x

def input_top(image,split):
    im=image.convert('RGB'); w,h=im.size; p=im.load(); scores=[]
    for y in range(int(h*.52),int(h*.88)):
        hits=0; total=0
        for x in range(split+35,w-30,8):
            total+=1
            if sum(abs(p[x,y-3][i]-p[x,y+3][i]) for i in range(3))>=9 or min(sum(abs(p[x,y][i]-p[x,y-3][i]) for i in range(3)),sum(abs(p[x,y][i]-p[x,y+3][i]) for i in range(3)))>=9:hits+=1
        if total and hits/total>.70:scores.append((hits/total,y))
    if not scores:raise ValueError('未识别到输入框分隔线，暂不发送')
    return max(scores)[1]

def unread_badges(image,split):
    # Red badge components in the conversation-list avatar/name area.
    im=image.convert('RGB'); w,h=im.size; scale=3; small=im.resize((w//scale,h//scale)); p=small.load()
    points=set()
    for y in range(28,(h-35)//scale):
        for x in range(20,max(21,split//scale-4)):
            r,g,b=p[x,y]
            if r>175 and r>g*1.45 and r>b*1.45:points.add((x,y))
    rows=[]
    while points:
        seed=points.pop(); stack=[seed]; component=[seed]
        while stack:
            x,y=stack.pop()
            for other in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                if other in points:points.remove(other); stack.append(other); component.append(other)
        if not 3<=len(component)<=220:continue
        xs=[a for a,b in component]; ys=[b for a,b in component]
        if max(xs)-min(xs)>20 or max(ys)-min(ys)>20:continue
        row=int(sum(ys)/len(ys)*scale)
        if all(abs(row-old)>25 for old in rows):rows.append(row)
    return sorted(rows)

def parse(image,lines,hwnd,rect,split_override=None,input_override=None):
    split=divider(image) if split_override is None else split_override
    top=input_top(image,split) if input_override is None else input_override
    w,h=image.size
    titles=[t for t in lines if t.x>split+12 and t.y<95 and t.y>12 and t.text.replace(' ','') not in ('微信','WeChat','Weixin','搜索')]
    if not titles:raise ValueError('未识别到聊天标题，请打开一个聊天')
    title=min(titles,key=lambda t:(t.y,t.x)).text
    incoming=[]
    for line in sorted(lines,key=lambda t:(t.y,t.x)):
        compact=''.join(line.text.split())
        if not split+35<line.x<split+(w-split)*.48 or not 100<line.y<top-15:continue
        if re.fullmatch(r'[\d:年月日/\-\s]+',compact) or compact in ('查看更多消息','以下为新消息'):continue
        # Reject right-aligned timestamps and text printed inside a green outgoing bubble.
        px=image.convert('RGB').getpixel((min(w-1,int(line.x+2)),min(h-1,int(line.y+line.height/2))))
        if px[1]>px[0]*1.12 and px[1]>px[2]*1.12:continue
        incoming.append(line.text)
    x,y,_,_=rect
    return View(hwnd,rect,title,'\n'.join(incoming),(x+split+70,y+top+65),[(x+int(split*.6),y+row) for row in unread_badges(image,split)])
