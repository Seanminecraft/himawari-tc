#!/usr/bin/env python3
"""
（維護者用，一般不必執行）把 Noto Sans CJK TC 裁成「Big5 全部繁體字（約 1.3 萬字）＋ scripts/*.py 用到的所有字＋全形標點」，
輸出 fonts/HWNotoSansTC-subset.otf，這樣 GitHub Actions 不必每次安裝 CJK 字型。
v18：舊版只收「當時」程式裡的字，v17 新增的「雷達、回波、疊、實…」不在裡面，Actions 上畫出來變成方框（□）。
     改成收整個 Big5，之後新增中文字也不會缺字；himawari_pipeline.py 另外會在畫圖時逐字檢查、缺字就在紀錄裡警告。
修改圖上的中文字之後重新執行：
  python tools/make_font_subset.py /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
字型授權：SIL Open Font License 1.1（見 fonts/OFL.txt）。
"""
import glob
import os
import sys

from fontTools import subset
from fontTools.ttLib import TTCollection, TTFont

here = os.path.dirname(os.path.abspath(__file__))
root = os.path.dirname(here)
src = sys.argv[1] if len(sys.argv) > 1 else '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
chars = set()
for fn in glob.glob(os.path.join(root, 'scripts', '*.py')):
    chars |= {c for c in open(fn, encoding='utf-8').read() if ord(c) > 127}
chars |= set('０１２３４５６７８９：（）＋　，。、')
# v18：整個 Big5（cp950）字集——繁體常用＋次常用字、注音、全形符號
for hi in range(0x81, 0xFF):
    for lo in list(range(0x40, 0x7F)) + list(range(0xA1, 0xFF)):
        try:
            c = bytes([hi, lo]).decode('cp950')
        except UnicodeDecodeError:
            continue
        if len(c) == 1 and ord(c) > 127:
            chars.add(c)
chars |= set('°µ×÷±≥≤→←↑↓·•…–—‧℃※〈〉《》「」『』【】〔〕！？；～｜／＼＿＝％＆＃＊＠')
if src.lower().endswith('.ttc'):
    col = TTCollection(src)
    font = next(f for f in col.fonts if f['name'].getDebugName(1) == 'Noto Sans CJK TC')
else:
    font = TTFont(src)
opts = subset.Options()
opts.layout_features = ['*']
opts.name_IDs = ['*']
opts.notdef_outline = True
sub = subset.Subsetter(opts)
sub.populate(text=''.join(sorted(chars)))
sub.subset(font)
fam = 'HW Noto Sans TC Subset'
for rec in font['name'].names:
    if rec.nameID in (1, 16):
        rec.string = fam
    elif rec.nameID == 4:
        rec.string = fam + ' Regular'
    elif rec.nameID == 6:
        rec.string = 'HWNotoSansTCSubset-Regular'
out = os.path.join(root, 'fonts', 'HWNotoSansTC-subset.otf')
os.makedirs(os.path.dirname(out), exist_ok=True)
font.save(out)
print('%d 個非 ASCII 字元 → %s (%d bytes)' % (len(chars), out, os.path.getsize(out)))
