"""Screen-only WeChat layout recognition. No UIA, database or WeChat API."""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import re

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

def parse(image,lines,hwnd,rect):
    split=divider(image); top=input_top(image,split); w,h=image.size
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
