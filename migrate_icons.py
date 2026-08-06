#!/usr/bin/env python3
"""一次性将所有 template 里 HTML 标签中的 <i class="bi bi-XXX"></i> 替换为 SVG 调用。

策略：
1. 只替换形如 <i class="...bi bi-XXX..."></i> 的整段（不在 JS 字符串里 - 那些由各模板单独手工改）
2. 在每个文件开头插入 {% import '_macros.html' as ui %}（如尚未有）
3. 替换规则尽量保留下属 utility class（text-success/text-danger/d-lg-none 之类），映射为 cls 参数

格式参考：
- <i class="bi bi-list-check"></i>          → {{ ui.icon('list-check', '', 16) }}
- <i class="bi bi-list-check text-success"></i> → {{ ui.icon('list-check', 'text-success', 16) }}
- <i class="bi bi-bar-chart fs-3"></i>      → {{ ui.icon('bar-chart', 'fs-3', 16) }}

默认 16px 不带尺寸；保留常见尺寸 12/14/18/20/24/32：检测 inline style width="..px" 后回填。
"""
import re
import os
import sys

TEMPLATES_DIR = 'templates'
ICON_NAME_RE = re.compile(r'bi-([a-z0-9-]+)')
ICON_TAG_RE = re.compile(
    r'<i\s+class="([^"]*?bi-([a-z0-9-]+)[^"]*)"(\s+style="[^"]*")?\s*></i>',
    re.IGNORECASE,
)
INLINE_SIZE_RE = re.compile(r'width\s*:\s*(\d+)\s*px', re.IGNORECASE)
FONT_SIZE_RE = re.compile(r'\b(?:fs-(\d)|fs-(\d)x|font-size:\s*(\d+)px)', re.IGNORECASE)


def detect_size(style: str) -> int:
    """把 inline style 里的 width:XXpx 转成 int。"""
    if not style:
        return 16
    m = INLINE_SIZE_RE.search(style)
    return int(m.group(1)) if m else 16


def detect_size_from_class(cls) -> int:
    """把 class 里的 fs-3 之类转成 int。Bootstrap fs-3 = 1.5rem ≈ 24px；fs-5 = 1.5rem 近似。"""
    for tok in cls.split():
        m = re.match(r'^fs-(\d)$', tok)
        if m:
            n = int(m.group(1))
            return {1: 12, 2: 16, 3: 24, 4: 32, 5: 40}.get(n, 16)
    return 16


def transform(match) -> str:
    full_class = match.group(1)
    icon_name = match.group(2)
    style = match.group(3) or ''
    # 去掉 bi-XXX，保留其他 class
    other_classes = []
    for tok in full_class.split():
        if tok.startswith('bi-'):
            continue
        other_classes.append(tok)
    cls = ' '.join(other_classes)
    size = detect_size(style) if style else detect_size_from_class(full_class)
    if cls:
        return f"{{{{ ui.icon('{icon_name}', '{cls}', {size}) }}}}"
    return f"{{{{ ui.icon('{icon_name}', '', {size}) }}}}"


def ensure_macro_import(text: str) -> str:
    """在文件顶部（<!DOCTYPE 之前或紧随其后）插入 import 语句；已有则跳过。"""
    if 'import \'_macros.html\' as ui' in text:
        return text
    # 插在 <!DOCTYPE html> 后面那行
    pattern = re.compile(r'(<\!DOCTYPE html>\s*\n)', re.IGNORECASE)
    if pattern.search(text):
        return pattern.sub(
            r"\1{% import '_macros.html' as ui %}\n",
            text, count=1)
    # 如果没有 DOCTYPE，插在文件首行
    return "{% import '_macros.html' as ui %}\n" + text


def process_file(path: str) -> tuple[int, str]:
    text = open(path, encoding='utf-8').read()
    new_text, count = ICON_TAG_RE.subn(transform, text)
    if count == 0:
        return 0, 'no html icon tags found'
    new_text = ensure_macro_import(new_text)
    open(path, 'w', encoding='utf-8').write(new_text)
    return count, 'ok'


def main():
    files = sorted(f for f in os.listdir(TEMPLATES_DIR) if f.endswith('.html')
                   and f not in ('_icons.html', '_macros.html', 'base.html'))
    total = 0
    for f in files:
        path = os.path.join(TEMPLATES_DIR, f)
        n, status = process_file(path)
        print(f'{f:30s}  replaced={n:3d}  {status}')
        total += n
    print(f'\nTOTAL replaced: {total}')


if __name__ == '__main__':
    main()