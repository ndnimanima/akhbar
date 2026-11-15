"""Regenerate the README's animated illustrations.

Requires Pillow and DejaVu Sans fonts (or set DEJAVU_DIR to another font directory).
The avatar crops are from the team's supplied README screenshot. The workflow's
background is an AI-generated illustration. Outputs are local, GitHub-safe GIFs;
no JavaScript, external image service, or SVG animation is required.
Run: python docs/create_readme_art.py
"""
from pathlib import Path
import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent / 'images'
FONT_DIR = Path(os.environ.get('DEJAVU_DIR', '/usr/share/fonts/truetype/dejavu'))
REGULAR = FONT_DIR / 'DejaVuSans.ttf'
BOLD = FONT_DIR / 'DejaVuSans-Bold.ttf'
if not (REGULAR.exists() and BOLD.exists()):
    raise SystemExit('Set DEJAVU_DIR to a directory containing DejaVuSans.ttf and DejaVuSans-Bold.ttf')

def font(size, bold=False):
    return ImageFont.truetype(str(BOLD if bold else REGULAR), size)

def centred(draw, xy, text, ft, fill):
    draw.text(xy, text, font=ft, fill=fill, anchor='mm')

def rgba(col, a=255):
    return (*col, a)

CYAN = (91, 229, 231)
PURPLE = (181, 145, 255)
GOLD = (255, 202, 137)
WHITE = (237, 246, 255)
MUTED = (166, 188, 214)


def make_workflow():
    w, h = 1200, 350
    backdrop = ImageOps.fit(Image.open(ROOT / 'readme-divider.png').convert('RGB'), (w, h), centering=(.5, .5)).convert('RGBA')
    backdrop = Image.alpha_composite(backdrop, Image.new('RGBA', (w, h), (5, 12, 28, 185))).convert('RGB')
    d = ImageDraw.Draw(backdrop, 'RGBA')
    d.rounded_rectangle((2, 2, w-3, h-3), radius=25, outline=(59, 92, 126, 210), width=2)
    d.text((42, 31), 'THE ROZANEH FLOW', font=font(14, True), fill=rgba(CYAN))
    d.text((42, 56), 'From signals to stories', font=font(29, True), fill=rgba(WHITE))
    d.text((42, 98), 'One clear path from your sources to your audience.', font=font(15), fill=rgba(MUTED))
    d.text((42, 305), 'BUILT FOR YOUR NEWSROOM', font=font(12, True), fill=(132, 159, 189, 255))
    d.text((1158, 305), '01  /  05', font=font(12, True), fill=(132, 159, 189, 255), anchor='ra')
    steps = [
        ('01', 'COLLECT', 'RSS / Atom / HTML', CYAN),
        ('02', 'FILTER', 'Time window', (114, 191, 255)),
        ('03', 'CLASSIFY', 'Topic matching', PURPLE),
        ('04', 'TRANSLATE', 'Optional', (233, 151, 235)),
        ('05', 'PUBLISH', 'Messages + archive', GOLD),
    ]
    starts = [42, 271, 500, 729, 958]
    frames = []
    for frame in range(40):
        img = backdrop.copy()
        d = ImageDraw.Draw(img, 'RGBA')
        active = (frame // 8) % 5
        phase = (frame % 8) / 8
        for i, ((number, label, desc, accent), x) in enumerate(zip(steps, starts)):
            is_active = i == active
            d.rounded_rectangle((x, 155, x+200, 273), radius=15, fill=(14, 27, 48, 235), outline=rgba(accent, 220 if is_active else 68), width=2)
            d.rounded_rectangle((x+14, 169, x+49, 204), radius=10, fill=rgba(accent, 42), outline=rgba(accent, 145 if is_active else 70))
            centred(d, (x+31, 186), number, font(12, True), rgba(accent))
            d.text((x+14, 217), label, font=font(14, True), fill=rgba(WHITE))
            d.text((x+14, 244), desc, font=font(11), fill=rgba(MUTED))
            if is_active:
                d.rounded_rectangle((x+14, 262, x+186, 264), radius=1, fill=rgba(accent, 80))
                d.rounded_rectangle((x+14, 262, x+14+int(172*phase), 264), radius=1, fill=rgba(accent, 255))
            if i < 4:
                d.line((x+202, 214, x+226, 214), fill=(117, 156, 189, 150), width=2)
                if i == active:
                    dotx = x+202+round(24*phase)
                    d.ellipse((dotx-3,211,dotx+3,217), fill=rgba(accent))
        frames.append(img)
    frames[0].save(ROOT / 'workflow-still.png', optimize=True)
    palette = frames[0].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    frames = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in frames]
    frames[0].save(ROOT / 'workflow.gif', save_all=True, append_images=frames[1:], duration=120, loop=0, optimize=True, disposal=1)


def make_creator(handle, name, role, accent, index):
    w, h = 300, 350
    base = Image.new('RGB', (w, h), '#0a1326')
    d = ImageDraw.Draw(base, 'RGBA')
    for y in range(h):
        d.line((0,y,w,y), fill=(12+int(y/h*4), 24+int(y/h*6), 44+int(y/h*8), 255))
    d.rounded_rectangle((2, 2, w-3, h-3), radius=22, outline=(80, 112, 145, 135), width=2)
    d.text((22, 21), 'ROZANEH  /  TEAM', font=font(10, True), fill=rgba(accent))
    d.text((277, 21), '0'+str(index), font=font(10, True), fill=(132, 159, 189, 255), anchor='ra')
    # Crop and circle-mask the portrait supplied by the user.
    portrait = ImageOps.fit(Image.open(ROOT / 'avatars' / (handle+'.png')).convert('RGB'), (142,142), method=Image.Resampling.LANCZOS)
    mask = Image.new('L', (142,142), 0)
    ImageDraw.Draw(mask).ellipse((0,0,141,141), fill=255)
    base.paste(portrait, (79, 74), mask)
    d = ImageDraw.Draw(base, 'RGBA')
    centred(d, (150, 256), name, font(18, True), rgba(WHITE))
    centred(d, (150, 282), role, font(11), rgba(MUTED))
    d.rounded_rectangle((75, 302, 225, 330), radius=14, fill=rgba(accent, 24), outline=rgba(accent, 95), width=1)
    centred(d, (150, 316), '@'+handle, font(10, True), rgba(accent))
    frames = []
    for frame in range(32):
        img = base.copy(); d = ImageDraw.Draw(img, 'RGBA')
        d.ellipse((69,64,230,225), outline=rgba(accent,75), width=2)
        start = frame * 360 / 32
        d.arc((69,64,230,225), start=start, end=start+58, fill=rgba(accent,245), width=3)
        angle = math.radians(start)
        x, y = 149.5+81*math.cos(angle), 144.5+81*math.sin(angle)
        d.ellipse((x-4,y-4,x+4,y+4), fill=rgba(accent))
        frames.append(img)
    palette = frames[0].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    frames = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in frames]
    frames[0].save(ROOT / ('creator-'+handle+'.gif'), save_all=True, append_images=frames[1:], duration=110, loop=0, optimize=True, disposal=1)

if __name__ == '__main__':
    make_workflow()
    make_creator('ndnima', 'Nima Dalir', 'DEVELOPER  /  EDUCATOR', CYAN, 1)
    make_creator('alireza-bokharaei', 'Alireza Bokharaei', 'SOFTWARE  /  INFRASTRUCTURE', PURPLE, 2)
    make_creator('ostovar04', 'Matin', 'PYTHON  /  WEB', GOLD, 3)
