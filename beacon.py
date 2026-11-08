#!/usr/bin/env python3
"""Agent Beacon — dependency-free terminal agent activity dashboard."""
import argparse
from collections import deque
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import select
import shutil
import sys
import termios
import time
import tty
import unicodedata

BG = (14, 21, 42)
COLORS = {
    'text': (198, 211, 232), 'muted': (103, 123, 153),
    'dim': (49, 66, 95), 'cyan': (117, 231, 244),
    'gold': (241, 191, 126), 'violet': (177, 161, 235),
    'green': (123, 212, 173), 'error': (246, 133, 143),
    'white': (236, 251, 255),
}

def clean(value):
    return ''.join(c for c in str(value) if not unicodedata.category(c).startswith('C'))


def fit(text, width):
    result, used = '', 0
    for c in clean(text):
        n = 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
        if used + n > width:
            break
        result += c
        used += n
    return result


class Canvas:
    def __init__(self, width, height):
        self.w, self.h = width, height
        self.cells = [[(' ', 'text') for _ in range(width)] for _ in range(height)]

    def text(self, x, y, text, color='text'):
        if not 0 <= y < self.h:
            return
        for c in clean(text):
            n = 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
            if 0 <= x and x + n <= self.w:
                self.cells[y][x] = (c, color)
                if n == 2:
                    self.cells[y][x + 1] = ('', color)
            x += n

    def center(self, x, y, width, text, color='text'):
        text = fit(text, width)
        used = sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)
        self.text(x + max(0, (width - used) // 2), y, text, color)

    def box(self, x, y, w, h, title='', color='dim', active=False):
        horizontal, vertical = ('━', '┃') if active else ('─', '│')
        corners = ('┏', '┓', '┗', '┛') if active else ('┌', '┐', '└', '┘')
        self.text(x, y, corners[0] + horizontal * (w - 2) + corners[1], color)
        self.text(x, y + h - 1, corners[2] + horizontal * (w - 2) + corners[3], color)
        for row in range(y + 1, y + h - 1):
            self.text(x, row, vertical, color)
            self.text(x + w - 1, row, vertical, color)
        if title:
            self.text(x + 2, y, ' ' + fit(title, w - 6) + ' ', color)

    def path(self, points, frame, active=False, color='cyan', animate=True):
        cells = []
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            dx = (x2 > x1) - (x2 < x1)
            dy = (y2 > y1) - (y2 < y1)
            x, y = x1, y1
            while (x, y) != (x2, y2):
                cells.append((x, y))
                self.text(x, y, '│' if dy else '─', color if active else 'dim')
                x, y = x + dx, y + dy
        for index in range(1, len(points) - 1):
            x, y = points[index]
            px, py = points[index - 1]
            nx, ny = points[index + 1]
            directions = frozenset([
                ((px > x) - (px < x), (py > y) - (py < y)),
                ((nx > x) - (nx < x), (ny > y) - (ny < y)),
            ])
            corner = {
                frozenset([(-1, 0), (0, 1)]): '┐',
                frozenset([(1, 0), (0, 1)]): '┌',
                frozenset([(-1, 0), (0, -1)]): '┘',
                frozenset([(1, 0), (0, -1)]): '└',
            }.get(directions, '┼')
            self.text(x, y, corner, color if active else 'dim')
        if active and animate and cells:
            for offset, symbol in [(2, '·'), (1, '•'), (0, '◆')]:
                x, y = cells[(frame - offset) % len(cells)]
                self.text(x, y, symbol, 'white' if offset == 0 else color)
        x, y = points[-1]
        before = points[-2]
        self.text(x, y, '▼' if y > before[1] else '▶', color if active else 'dim')

    def lines(self, ansi=True):
        output = []
        for row in self.cells:
            if not ansi:
                output.append(''.join(c for c, _ in row))
                continue
            chunks, last = [], None
            for char, color in row:
                if color != last:
                    r, g, b = COLORS[color]
                    chunks.append(f'\033[38;2;{r};{g};{b}m')
                    last = color
                chunks.append(char)
            output.append(''.join(chunks))
        return output


@dataclass
class Review:
    stamp: str
    detail: str
    active: bool = False


@dataclass
class Agent:
    name: str
    model: str = 'unknown'
    state: str = 'waiting'
    detail: str = 'awaiting activity'
    tools: int = 0
    calls: int = 0
    progress: object = None
    role: str = 'subagent'
    started: float = 0.0
    updated: float = 0.0
    last_tool: str = ''
    tokens: int = 0
    cost: float = 0.0


def _number(value):
    """Return a finite non-negative float, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def add_usage(target, usage, input_key='input', output_key='output', cache_read_key='cacheRead', cache_write_key='cacheWrite'):
    """Accumulate one message's token/cost usage onto an Agent."""
    if not isinstance(usage, dict):
        return
    total = _number(usage.get('totalTokens'))
    if total is None:
        total = 0.0
        for key in (input_key, output_key, cache_read_key, cache_write_key):
            value = _number(usage.get(key))
            if value is not None:
                total += value
    cost = usage.get('cost')
    if isinstance(cost, dict):
        cost = cost.get('total')
    cost = _number(cost)
    target.tokens += int(total)
    if cost is not None:
        target.cost += cost


def fmt_tokens(value):
    if value >= 1_000_000:
        return f'{value / 1_000_000:.1f}M'
    if value >= 1_000:
        return f'{value / 1_000:.1f}k'
    return str(int(value))


def fmt_cost(value):
    if value <= 0:
        return ''
    if value < 0.01:
        return f'${value:.4f}'
    if value < 1:
        return f'${value:.3f}'
    return f'${value:,.2f}'


def usage_text(agent, compact=False):
    parts = []
    if agent.tokens:
        parts.append(fmt_tokens(agent.tokens) + ('' if compact else ' tok'))
    if agent.cost:
        parts.append(fmt_cost(agent.cost))
    return ' · '.join(parts)


def usage_totals(state):
    agents = [state.main, state.advisor] + list(state.agents.values())
    return sum(a.tokens for a in agents), sum(a.cost for a in agents)


@dataclass
class State:
    main: Agent = field(default_factory=lambda: Agent('main', role='main'))
    advisor: Agent = field(default_factory=lambda: Agent('advisor', state='not connected', role='advisor'))
    agents: dict = field(default_factory=dict)
    logs: deque = field(default_factory=lambda: deque(maxlen=100))
    reviews: deque = field(default_factory=lambda: deque(maxlen=20))
    total: int = 0
    error: str = ''
    label: str = 'waiting for session'

    def event(self, event):
        kind = event.get('type', '')
        if kind not in ('session', 'tool', 'agent_start', 'agent_end', 'advisor', 'done', 'error', 'progress', 'usage'):
            raise ValueError('unsupported event type: ' + str(kind))
        agent_id = str(event.get('agent_id', 'main'))
        if agent_id == 'main':
            target = self.main
        elif agent_id == '__advisor':
            target = self.advisor
        else:
            target = self.agents.setdefault(agent_id, Agent(agent_id))
        now = time.monotonic()
        if target.started <= 0:
            target.started = now
        target.updated = now
        if event.get('model') and kind != 'advisor':
            target.model = clean(event['model'])
        if kind == 'usage':
            tokens = _number(event.get('tokens'))
            cost = _number(event.get('cost'))
            if tokens is None and cost is None:
                raise ValueError('usage requires a finite non-negative tokens or cost')
            if tokens is not None:
                target.tokens += int(tokens)
            if cost is not None:
                target.cost += cost
            self.total += 1
            return
        if kind == 'session':
            self.label = clean(event.get('name', 'local session'))
            target.state = 'running'
            target.role = 'main'
        elif kind == 'agent_start':
            target.name = clean(event.get('name', agent_id))
            target.state = 'running'
            target.role = 'subagent'
            target.started = now
        elif kind in ('agent_end', 'done'):
            target.state = 'done'
        elif kind == 'tool':
            target.state = 'running'
            target.tools += 1
            target.last_tool = clean(event.get('tool', ''))
        elif kind == 'advisor':
            target = self.advisor
            target.role = 'advisor'
            target.model = clean(event.get('model', 'unknown'))
            active = event.get('active', True)
            target.state = 'consulting' if active else 'done'
            if active:
                target.calls += 1
                target.started = now
            detail = clean(event.get('detail', 'advisor review'))
            if not active and self.reviews and self.reviews[-1].active:
                self.reviews[-1].active = False
                self.reviews[-1].detail = detail
                self.reviews[-1].stamp = time.strftime('%H:%M:%S')
            else:
                self.reviews.append(Review(time.strftime('%H:%M:%S'), detail, active))
        elif kind == 'error':
            target.state = 'error'
        elif kind == 'progress':
            completed, total = event.get('completed'), event.get('total')
            if not isinstance(completed, (int, float)) or not isinstance(total, (int, float)) or not math.isfinite(completed) or not math.isfinite(total) or total <= 0 or not 0 <= completed <= total:
                raise ValueError('progress requires finite 0 <= completed <= total and total > 0')
            target.progress = completed / total
        if kind != 'advisor':
            target.detail = clean(event.get('detail', event.get('tool', kind)))
        else:
            target.detail = self.reviews[-1].detail if self.reviews else clean(event.get('detail', kind))
        self.total += 1
        self.logs.append((time.strftime('%H:%M:%S'), target.name, target.detail, target.role))

    def waiting_on(self):
        if self.advisor.state == 'consulting':
            return 'waiting on advisor'
        running = [a.name for a in self.agents.values() if a.state == 'running']
        if running and self.main.state in ('waiting', 'idle'):
            return 'waiting on ' + ', '.join(running[:3])
        return ''


class EventFile:
    """Incremental reader; incomplete final lines wait for the next poll."""
    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.identity = None

    def poll(self):
        stat = self.path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity != self.identity or stat.st_size < self.offset:
            self.offset, self.identity = 0, identity
        events = []
        with self.path.open('rb') as source:
            source.seek(self.offset)
            for _ in range(2000):
                line = source.readline()
                if not line or not line.endswith(b'\n'):
                    break
                self.offset = source.tell()
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError('event must be an object')
                    events.append(event)
                except (ValueError, UnicodeError) as error:
                    events.append({'type': 'error', 'detail': f'Invalid JSONL at byte {self.offset}: {error}'})
        return events


class ClaudeSource:
    def __init__(self, path):
        self.root = Path(path).expanduser()
        self.readers = {}
        self.session = None
        self.scan_at = 0
        self.tools = {}
        self.usage_seen = set()

    def poll(self, state):
        now = time.monotonic()
        if now >= self.scan_at:
            self.scan_at = now + 2
            if self.root.is_file():
                candidates = [self.root]
            else:
                candidates = [p for p in self.root.glob('*/*.jsonl') if p.is_file()]
            if candidates:
                session = max(candidates, key=lambda p: p.stat().st_mtime)
                if session != self.session:
                    self.session = session
                    self.readers = {session: EventFile(session)}
                    state.main = Agent('main')
                    state.agents.clear()
                    state.advisor = Agent('advisor', state='not connected')
                    state.total = 0
                    state.logs.clear()
                    self.tools.clear()
                    self.usage_seen.clear()
                    state.label = session.stem[:24]
                subdir = session.parent / session.stem / 'subagents'
                for path in subdir.glob('agent-*.jsonl'):
                    self.readers.setdefault(path, EventFile(path))
        for path, reader in list(self.readers.items()):
            agent_id = 'main' if path == self.session else path.stem
            for row in reader.poll():
                kind = row.get('type')
                if kind == 'error':
                    state.event(row)
                    continue
                message = row.get('message', {})
                if not isinstance(message, dict):
                    continue
                content = message.get('content', [])
                if agent_id != 'main' and agent_id not in state.agents:
                    state.event({'type': 'agent_start', 'agent_id': agent_id, 'name': agent_id, 'detail': 'subagent transcript detected'})
                if message.get('model'):
                    (state.main if agent_id == 'main' else state.agents[agent_id]).model = clean(message['model'])
                if kind == 'assistant':
                    target = state.main if agent_id == 'main' else state.agents[agent_id]
                    key = message.get('id') or row.get('uuid')
                    if key is None or key not in self.usage_seen:
                        if key is not None:
                            self.usage_seen.add(key)
                        add_usage(target, message.get('usage'), 'input_tokens', 'output_tokens',
                                  'cache_read_input_tokens', 'cache_creation_input_tokens')
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') == 'tool_use':
                        name = block.get('name', 'tool')
                        args = block.get('input', {})
                        if not isinstance(args, dict):
                            args = {}
                        detail = args.get('file_path') or args.get('pattern') or args.get('description') or args.get('command') or ''
                        self.tools[block.get('id')] = name
                        state.event({'type': 'tool', 'agent_id': agent_id, 'tool': name, 'detail': f'{name} · {clean(detail)[:160]}'})
                    elif block.get('type') == 'tool_result':
                        target = state.main if agent_id == 'main' else state.agents[agent_id]
                        target.state = 'error' if block.get('is_error') else 'waiting'
                        target.detail = self.tools.get(block.get('tool_use_id'), 'tool') + (' failed' if block.get('is_error') else ' returned')
                    elif block.get('type') == 'text' and kind == 'assistant':
                        target = state.main if agent_id == 'main' else state.agents[agent_id]
                        target.detail = clean(block.get('text', ''))[:160]
                        target.state = 'responding'
                if message.get('stop_reason') == 'end_turn':
                    state.event({'type': 'done' if agent_id == 'main' else 'agent_end', 'agent_id': agent_id, 'detail': 'turn completed'})


class OmpSource:
    """Read OMP's session/custom tool events and sibling agent transcripts."""
    def __init__(self, path):
        self.root = Path(path).expanduser()
        self.session = None
        self.readers = {}
        self.scan_at = 0
        self.seen = set()
        self.pending = {}
        self.advice = {}

    def poll(self, state):
        now = time.monotonic()
        if now >= self.scan_at:
            self.scan_at = now + 1
            candidates = [self.root] if self.root.is_file() else list(self.root.glob('*/*.jsonl'))
            candidates = [p for p in candidates if not p.name.startswith('.')]
            if candidates:
                session = max(candidates, key=lambda p: p.stat().st_mtime)
                if session != self.session:
                    self.session = session
                    self.readers = {session: EventFile(session)}
                    self.seen.clear()
                    self.pending.clear()
                    self.advice.clear()
                    state.main = Agent('main', role='main')
                    state.advisor = Agent('advisor', state='not connected', role='advisor')
                    state.agents.clear()
                    state.logs.clear()
                    state.reviews.clear()
                    state.total = 0
                    state.label = session.stem[:24]
                for path in session.with_suffix('').glob('*.jsonl'):
                    if not path.name.startswith('.'):
                        self.readers.setdefault(path, EventFile(path))
        for path, reader in list(self.readers.items()):
            agent_id = 'main' if path == self.session else path.stem
            for row in reader.poll():
                self.consume(state, row, agent_id)

    def consume(self, state, row, agent_id='main'):
        advisor = agent_id == '__advisor'
        if agent_id not in ('main', '__advisor') and agent_id not in state.agents:
            state.event({'type': 'agent_start', 'agent_id': agent_id, 'name': agent_id, 'detail': 'OMP subagent transcript detected'})
        target = state.main if agent_id == 'main' else state.advisor if advisor else state.agents[agent_id]
        kind = row.get('type')
        if kind == 'error':
            state.event(dict(row, agent_id=agent_id))
        elif kind == 'session':
            if agent_id == 'main':
                state.event({'type': 'session', 'name': row.get('cwd', 'OMP session'), 'detail': 'OMP session connected'})
        elif kind in ('title', 'title_change') and agent_id == 'main':
            state.label = clean(row.get('title', state.label))
        elif kind == 'model_change':
            target.model = clean(row.get('model', target.model))
        elif kind == 'custom' and row.get('customType') == 'tool_execution_start':
            data = row.get('data', {})
            if isinstance(data, dict):
                self.tool(state, target, agent_id, data.get('toolCallId'), data.get('toolName', 'tool'), data.get('intent', ''))
        elif kind == 'message':
            message = row.get('message', {})
            if not isinstance(message, dict):
                return
            role = message.get('role')
            if message.get('model'):
                target.model = clean(message['model'])
            if role == 'user':
                if advisor:
                    if target.state != 'consulting':
                        state.event({'type': 'advisor', 'model': target.model, 'detail': 'reviewing the session'})
                else:
                    target.state, target.detail = 'running', 'processing user input'
            elif role == 'toolResult':
                call_id = message.get('toolCallId')
                self.pending.setdefault(agent_id, set()).discard(call_id)
                failed = bool(message.get('isError'))
                name = clean(message.get('toolName', 'tool'))
                detail = name + (' failed' if failed else ' returned')
                if failed:
                    state.event({'type': 'error', 'agent_id': agent_id, 'detail': detail})
                else:
                    target.state = 'running' if self.pending.get(agent_id) else 'waiting'
                    target.detail = detail
                    state.logs.append((time.strftime('%H:%M:%S'), target.name, detail, target.role))
                    if advisor and name == 'advise':
                        note = self.advice.get((agent_id, call_id), '')
                        state.event({'type': 'advisor', 'model': target.model, 'active': False,
                                     'detail': note or 'review returned'})
            elif role == 'assistant':
                add_usage(target, message.get('usage'))
                content = message.get('content', [])
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') == 'toolCall':
                        args = block.get('arguments', {})
                        if not isinstance(args, dict):
                            args = {}
                        if advisor and block.get('name') == 'advise':
                            self.advice[(agent_id, block.get('id'))] = clean(args.get('note', ''))[:200]
                        detail = args.get('i') or args.get('path') or args.get('command') or args.get('description') or ''
                        self.tool(state, target, agent_id, block.get('id'), block.get('name', 'tool'), detail)
                    elif block.get('type') == 'text' and not self.pending.get(agent_id):
                        target.state, target.detail = 'responding', clean(block.get('text', ''))[:160]
                stop = message.get('stopReason')
                if stop == 'stop':
                    if advisor:
                        state.event({'type': 'advisor', 'model': target.model, 'active': False, 'detail': 'OMP advisor review returned'})
                    else:
                        state.event({'type': 'done' if agent_id == 'main' else 'agent_end', 'agent_id': agent_id, 'detail': 'OMP turn completed'})
                elif stop in ('error', 'aborted'):
                    state.event({'type': 'error', 'agent_id': agent_id, 'detail': clean(message.get('errorMessage', stop))})

    def tool(self, state, target, agent_id, call_id, name, detail):
        key = (agent_id, call_id)
        if call_id and key in self.seen:
            return
        if call_id:
            self.seen.add(key)
            self.pending.setdefault(agent_id, set()).add(call_id)
        state.event({'type': 'tool', 'agent_id': agent_id, 'tool': name, 'detail': f'{name} · {clean(detail)[:140]}'})


class Demo:
    def __init__(self):
        self.step = -1

    def poll(self, state, elapsed):
        step = int(elapsed * 2)
        if step == self.step:
            return
        self.step = step
        phase = step % 64
        if phase == 0:
            state.agents.clear()
            for agent in (state.main, state.advisor):
                agent.tokens, agent.cost = 0, 0.0
            state.event({'type': 'session', 'name': 'payment refactor / DEMO', 'model': 'Sonnet · demo', 'detail': 'plan the payment migration'})
            state.event({'type': 'advisor', 'model': 'Opus · demo', 'detail': 'reviewing the execution plan'})
        elif phase == 3:
            state.event({'type': 'usage', 'tokens': 52000, 'cost': 0.14})
        elif phase == 5:
            state.event({'type': 'usage', 'agent_id': '__advisor', 'tokens': 9800, 'cost': 0.06})
            state.event({'type': 'advisor', 'model': 'Opus · demo', 'active': False, 'detail': 'split payment extraction from migration'})
        elif phase == 8:
            for name, detail in [('search', 'map the repository'), ('grep', 'find payment call sites'), ('docs', 'read the API reference')]:
                state.event({'type': 'agent_start', 'agent_id': name, 'name': name, 'model': 'Haiku · demo', 'detail': detail})
        elif phase == 40:
            state.event({'type': 'advisor', 'model': 'Opus · demo', 'detail': 'checking the research summaries'})
        elif phase == 42:
            state.event({'type': 'usage', 'agent_id': '__advisor', 'tokens': 7400, 'cost': 0.05})
            state.event({'type': 'advisor', 'model': 'Opus · demo', 'active': False, 'detail': 'summaries are consistent, proceed to edit'})
        elif 9 <= phase <= 43:
            if phase == 10:
                for name, tokens, cost in [('search', 4200, 0.002), ('grep', 3100, 0.0015), ('docs', 2600, 0.0012)]:
                    state.event({'type': 'usage', 'agent_id': name, 'tokens': tokens, 'cost': cost})
            for i, name in enumerate(['search', 'grep', 'docs']):
                progress = min(100, max(0, (phase - 9) * (4 - i)))
                state.event({'type': 'progress', 'agent_id': name, 'completed': progress, 'total': 100, 'detail': ['mapping source files', 'finding payment references', 'reading API pages'][i]})
                if progress == 100:
                    state.agents[name].state = 'done'
            state.main.detail = 'delegating read-only research'
        elif phase == 44:
            for name in ('search', 'grep', 'docs'):
                state.event({'type': 'progress', 'agent_id': name, 'completed': 100, 'total': 100, 'detail': 'research completed'})
                state.event({'type': 'agent_end', 'agent_id': name, 'detail': 'summary returned to main'})
        elif phase == 45:
            state.event({'type': 'tool', 'tool': 'Edit', 'detail': 'editing payments/session.ts'})
        elif phase == 51:
            state.event({'type': 'tool', 'tool': 'Bash', 'detail': 'running payment regression checks'})
        elif phase == 58:
            state.event({'type': 'done', 'detail': 'review complete · all checks passed (demo)'})


def render(state, width, height, frame, mode, paused=False, page=0):
    c = Canvas(width, height)
    if width < 86 or height < 44:
        c.text(2, 2, 'AGENT BEACON', 'cyan')
        c.text(2, 4, f'Terminal {width} x {height} · minimum 86 x 44', 'gold')
        c.text(2, 6, 'Enlarge the window or reduce the font size.')
        c.text(2, 8, '[q] quit    [+/-] font size in terminal settings', 'muted')
        return c
    w = min(width - 4, 132)
    ox = (width - w) // 2
    c.center(ox, 1, w, 'A G E N T   B E A C O N', 'white')
    c.center(ox, 2, w, 'MAIN WRITES   ·   SUBAGENTS DIG   ·   ADVISOR REVIEWS', 'muted')
    c.text(ox, 4, '■ main', 'gold')
    c.text(ox + 17, 4, '■ subagents', 'cyan')
    c.text(ox + 38, 4, '■ advisor', 'violet')
    c.text(ox + w - 23, 4, ('Ⅱ PAUSED' if paused else '● ' + mode.upper()), 'gold' if paused else 'green')
    side = max(19, w // 5)
    left, area = ox + side + 4, w - side - 4
    center = left + area // 2
    log_y = height - 9
    agent_y = max(22, log_y - 13)
    main_w = min(45, area - 16)
    main_x = center - main_w // 2
    waiting = state.waiting_on()
    active = state.main.state in ('running', 'responding') or bool(waiting)
    c.box(main_x, 6, main_w, 6, 'MAIN SESSION', 'gold', active)
    c.center(main_x + 1, 7, main_w - 2, state.main.model, 'gold')
    main_usage = usage_text(state.main)
    if main_usage:
        c.center(main_x + 1, 8, main_w - 2, main_usage, 'muted')
    c.center(main_x + 1, 9, main_w - 2, state.main.detail)
    c.center(main_x + 1, 10, main_w - 2, '[ ' + (waiting or state.main.state) + ' ]', 'violet' if waiting else 'muted')
    c.box(ox, 6, side, log_y - 7, 'ADVISOR', 'violet', state.advisor.state == 'consulting')
    c.center(ox + 1, 8, side - 2, state.advisor.model, 'violet')
    advisor_usage = usage_text(state.advisor, compact=True)
    if advisor_usage:
        c.center(ox + 1, 9, side - 2, advisor_usage, 'muted')
    c.text(ox + 2, 10, fit(state.advisor.state, side - 4), 'text')
    reviews = list(state.reviews)[-3:]
    for i in range(3):
        y = 13 + i * 3
        if i < len(reviews):
            review = reviews[i]
            c.text(ox + 2, y, fit(review.stamp + ('  live' if review.active else ''), side - 4), 'violet' if review.active else 'muted')
            c.text(ox + 2, y + 1, fit(review.detail, side - 4))
        else:
            c.text(ox + 2, y, '◇ no review yet', 'muted')
    c.text(ox + 2, 22, f'reviews {state.advisor.calls}', 'violet')
    c.path([(ox + side, 9), (main_x - 1, 9)], frame, state.advisor.state == 'consulting', 'violet', not paused)
    route_y = 14
    c.path([(center, 12), (center, route_y - 1)], frame, active, 'gold', not paused)
    route_w = min(52, area - 4)
    route_x = center - route_w // 2
    c.box(route_x, route_y, route_w, 5, 'EXECUTION BUS', 'cyan', active)
    c.text(route_x + 2, route_y + 1, f'events {state.total:<8} tools {state.main.tools}', 'cyan')
    total_tokens, total_cost = usage_totals(state)
    spend = []
    if total_tokens:
        spend.append(fmt_tokens(total_tokens) + ' tok')
    if total_cost:
        spend.append(fmt_cost(total_cost))
    if spend:
        text = ' · '.join(spend)
        spend_x = route_x + route_w - 3 - len(text)
        if spend_x >= route_x + 24:
            c.text(spend_x, route_y + 1, text, 'muted')
    c.text(route_x + 2, route_y + 2, fit(state.label, route_w - 4))
    c.text(route_x + 2, route_y + 3, 'synthetic timeline' if mode.startswith('demo') else 'observed activity', 'muted')
    agents = list(state.agents.values())
    pages = max(1, (len(agents) + 2) // 3)
    page %= pages
    shown = agents[page * 3: page * 3 + 3]
    card_w = (area - 4) // 3
    branch_y = agent_y - 1
    running = any(a.state == 'running' for a in shown)
    c.path([(center, route_y + 5), (center, branch_y)], frame, running, animate=not paused)
    for i in range(3):
        x = left + i * (card_w + 2)
        target_x = center if i == 1 else x + card_w // 2
        a = shown[i] if i < len(shown) else Agent('awaiting agent')
        live = a.state == 'running'
        color = 'cyan' if live else 'green' if a.state == 'done' else 'error' if a.state == 'error' else 'dim'
        if target_x != center:
            c.path([(center, branch_y), (target_x, branch_y), (target_x, agent_y - 1)], frame + i * 4, live, animate=not paused)
        c.text(target_x, agent_y - 1, '▼', color)
        number = f'{page * 3 + i + 1:02d} ' if i < len(shown) else ''
        c.box(x, agent_y, card_w, 7, number + a.name, color, live)
        c.center(x + 1, agent_y + 1, card_w - 2, a.model, 'muted')
        c.center(x + 1, agent_y + 2, card_w - 2, a.state, color)
        if a.progress is not None:
            bar_w = max(2, card_w - 10)
            filled = int(bar_w * a.progress)
            c.text(x + 2, agent_y + 3, '━' * filled + '░' * (bar_w - filled), color)
            c.text(x + card_w - 6, agent_y + 3, f'{a.progress:3.0%}', color)
        else:
            metric = f'tools {a.tools}' if a.state != 'running' else (a.last_tool or f'tools {a.tools}')
            c.center(x + 1, agent_y + 3, card_w - 2, metric, color)
        agent_usage = usage_text(a, compact=True)
        if agent_usage:
            c.center(x + 1, agent_y + 4, card_w - 2, agent_usage, 'muted')
        c.center(x + 1, agent_y + 5, card_w - 2, a.detail, 'muted')
    c.center(left, agent_y - 3, area, f' SUBAGENTS / {len(agents)} observed / page {page + 1}/{pages} ', 'muted')
    summary_y = agent_y + 9
    for i in range(3):
        x = center if i == 1 else left + i * (card_w + 2) + card_w // 2
        if i != 1:
            c.path([(x, agent_y + 7), (x, summary_y - 1), (center, summary_y - 1)], frame, False)
        else:
            c.text(center, agent_y + 7, '│', 'dim')
    c.text(center, summary_y - 1, '┼', 'dim')
    c.box(main_x, summary_y, main_w, 3, '', 'gold')
    c.text(center, summary_y, '┴', 'gold')
    c.center(main_x + 1, summary_y + 1, main_w - 2, 'BACK TO MAIN · REVIEW + VERIFY', 'gold')
    c.box(ox, log_y, w, 7, 'SESSION LOG  /  LIVE', 'dim')
    logs = list(state.logs)[-4:]
    for i, (stamp, name, detail, role) in enumerate(logs):
        c.text(ox + 2, log_y + 2 + i, stamp, 'muted')
        c.text(ox + 12, log_y + 2 + i, fit(name, 12), {'main': 'gold', 'advisor': 'violet'}.get(role, 'cyan'))
        c.text(ox + 26, log_y + 2 + i, fit(detail, w - 29))
    footer = state.error or '[q] quit   [space] pause motion   [tab] agents   [r] clear log'
    c.text(ox, height - 1, fit(footer, w), 'error' if state.error else 'muted')
    return c


def stream_frames(state, update, mode, width, height):
    """Let OMP own terminal input/output; exchange frames over private pipes."""
    paused, page, frame = False, 0, 0
    pending = b''
    native = False
    previous = None
    start = time.monotonic()
    try:
        while True:
            update(time.monotonic() - start)
            if not paused:
                frame += 1
            if native:
                now = time.monotonic()
                def view(agent):
                    elapsed = max(0, int((now if agent.state in ('running', 'consulting') else agent.updated) - agent.started)) if agent.started else 0
                    return {
                        'name': agent.name, 'model': agent.model, 'state': agent.state,
                        'detail': agent.detail, 'tools': agent.tools, 'calls': agent.calls,
                        'progress': agent.progress, 'role': agent.role, 'elapsed': elapsed,
                        'lastTool': agent.last_tool, 'tokens': agent.tokens, 'cost': agent.cost,
                    }
                payload = {'state': {
                    'main': view(state.main), 'advisor': view(state.advisor),
                    'agents': [view(agent) for agent in state.agents.values()],
                    'reviews': [{'stamp': r.stamp, 'detail': r.detail, 'active': r.active} for r in state.reviews],
                    'waiting': state.waiting_on(),
                    'logs': list(state.logs), 'total': state.total, 'label': state.label,
                    'error': state.error, 'mode': mode, 'paused': paused, 'page': page,
                }}
            else:
                lines = render(state, width, height, frame, mode, paused, page).lines()
                lines = ['\033[48;2;14;21;42m' + line + '\033[0m' for line in lines]
                payload = {'width': width, 'height': height, 'lines': lines}
            encoded = json.dumps(payload)
            if encoded != previous:
                print(encoded, flush=True)
                previous = encoded
            ready, _, _ = select.select([sys.stdin], [], [], 0.1)
            if not ready:
                continue
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                return
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                command = json.loads(line)
                if command.get('type') == 'view':
                    native = command.get('native', False)
                elif command.get('type') == 'context' and not state.total:
                    state.main.model = clean(command.get('model', 'unknown'))
                    state.label = clean(command.get('label', 'OMP session'))
                    state.main.state = 'idle' if command.get('idle') else 'running'
                    state.main.detail = 'Ready · waiting for first task' if command.get('idle') else 'OMP is processing this session'
                elif command.get('type') == 'size':
                    width, height = command['width'], command['height']
                    if type(width) is not int or type(height) is not int or not 1 <= width <= 1000 or not 1 <= height <= 500:
                        raise ValueError('frame dimensions must be integers in 1..1000 by 1..500')
                elif command.get('type') == 'key':
                    key = command.get('key')
                    if key in ('q', 'escape', 'ctrl+c'):
                        return
                    if key == ' ':
                        paused = not paused
                    elif key == '\t':
                        page += 1
                    elif key == 'r':
                        state.logs.clear()
    except (BrokenPipeError, KeyboardInterrupt):
        return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument('--demo', action='store_true', help='play a clearly labeled synthetic workflow')
    sources.add_argument('--events', type=Path, help='follow a JSONL event file, including future appends')
    sources.add_argument('--claude', nargs='?', const=str(Path.home() / '.claude/projects'), help='watch latest Claude transcript; optionally specify file or projects directory')
    sources.add_argument('--omp', nargs='?', const=str(Path(os.environ.get('PI_CODING_AGENT_DIR', str(Path.home() / '.omp/agent'))) / 'sessions'), help='watch latest OMP/Tern session; optionally specify file or sessions directory')
    output = parser.add_mutually_exclusive_group()
    output.add_argument('--snapshot', action='store_true', help='print one plain-text frame and exit')
    output.add_argument('--frames', action='store_true', help='stream JSON frames for the OMP extension over stdin/stdout')
    parser.add_argument('--width', type=int, default=110, help='snapshot width')
    parser.add_argument('--height', type=int, default=48, help='snapshot height')
    args = parser.parse_args()
    if not args.demo and not args.events and args.claude is None and args.omp is None:
        args.demo = True
    if args.width < 1 or args.height < 1:
        parser.error('snapshot dimensions must be positive')
    state = State()
    demo = Demo() if args.demo else None
    events = EventFile(args.events) if args.events else None
    claude = ClaudeSource(args.claude) if args.claude else None
    omp = OmpSource(args.omp) if args.omp else None
    mode = 'demo / synthetic' if demo else 'live / events' if events else 'live / omp' if omp else 'live / claude'
    start = time.monotonic()

    def update(elapsed):
        try:
            state.error = ''
            if demo:
                demo.poll(state, elapsed)
            elif events:
                for event in events.poll():
                    try:
                        state.event(event)
                    except (ValueError, TypeError) as error:
                        state.error = clean(error)
            elif omp:
                omp.poll(state)
            else:
                claude.poll(state)
        except (OSError, ValueError, TypeError) as error:
            state.error = clean(error)

    if args.snapshot:
        if demo:
            for step in range(31):
                update(step / 2)
        else:
            update(0)
        print('\n'.join(render(state, args.width, args.height, 30, mode).lines(False)))
        return
    if args.frames:
        stream_frames(state, update, mode, args.width, args.height)
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error('interactive mode requires a terminal; use --snapshot for plain output')
    old = termios.tcgetattr(sys.stdin)
    paused, page, frame = False, 0, 0
    previous = []
    try:
        tty.setcbreak(sys.stdin.fileno())
        sys.stdout.write('\033[?1049h\033[?25l\033[48;2;14;21;42m\033[2J')
        while True:
            elapsed = time.monotonic() - start
            update(elapsed)
            if not paused:
                frame += 1
            width, height = shutil.get_terminal_size((110, 48))
            canvas = render(state, width, height, frame, mode, paused, page)
            lines = canvas.lines()
            chunks = []
            for index, line in enumerate(lines):
                if len(previous) != len(lines) or index >= len(previous) or previous[index] != line:
                    chunks.append(f'\033[{index + 1};1H' + line)
            sys.stdout.write(''.join(chunks))
            sys.stdout.flush()
            previous = lines
            ready, _, _ = select.select([sys.stdin], [], [], 0.1)
            if ready:
                key = os.read(sys.stdin.fileno(), 32)
                if not key or b'q' in key or b'\x03' in key:
                    break
                if b' ' in key:
                    paused = not paused
                if b'\t' in key:
                    page += 1
                if b'r' in key:
                    state.logs.clear()
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        sys.stdout.write('\033[0m\033[?25h\033[?1049l')
        sys.stdout.flush()


if __name__ == '__main__':
    main()
