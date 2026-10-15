"""Image-only recognition shared by the app and screenshot regression checks."""
import vision

def layout(image,factor=1):
    logical=image.resize((round(image.width/factor),round(image.height/factor))) if factor!=1 else image
    try:split=vision.divider(logical)
    except ValueError:split=0
    top=vision.input_top(logical,split)
    return logical,split,top

async def analyze(image,hwnd,rect,recognize,factor=1,crop=None):
    logical,split,top=layout(image,factor)
    _,signature=vision.header_image(logical,split)
    w,h=logical.size
    header=image.crop((round((split+12)*factor),round(8*factor),round(max(split+60,w-145)*factor),round(min(65,h)*factor)))
    result=await recognize(vision.prepare_ocr(header))
    title=''.join(result.text.split())
    if not vision.valid_title(title):title=''
    detected=vision.bubbles(logical,split,top)
    messages=[]
    for body in detected:
        x0,y0,x1,y1=[round(v*factor) for v in body.box]
        if body.side=='out':
            messages.append(vision.Bubble('out',(x0,y0,x1,y1)))
            continue
        if crop:
            x,y,cw,ch=crop
            # User crop narrows the eligible incoming bubbles; it never bypasses
            # direction detection or hides outgoing markers from sequence tracking.
            if not (x<=(x0+x1)/2<=x+cw and y<=(y0+y1)/2<=y+ch):continue
        # Trim the rounded boundary while retaining the text's inner padding.
        inset=max(2,round(3*factor))
        shot=image.crop((x0+inset,y0+inset,x1-inset,y1-inset))
        result=await recognize(vision.prepare_ocr(shot))
        text=vision.clean_text(result.text)
        if not text:continue
        if sum(c.isalnum() for c in text)<max(1,len(text)*.35):continue
        messages.append(vision.Bubble('in',(x0,y0,x1,y1),text))
    return vision.View(hwnd,rect,title,vision.message_tail(messages),None,[],signature,tuple(messages))
