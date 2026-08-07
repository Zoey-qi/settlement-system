#!/usr/bin/env python3
# 从正版 Bootstrap Icons 精灵图抽取项目实际用到的图标，生成本地自托管的 _icons.html
import re, os, glob

PROJ = r"C:\Users\Administrator\WorkBuddy\2026-08-02-08-29-45\settlement_system"
TEMPLATES = os.path.join(PROJ, "templates")
SPRITE = r"C:\Users\Administrator\.workbuddy\binaries\node\workspace\package\bootstrap-icons.svg"
CURRENT = os.path.join(TEMPLATES, "_icons.html")

# 1) 收集所有 ui.icon('NAME') 用到的图标名
names = set()
for f in glob.glob(os.path.join(TEMPLATES, "*.html")):
    txt = open(f, encoding="utf-8").read()
    for m in re.findall(r"ui\.icon\(\s*'([^']+)'", txt):
        names.add(m)
    # 同时捕获字面 <use href="#i-NAME"> 直接引用（如登录页密码显隐图标）
    for m in re.findall(r"#i-([a-z0-9-]+)", txt):
        names.add(m)

sprite_txt = open(SPRITE, encoding="utf-8").read()
current_txt = open(CURRENT, encoding="utf-8").read()

def extract(src, name):
    # 正版 sprite 里 id 为 name（无前缀）；现有手写文件里 id 为 i-name
    # 注意：symbol 的属性顺序不定（viewBox 可能在 id 之前），故用 [^>]* 宽松匹配
    for cand in (name, "i-" + name):
        m = re.search(r'<symbol\b[^>]*\bid="%s"[^>]*>.*?</symbol>' % re.escape(cand), src, re.S)
        if m:
            block = m.group(0)
            block = re.sub(r'\bid="%s"' % re.escape(cand), 'id="i-%s"' % name, block, count=1)
            # 去掉 path 上写死的 fill="currentColor"，改为继承外层 svg 的 fill，
            # 这样中性图标才能用 CSS 渐变（金属光泽）填充，而语义/装饰图标仍走 currentColor。
            block = re.sub(r'\sfill="currentColor"', '', block)
            return block
    return None

# Bootstrap Icons 没有的裸名 -> 映射到存在的近义图标（仅用于文档示例 ui.icon('check')）
ALIAS = {"check": "check-lg"}

chosen, missing = [], []
for name in sorted(names):
    blk = extract(sprite_txt, name)
    if blk is None and name in ALIAS:
        blk = extract(sprite_txt, ALIAS[name])
    if blk is None:
        blk = extract(current_txt, name)  # 回退：保留现有手写版
    if blk is None:
        missing.append(name)
        continue
    chosen.append(blk)

header = ('<!--\n'
          '  全站 SVG icon sprite（Bootstrap Icons 正版 1.13.1，本地自托管）\n'
          '  仅包含页面实际用到的图标；id 统一加 i- 前缀以匹配 ui.icon() 宏。\n'
          '  生成方式：scripts/build_icons.py 从官方 bootstrap-icons.svg 抽取。\n'
          '-->\n')
# 金属金渐变（暗金基调 #B8860B + 高光/暗部），用于中性图标 fill: url(#gold-metal)
GOLD_GRAD = (
    '  <linearGradient id="gold-metal" x1="0%" y1="0%" x2="100%" y2="100%">\n'
    '    <stop offset="0%" stop-color="#E6C66E"/>\n'
    '    <stop offset="42%" stop-color="#C9A227"/>\n'
    '    <stop offset="62%" stop-color="#B8860B"/>\n'
    '    <stop offset="100%" stop-color="#7A5606"/>\n'
    '  </linearGradient>\n'
)
svg_open = '<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0" style="position:absolute" aria-hidden="true">\n<defs>\n' + GOLD_GRAD
svg_close = '</defs>\n</svg>\n'
out = header + svg_open + "\n".join(chosen) + "\n" + svg_close
open(CURRENT, "w", encoding="utf-8").write(out)
# 同时输出可外部缓存的精灵文件（被 _macros.html 的 ui.icon 宏以 /static/icons.svg#id 引用）
STATIC_ICONS = os.path.join(PROJ, "static", "icons.svg")
open(STATIC_ICONS, "w", encoding="utf-8").write(out)

print("TOTAL_NAMES:", len(names))
print("WRITTEN:", len(chosen))
print("MISSING:", missing)
