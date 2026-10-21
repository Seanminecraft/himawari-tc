#!/usr/bin/env python3
"""
（維護者用，一般不必執行）把 Noto Sans CJK TC 裁成只含 scripts/*.py 會用到的中文字，
輸出 fonts/HWNotoSansTC-subset.otf（約數十 KB），這樣 GitHub Actions 不必每次安裝 CJK 字型。
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
