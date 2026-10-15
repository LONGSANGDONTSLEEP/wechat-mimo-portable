import unittest
from PIL import Image,ImageDraw
import vision

def body(text,side='in',y=100):return vision.Bubble(side,(200,y,500,y+35),text)

class MessageTests(unittest.TestCase):
    def test_wide_green_on_left_is_outgoing_even_in_dark_theme(self):
        for bg,fill in [((30,30,31),(47,47,48)),((245,245,245),(255,255,255))]:
            im=Image.new('RGB',(900,700),bg); draw=ImageDraw.Draw(im)
            draw.rounded_rectangle((270,120,385,156),8,fill=fill)
            draw.rounded_rectangle((310,200,820,255),8,fill=(53,210,141))
            found=vision.bubbles(im,200,480)
            self.assertEqual([b.side for b in found],['in','out'])
            self.assertLess(found[-1].box[0],im.width/2)

    def test_own_reply_never_creates_new_input(self):
        old=[body('你好')]; tracker=vision.MessageTracker(old)
        self.assertEqual(tracker.observe(old+[body('',side='out',y=160)]),'')
        self.assertEqual(vision.message_tail(old+[body('',side='out',y=160)]),'')

    def test_only_new_incoming_and_repeated_short_text_can_reply(self):
        seq=[body('你好'),body('',side='out')]; tracker=vision.MessageTracker(seq)
        seq=seq+[body('你好')]
        self.assertEqual(tracker.observe(seq),'你好')
        self.assertEqual(tracker.observe(seq),'')
        seq=seq+[body('',side='out')]; self.assertEqual(tracker.observe(seq),'')
        self.assertEqual(tracker.observe(seq+[body('你好')]),'你好')

    def test_scroll_and_ocr_wobble_reset_without_reply(self):
        tracker=vision.MessageTracker([body('第一句'),body('第二句')])
        self.assertEqual(tracker.observe([body('以前的旧记录')]),'')
        self.assertEqual(tracker.observe([body('以前的旧纪录')]),'')

    def test_header_noise_is_not_a_new_title(self):
        self.assertFalse(vision.valid_title('0'))
        self.assertFalse(vision.valid_title('...'))
        self.assertTrue(vision.valid_title('AI智能体'))
        self.assertEqual(vision.clean_text('智 能 体，可以 聊 天 吗 ？'),'智能体，可以聊天吗？')

    def test_header_visual_comparison(self):
        from PIL import ImageFont
        im=Image.new('RGB',(900,700),(30,30,31)); draw=ImageDraw.Draw(im)
        font=ImageFont.truetype('msyh.ttc',18)
        draw.text((220,20),'AI智能体',font=font,fill='white')
        _,first=vision.header_image(im,200)
        _,same=vision.header_image(im.copy(),200)
        draw.rectangle((210,8,600,65),fill=(30,30,31)); draw.text((220,20),'另外一个聊天',font=font,fill='white')
        _,different=vision.header_image(im,200)
        self.assertTrue(vision.header_same(first,same))
        self.assertFalse(vision.header_same(first,different))

if __name__=='__main__':unittest.main()
