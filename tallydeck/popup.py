"""Shared Rich presentation and scrollable viewport for deck popups.

Rich is already used by tally-decide; import this only on the popup path.
"""
from __future__ import annotations
import os
import re
import select
import sys
import time
from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown, CodeBlock
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from rich.syntax import Syntax

INDIGO, AMBER, FG, DIM, GREEN = '#7B88FF', '#FFB224', '#F2F3F5', '#8B8F98', '#43B75D'
THEME = Theme({'markdown.h1': f'bold {AMBER}', 'markdown.h2': f'bold {AMBER}',
               'markdown.strong': f'bold {FG}', 'markdown.code': INDIGO,
               'markdown.link': INDIGO, 'table.border': DIM})


class WrappedCode(CodeBlock):
    def __rich_console__(self, console, options):
        yield Syntax(str(self.text).rstrip(), self.lexer_name or 'text',
                     theme=self.theme, word_wrap=True, padding=1)


class FullMarkdown(Markdown):
    elements = dict(Markdown.elements, fence=WrappedCode, code_block=WrappedCode)


def markdown(text: str, width: int):
    # Rich tables can drop entire columns when too narrow. Stack cells in that
    # case, retaining every heading and value. Wide terminals keep the table.
    lines = text.splitlines()
    out = []
    i = 0
    fence = ''
    while i < len(lines):
        marker = re.match(r'^\s*(`{3,}|~{3,})', lines[i])
        if marker:
            if not fence:
                fence = marker.group(1)
            elif marker.group(1).startswith(fence[0]) and len(marker.group(1)) >= len(fence):
                fence = ''
            out.append(lines[i]); i += 1; continue
        if fence:
            out.append(lines[i]); i += 1; continue
        # Rich drops HTML blocks entirely; retain details/summary text instead
        # of silently hiding the supporting conditions supplied by a raiser.
        if re.search(r'</?(?:details|summary)\b', lines[i], re.I):
            out.append(re.sub(r'</?(?:details|summary)[^>]*>', '', lines[i], flags=re.I))
            i += 1; continue
        if i + 1 < len(lines) and '|' in lines[i] and re.fullmatch(r'[\s|:\-]+', lines[i+1]) and '-' in lines[i+1]:
            headers = [x.strip() for x in lines[i].strip().strip('|').split('|')]
            rows = []; end = i + 2
            while end < len(lines) and '|' in lines[end] and lines[end].strip():
                rows.append([x.strip() for x in lines[end].strip().strip('|').split('|')]); end += 1
            columns = len(headers)
            min_widths = [max((len(word) for row in [headers, *rows] if n < len(row)
                              for word in row[n].split()), default=0)
                          for n in range(columns)]
            if width < max(70, columns * 18, sum(min_widths) + columns * 3 + 1):
                for row in rows:
                    out.append('')
                    for n, value in enumerate(row):
                        out.append(f'- **{headers[n] if n < len(headers) else n+1}:** {value}')
                i = end; continue
        # Unknown raw HTML blocks are otherwise omitted by Rich completely.
        line = lines[i]
        if re.match(r'^\s*</?[A-Za-z][A-Za-z0-9]*(?:\s|>|/>)', line):
            line = line.replace('<', r'\<')
        out.append(line); i += 1
    return FullMarkdown('\n'.join(out), code_theme='monokai', inline_code_theme='monokai')


def header(label: str, state: str, width: int, meta: str = ''):
    color = {'blocked': '#E5484D', 'attention': AMBER, 'success': GREEN,
             'working': '#3E9BFF'}.get(state, DIM)
    return Panel(Text.assemble((f' ◆ {state.upper()} ', f'bold black on {color}'),
                              (f'  {label}', f'bold {FG}'),
                              (f'\n{meta}' if meta else '', DIM)),
                 box=box.ROUNDED, border_style=INDIGO)


def body(text: str, title: str, width: int):
    return Panel(markdown(text, width - 4), title=Text(title, style=DIM),
                 title_align='left', border_style=INDIGO,
                 box=box.ROUNDED, padding=(0, 1))


def options(opts: list[str], width: int):
    if not opts:
        return Text('')
    per = min(3, len(opts)) if width >= 100 else 2 if width >= 70 else 1
    grid = Table.grid(expand=True, padding=(0, 1))
    for _ in range(per):
        grid.add_column(ratio=1)
    cards = [Panel(Text.assemble((f' {i} ', f'bold black on {AMBER}'), ('  '+o, FG)),
                   box=box.ROUNDED, border_style=INDIGO, padding=(0, 1))
             for i, o in enumerate(opts, 1)]
    for n in range(0, len(cards), per):
        row = cards[n:n+per]
        grid.add_row(*(row + [Text('')] * (per-len(row))))
    return grid


class Viewport:
    """A full document with a fixed, wrapped action bar. No hidden truncation."""
    def __init__(self, console: Console):
        self.console = console
        self.offset = 0
        self.page = 1
        self.total = 0

    def draw(self, content, actions: str):
        c = self.console
        opts = c.options.update(width=c.width, height=None)
        if getattr(self, '_content', None) is not content or getattr(self, '_width', None) != c.width:
            self._rows = c.render_lines(content, opts, pad=False)
            self._content, self._width = content, c.width
        rows = self._rows
        footer = c.render_lines(Text(actions, style=INDIGO), opts, pad=False)
        nav = f'{len(rows)}–{len(rows)}/{len(rows)} lines · j/k ↑↓ · PgUp/PgDn · Home/End'
        nav_height = len(c.render_lines(Text(nav), opts, pad=False))
        self.page = max(1, c.height - len(footer) - nav_height - 1)
        self.total = len(rows)
        self.offset = min(self.offset, max(0, self.total-self.page))
        c.file.write('\033[H\033[2J')
        for row in rows[self.offset:self.offset+self.page]:
            c.print(Text.assemble(*[(seg.text, seg.style) for seg in row]), end='\n', soft_wrap=True)
        for _ in range(max(0, self.page - len(rows[self.offset:self.offset+self.page]))):
            c.print()
        c.print(Text(f'{self.offset+1}–{min(self.total,self.offset+self.page)}/{self.total} lines · j/k ↑↓ · PgUp/PgDn · Home/End', style=DIM))
        c.print(Text(actions, style=INDIGO), end='')
        c.file.flush()

    def scroll(self, key: str) -> bool:
        delta = {'j': 1, '\x1b[B': 1, 'k': -1, '\x1b[A': -1,
                 '\x1b[6~': self.page, '\x06': self.page,
                 '\x1b[5~': -self.page, '\x02': -self.page}
        if key in delta:
            self.offset = max(0, min(max(0, self.total-self.page), self.offset+delta[key]))
        elif key in ('\x1b[H', '\x1b[1~', 'g'):
            self.offset = 0
        elif key in ('\x1b[F', '\x1b[4~', 'G'):
            self.offset = max(0, self.total-self.page)
        else:
            return False
        return True


def choose(content, actions: str, *, console=None, gone=None, timeout=300, viewport=None,
           valid_keys=None):
    """Scroll/resize until an action key; terminal query replies never act."""
    import termios, tty
    c = console or Console(theme=THEME, color_system='truecolor')
    v = viewport or Viewport(c)
    fd = sys.stdin.fileno(); old = termios.tcgetattr(fd)
    deadline = time.monotonic()+timeout
    try:
        tty.setcbreak(fd)
        c.file.write('\033[?2004h\033[?25l')
        c.file.flush()
        rendered = content(c.width)
        v.draw(rendered, actions)
        size = (c.width, c.height)
        while time.monotonic() < deadline:
            if gone and gone():
                return ''
            if (c.width, c.height) != size:
                size = (c.width, c.height)
                rendered = content(c.width); v.draw(rendered, actions)
            if not select.select([fd], [], [], .2)[0]:
                continue
            raw = os.read(fd, 1)
            if not raw:
                return ''
            key = raw.decode('utf-8', 'ignore')
            if not key:
                continue
            if key == '\x1b':
                while select.select([fd], [], [], .025)[0]:
                    key += os.read(fd, 1).decode('utf-8', 'ignore')
                    if len(key) >= 3 and ('@' <= key[-1] <= '~') or len(key) > 64:
                        break
                key = {'\x1bOA':'\x1b[A', '\x1bOB':'\x1b[B', '\x1bOH':'\x1b[H', '\x1bOF':'\x1b[F'}.get(key, key)
                if key == '\x1b[200~':
                    # Pasted prose is not a sequence of menu commands. In
                    # particular an 'x' inside a paste must never park a job.
                    tail = b''
                    while time.monotonic() < deadline:
                        if gone and gone():
                            return ''
                        if select.select([fd], [], [], .2)[0]:
                            part = os.read(fd, 1)
                            if not part:
                                return ''
                            tail = (tail + part)[-6:]
                            if tail == b'\x1b[201~':
                                break
                    continue
                if key.startswith('\x1b[') and not v.scroll(key):
                    continue
                if key != '\x1b':
                    v.draw(rendered, actions); continue
            if v.scroll(key):
                v.draw(rendered, actions); continue
            if valid_keys is not None and key not in valid_keys:
                continue
            return key
        return ''
    finally:
        c.file.write('\033[?2004l\033[?25h')
        c.file.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
