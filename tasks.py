#!/usr/bin/env python3
"""Task Board, terminal edition.

Run `tasks` for the full-screen board. Every change is written to disk
immediately (atomic write), so there is nothing to save and nothing to lose.

Quick commands without opening the board:
  tasks ls                         list open tasks
  tasks add "text" [-s soon] [-d +3]
  tasks import board.json          replace the board with a JSON export

Web view (same tasks.json, plus a calendar of the next two weeks):
  tasks web [--port 8765] [--no-open]   start the local server and open the browser
  tasks config ics "URL"                link your calendar (secret iCal address)
  tasks config                          show current settings

Data lives in ~/.local/share/taskboard/tasks.json (override with TASKS_FILE).
Settings live in ~/.config/taskboard/config.json.
"""
import curses
import datetime as dt
import json
import os
import random
import shutil
import string
import sys
import threading
import time

SECTIONS = [("urgent", "Urgent"), ("soon", "Soon"), ("later", "Later"),
            ("backlog", "Backlog"), ("done", "Done")]
SEC_IDS = [s for s, _ in SECTIONS]
SEC_NAME = dict(SECTIONS)
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DATA = os.environ.get("TASKS_FILE") or os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
    "taskboard", "tasks.json")
CONFIG = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "taskboard", "config.json")
HERE = os.path.dirname(os.path.realpath(__file__))


# ---------------------------------------------------------------- data ----

def today():
    return dt.date.today()


def uid():
    return "t" + format(int(time.time() * 1000), "x") + "".join(
        random.choices(string.ascii_lowercase + string.digits, k=4))


def read_items(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    items = d.get("items", []) if isinstance(d, dict) else d
    return [i for i in items if isinstance(i, dict) and "text" in i]


def write_items(path, items):
    """Atomic write shared by the terminal board and the web server."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": items,
                   "updated": dt.datetime.now(dt.timezone.utc).isoformat()},
                  f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Store:
    """The board on disk. Every mutation goes through commit(), which saves."""

    def __init__(self, path):
        self.path = path
        self.items = []
        self.mtime = None
        self.saved_at = None
        self.undo = []
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            self.load()
            shutil.copy2(path, path + ".bak")   # one backup per session start
        else:
            seed = os.path.join(HERE, "tasks.json")
            if os.path.exists(seed) and os.path.abspath(seed) != os.path.abspath(path):
                self.items = read_items(seed)
            self.save()

    def load(self):
        self.items = read_items(self.path)
        self.mtime = os.path.getmtime(self.path)

    def save(self):
        write_items(self.path, self.items)
        self.mtime = os.path.getmtime(self.path)
        self.saved_at = dt.datetime.now()

    def changed_on_disk(self):
        try:
            return os.path.getmtime(self.path) != self.mtime
        except OSError:
            return False

    def commit(self, fn):
        """Snapshot for undo, apply fn, save to disk."""
        before = json.dumps(self.items)
        fn()
        if json.dumps(self.items) != before:
            self.undo.append(before)
            del self.undo[:-200]
            self.save()
            return True
        return False

    def undo_last(self):
        if not self.undo:
            return False
        self.items = json.loads(self.undo.pop())
        self.save()
        return True

    # board operations (same rules as the web board)
    def find(self, iid):
        return next((x for x in self.items if x["id"] == iid), None)

    def by_sec(self, sec):
        return [x for x in self.items if x["section"] == sec]

    def add(self, text, sec, after=None, date=None):
        it = {"id": uid(), "text": text, "section": sec}
        if date:
            it["date"] = date
        self.items.append(it)
        self.move_to(it["id"], sec, after=after)
        return it["id"]

    def move_to(self, iid, sec, after=None):
        k = next(i for i, x in enumerate(self.items) if x["id"] == iid)
        it = self.items.pop(k)
        it["section"] = sec
        if sec == "done":
            it.setdefault("doneOn", today().isoformat())
        else:
            it.pop("doneOn", None)
        pos = None
        if after:
            pos = next((i + 1 for i, x in enumerate(self.items) if x["id"] == after), None)
        if pos is None:
            last = -1
            for i, x in enumerate(self.items):
                if x["section"] == sec:
                    last = i
            pos = last + 1 if last >= 0 else len(self.items)
        self.items.insert(pos, it)

    def move_within(self, iid, d):
        it = self.find(iid)
        lst = self.by_sec(it["section"])
        k = lst.index(it)
        if not 0 <= k + d < len(lst):
            return
        a, b = self.items.index(it), self.items.index(lst[k + d])
        self.items[a], self.items[b] = self.items[b], self.items[a]

    def remove(self, iid):
        self.items = [x for x in self.items if x["id"] != iid]


# --------------------------------------------------------------- dates ----

def parse_date(s):
    """'' -> None (clear). Accepts 2026-10-05, 5.10, 5.10.2026, +3, today,
    tomorrow, mon..sun (next one). Returns ISO string or raises ValueError."""
    s = s.strip().lower()
    t = today()
    if not s or s in ("-", "none", "clear"):
        return None
    if s in ("today", "tod", "heute"):
        return t.isoformat()
    if s in ("tomorrow", "tom", "morgen"):
        return (t + dt.timedelta(days=1)).isoformat()
    if s.startswith("+") and s[1:].isdigit():
        return (t + dt.timedelta(days=int(s[1:]))).isoformat()
    if s[:3] in WEEKDAYS:
        n = (WEEKDAYS.index(s[:3]) - t.weekday()) % 7 or 7
        return (t + dt.timedelta(days=n)).isoformat()
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        pass
    parts = s.rstrip(".").split(".")
    if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
        d, m = int(parts[0]), int(parts[1])
        y = int(parts[2]) if len(parts) == 3 else t.year
        if y < 100:
            y += 2000
        out = dt.date(y, m, d)
        if len(parts) == 2 and out < t - dt.timedelta(days=60):
            out = dt.date(y + 1, m, d)
        return out.isoformat()
    raise ValueError(s)


def rel_label(iso):
    try:
        d = dt.date.fromisoformat(iso)
    except ValueError:
        return iso, 0
    diff = (d - today()).days
    if diff < 0:
        return f"{-diff}d late", diff
    if diff == 0:
        return "Today", 0
    if diff == 1:
        return "Tomorrow", 1
    if diff < 7:
        return d.strftime("%a ") + f"{d.day} {MON[d.month - 1]}", diff
    return f"{d.day} {MON[d.month - 1]}", diff


# ------------------------------------------------------------------ UI ----

HELP = [
    ("Moving around", ""),
    ("↑ ↓  j k", "move cursor"),
    ("Tab  ← →", "jump to next / previous section"),
    ("g  G", "top / bottom"),
    ("Editing (saved instantly)", ""),
    ("a  o", "add task below the cursor"),
    ("e  Enter", "edit task text"),
    ("x  Space", "mark done / undone"),
    ("d", "set date: 5.10  2026-10-05  +3  today  tomorrow  fri  (empty clears)"),
    ("J K  Shift ↑↓", "move task up / down"),
    ("H L  Shift ←→", "move task to previous / next section"),
    ("1-5", "move task to Urgent / Soon / Later / Backlog / Done"),
    ("D", "delete task (asks y/n)"),
    ("u", "undo (up to 200 steps this session)"),
    ("View", ""),
    ("z", "show / hide finished tasks"),
    ("c", "show / hide the next-two-weeks panel"),
    ("q", "quit"),
    ("While typing", ""),
    ("Enter / Esc", "confirm / cancel"),
    ("Ctrl-A Ctrl-E", "start / end of line"),
    ("Ctrl-U Ctrl-W", "clear line / delete word"),
]


class UI:
    def __init__(self, store):
        self.s = store
        self.sel_id = None
        self.sel_sec = "urgent"
        self.idx = 0
        self.top = 0
        self.show_done = False
        self.show_up = True
        self.msg = ""
        self.msg_at = 0
        self.prev_sec = {}
        self.touched = []      # ids changed by each commit, for undo cursor

    # ---- helpers
    def flash(self, m):
        self.msg, self.msg_at = m, time.time()

    def selectables(self):
        out = []
        for sec in SEC_IDS:
            lst = self.s.by_sec(sec)
            if sec == "done" and not self.show_done:
                out.append((sec, None))
                continue
            if lst:
                out += [(sec, x["id"]) for x in lst]
            else:
                out.append((sec, None))
        return out

    def resolve(self):
        sel = self.selectables()
        hit = None
        for i, (sec, iid) in enumerate(sel):
            if (self.sel_id and iid == self.sel_id) or (not self.sel_id and iid is None and sec == self.sel_sec):
                hit = i
                break
        self.idx = hit if hit is not None else max(0, min(self.idx, len(sel) - 1))
        self.sel_sec, self.sel_id = sel[self.idx]
        return sel

    def current(self):
        return self.s.find(self.sel_id) if self.sel_id else None

    def select(self, sec, iid):
        self.sel_sec, self.sel_id = sec, iid

    # ---- drawing
    def put(self, scr, y, x, text, attr=0):
        h, w = scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        text = text[: max(0, w - x - (1 if y == h - 1 else 0))]
        try:
            scr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def draw(self, scr):
        sel = self.resolve()
        scr.erase()
        h, w = scr.getmaxyx()
        t = today()
        # header
        title = " TASK BOARD "
        self.put(scr, 0, 0, title, curses.A_BOLD | curses.A_REVERSE)
        self.put(scr, 0, len(title) + 1,
                 f"{t.strftime('%a')} {t.day} {MON[t.month - 1]} {t.year} · KW {t.isocalendar()[1]}",
                 curses.A_DIM)
        if self.msg and time.time() - self.msg_at < 4:
            right, attr = self.msg, curses.A_BOLD | C("soon")
        elif self.s.saved_at:
            right, attr = "✓ saved " + self.s.saved_at.strftime("%H:%M:%S"), C("later")
        else:
            right, attr = "autosave on", curses.A_DIM
        self.put(scr, 0, max(0, w - len(right) - 1), right, attr)

        rows = []   # (kind, payload)
        if self.show_up:
            horizon = (t + dt.timedelta(days=14)).isoformat()
            up = sorted((x for x in self.s.items
                         if x.get("date") and x["section"] != "done" and x["date"] <= horizon),
                        key=lambda x: x["date"])
            rows.append(("uphead", len(up)))
            if up:
                for x in up[:8]:
                    rows.append(("up", x))
                if len(up) > 8:
                    rows.append(("upmore", len(up) - 8))
            else:
                rows.append(("upnone", None))
            rows.append(("gap", None))
        sel_row = 0
        for sec in SEC_IDS:
            lst = self.s.by_sec(sec)
            rows.append(("head", (sec, len(lst))))
            if sec == "done" and not self.show_done:
                if (sec, None) == (self.sel_sec, self.sel_id):
                    sel_row = len(rows)
                rows.append(("collapsed", len(lst)))
            elif not lst:
                if (sec, None) == (self.sel_sec, self.sel_id):
                    sel_row = len(rows)
                rows.append(("empty", sec))
            else:
                for x in lst:
                    if x["id"] == self.sel_id:
                        sel_row = len(rows)
                    rows.append(("task", x))
            rows.append(("gap", None))

        body_top, body_h = 2, max(1, h - 4)
        if sel_row < self.top:
            self.top = sel_row
        elif sel_row >= self.top + body_h:
            self.top = sel_row - body_h + 1
        if sel_row < 6 and self.show_up:
            self.top = min(self.top, 0)
        self.top = max(0, min(self.top, max(0, len(rows) - body_h)))

        for n, (kind, p) in enumerate(rows[self.top: self.top + body_h]):
            y = body_top + n
            selected = (self.top + n) == sel_row and kind in ("task", "empty", "collapsed")
            self.draw_row(scr, y, w, kind, p, selected)

        if self.top > 0:
            self.put(scr, 1, w - 3, "▲", curses.A_DIM)
        if self.top + body_h < len(rows):
            self.put(scr, h - 2, w - 3, "▼", curses.A_DIM)
        foot = " a add  e edit  x done  d date  J/K order  H/L section  D delete  u undo  ? help  q quit"
        self.put(scr, h - 1, 0, foot.ljust(w), curses.A_REVERSE | curses.A_DIM)
        scr.refresh()

    def draw_row(self, scr, y, w, kind, p, selected):
        rev = curses.A_REVERSE if selected else 0
        if kind == "uphead":
            self.put(scr, y, 1, "Next two weeks", curses.A_BOLD | C("accent"))
        elif kind == "upnone":
            self.put(scr, y, 3, "Nothing dated. Press d on a task to give it a date.", curses.A_DIM)
        elif kind == "upmore":
            self.put(scr, y, 3, f"… and {p} more", curses.A_DIM)
        elif kind == "up":
            lab, diff = rel_label(p["date"])
            attr = C("urgent") | curses.A_BOLD if diff < 0 else (C("accent") | curses.A_BOLD if diff <= 1 else curses.A_DIM)
            self.put(scr, y, 3, lab.ljust(11), attr)
            self.put(scr, y, 15, "●", C(p["section"]))
            self.put(scr, y, 17, p["text"])
        elif kind == "head":
            sec, n = p
            self.put(scr, y, 1, "● ", C(sec) | curses.A_BOLD)
            self.put(scr, y, 3, SEC_NAME[sec], C(sec) | curses.A_BOLD)
            self.put(scr, y, 4 + len(SEC_NAME[sec]), str(n), curses.A_DIM)
        elif kind == "empty":
            txt = "(empty — press a to add)" if p != "done" else "(nothing finished yet)"
            self.put(scr, y, 3, txt.ljust(w - 4) if selected else txt, curses.A_DIM | rev)
        elif kind == "collapsed":
            txt = f"▸ {p} finished — z to show" if p else "(nothing finished yet)"
            self.put(scr, y, 3, txt.ljust(w - 4) if selected else txt, curses.A_DIM | rev)
        elif kind == "task":
            done = p["section"] == "done"
            box = "[x] " if done else "[ ] "
            right, rattr = "", curses.A_DIM
            if done and p.get("doneOn"):
                right = "done " + p["doneOn"][5:].replace("-", ".")
            elif p.get("date"):
                right, diff = rel_label(p["date"])
                rattr = C("urgent") | curses.A_BOLD if diff < 0 else (C("accent") if diff <= 1 else curses.A_DIM)
            elif p.get("due"):
                right = p["due"]
            avail = w - 4 - len(box) - len(right) - 3
            text = p["text"]
            if len(text) > avail:
                text = text[: max(0, avail - 1)] + "…"
            tattr = curses.A_DIM if done else 0
            line = (box + text).ljust(max(0, w - 6 - len(right)))
            self.put(scr, y, 3, line, tattr | rev)
            if right:
                self.put(scr, y, w - 2 - len(right), right, rattr | rev)

    def help(self, scr):
        scr.erase()
        h, w = scr.getmaxyx()
        self.put(scr, 0, 1, "Keys", curses.A_BOLD)
        y = 2
        for k, d in HELP:
            if not d:
                y += 1 if y > 2 else 0
                self.put(scr, y, 1, k, curses.A_BOLD | C("accent"))
            else:
                self.put(scr, y, 3, k.ljust(16), curses.A_BOLD)
                self.put(scr, y, 20, d)
            y += 1
        self.put(scr, min(h - 2, y + 1), 1, f"Data file: {self.s.path}", curses.A_DIM)
        self.put(scr, h - 1, 1, "Press any key", curses.A_DIM)
        scr.refresh()
        scr.timeout(-1)
        scr.get_wch()
        scr.timeout(1500)

    # ---- input
    def prompt(self, scr, label, initial=""):
        scr.timeout(-1)
        curses.curs_set(1)
        buf, pos = list(initial), len(initial)
        try:
            while True:
                h, w = scr.getmaxyx()
                scr.move(h - 1, 0)
                scr.clrtoeol()
                self.put(scr, h - 1, 0, label, curses.A_BOLD | C("accent"))
                x0 = len(label)
                avail = max(1, w - x0 - 2)
                start = max(0, pos - avail + 1)
                self.put(scr, h - 1, x0, "".join(buf[start:start + avail]))
                scr.move(h - 1, min(w - 2, x0 + pos - start))
                scr.refresh()
                try:
                    ch = scr.get_wch()
                except curses.error:
                    continue
                if ch in ("\n", "\r") or ch == curses.KEY_ENTER:
                    return "".join(buf).strip()
                if ch == "\x1b":
                    return None
                if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if pos:
                        del buf[pos - 1]
                        pos -= 1
                elif ch == curses.KEY_DC:
                    if pos < len(buf):
                        del buf[pos]
                elif ch == curses.KEY_LEFT:
                    pos = max(0, pos - 1)
                elif ch == curses.KEY_RIGHT:
                    pos = min(len(buf), pos + 1)
                elif ch in (curses.KEY_HOME, "\x01"):
                    pos = 0
                elif ch in (curses.KEY_END, "\x05"):
                    pos = len(buf)
                elif ch == "\x15":
                    del buf[:pos]
                    pos = 0
                elif ch == "\x17":
                    k = pos
                    while k and buf[k - 1] == " ":
                        k -= 1
                    while k and buf[k - 1] != " ":
                        k -= 1
                    del buf[k:pos]
                    pos = k
                elif isinstance(ch, str) and ch.isprintable():
                    buf.insert(pos, ch)
                    pos += 1
        finally:
            curses.curs_set(0)
            scr.timeout(1500)

    def handle(self, scr, ch):
        s = self.s
        n_undo = len(s.undo)
        sel = self.resolve()
        it = self.current()
        key = ch if isinstance(ch, str) else None

        if key == "q":
            return False
        if key == "?":
            self.help(scr)
        elif key in ("j",) or ch == curses.KEY_DOWN:
            self.idx = min(len(sel) - 1, self.idx + 1)
            self.select(*sel[self.idx])
        elif key in ("k",) or ch == curses.KEY_UP:
            self.idx = max(0, self.idx - 1)
            self.select(*sel[self.idx])
        elif key == "g" or ch == curses.KEY_HOME:
            self.select(*sel[0])
        elif key == "G" or ch == curses.KEY_END:
            self.select(*sel[-1])
        elif key == "\t" or ch == curses.KEY_RIGHT or ch == curses.KEY_NPAGE:
            i = SEC_IDS.index(self.sel_sec)
            self.jump(sel, SEC_IDS[(i + 1) % len(SEC_IDS)])
        elif ch == curses.KEY_BTAB or ch == curses.KEY_LEFT or ch == curses.KEY_PPAGE:
            i = SEC_IDS.index(self.sel_sec)
            self.jump(sel, SEC_IDS[(i - 1) % len(SEC_IDS)])
        elif key == "z":
            self.show_done = not self.show_done
            if not self.show_done and self.sel_sec == "done":
                self.select("done", None)
        elif key == "c":
            self.show_up = not self.show_up
        elif key in ("a", "o"):
            sec = self.sel_sec if self.sel_sec != "done" else "backlog"
            text = self.prompt(scr, f" New task in {SEC_NAME[sec]}: ")
            if text:
                new = {}
                s.commit(lambda: new.setdefault("id", s.add(text, sec, after=it["id"] if it and it["section"] == sec else None)))
                self.select(sec, new["id"])
                self.flash("Added")
        elif key == "u":
            if s.undo_last():
                back = self.touched.pop() if self.touched else None
                if back and s.find(back):
                    self.select(s.find(back)["section"], back)
                    if s.find(back)["section"] == "done":
                        self.show_done = True
                self.flash("Undone")
            else:
                self.flash("Nothing to undo")
        elif it is None:
            pass
        elif key in ("e", "\n", "\r") or ch == curses.KEY_ENTER:
            text = self.prompt(scr, " Edit: ", it["text"])
            if text:
                s.commit(lambda: it.__setitem__("text", text))
        elif key in ("x", " "):
            if it["section"] == "done":
                back = self.prev_sec.get(it["id"], "later")
                s.commit(lambda: s.move_to(it["id"], back))
                self.select(back, it["id"])
                self.flash(f"Back to {SEC_NAME[back]}")
            else:
                self.prev_sec[it["id"]] = it["section"]
                s.commit(lambda: s.move_to(it["id"], "done"))
                self.flash("Done ✓  (u to undo)")
                if not self.show_done:
                    self.sel_id = None   # cursor stays at the same position
        elif key == "d":
            cur = it.get("date", "")
            raw = self.prompt(scr, " Date (5.10 · +3 · fri · tomorrow · empty clears): ", cur)
            if raw is not None:
                try:
                    d = parse_date(raw)
                except ValueError:
                    self.flash(f"Didn't understand date '{raw}'")
                else:
                    def apply():
                        if d:
                            it["date"] = d
                        else:
                            it.pop("date", None)
                    s.commit(apply)
                    self.flash(f"Date set: {rel_label(d)[0]}" if d else "Date cleared")
        elif key == "J" or ch == curses.KEY_SF:
            s.commit(lambda: s.move_within(it["id"], 1))
        elif key == "K" or ch == curses.KEY_SR:
            s.commit(lambda: s.move_within(it["id"], -1))
        elif key in ("H", "L") or ch in (curses.KEY_SLEFT, curses.KEY_SRIGHT):
            d = -1 if key == "H" or ch == curses.KEY_SLEFT else 1
            i = SEC_IDS.index(it["section"]) + d
            if 0 <= i < len(SEC_IDS):
                self.move_task(it, SEC_IDS[i])
        elif key and key in "12345":
            self.move_task(it, SEC_IDS[int(key) - 1])
        elif key == "D":
            short = it["text"] if len(it["text"]) < 40 else it["text"][:39] + "…"
            ans = self.prompt(scr, f" Delete \"{short}\"? (y/n) ")
            if ans and ans.lower().startswith("y"):
                s.commit(lambda: s.remove(it["id"]))
                self.sel_id = None
                self.flash("Deleted  (u to undo)")
        if len(s.undo) > n_undo:
            self.touched.append(self.sel_id if key in ("a", "o") or not it else it["id"])
            del self.touched[:-200]
        return True

    def move_task(self, it, sec):
        if sec == it["section"]:
            return
        if sec == "done":
            self.prev_sec[it["id"]] = it["section"]
        self.s.commit(lambda: self.s.move_to(it["id"], sec))
        if sec == "done" and not self.show_done:
            self.sel_id = None
            self.flash("Moved to Done")
        else:
            self.select(sec, it["id"])

    def jump(self, sel, sec):
        for cand in sel:
            if cand[0] == sec:
                self.select(*cand)
                return


_COLORS = {}


def C(name):
    return curses.color_pair(_COLORS.get(name, 0))


def init_colors():
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    pairs = {"urgent": curses.COLOR_RED, "soon": curses.COLOR_YELLOW,
             "later": curses.COLOR_GREEN, "backlog": curses.COLOR_MAGENTA,
             "done": curses.COLOR_WHITE, "accent": curses.COLOR_CYAN}
    for n, (name, fg) in enumerate(pairs.items(), start=1):
        curses.init_pair(n, fg, bg)
        _COLORS[name] = n


def run_tui(store):
    os.environ.setdefault("ESCDELAY", "25")

    def main(scr):
        curses.curs_set(0)
        init_colors()
        scr.keypad(True)
        scr.timeout(1500)
        ui = UI(store)
        while True:
            if store.changed_on_disk():
                store.load()
                ui.flash("Reloaded: changed in the web view or another terminal")
            ui.draw(scr)
            try:
                ch = scr.get_wch()
            except curses.error:
                continue          # timeout: loop to poll the file
            if ch == curses.KEY_RESIZE:
                continue
            if not ui.handle(scr, ch):
                break

    curses.wrapper(main)


# ------------------------------------------------------------ calendar ----
#
# Reads the calendar's ICS feed (for Google Calendar: Settings → your calendar
# → Integrate calendar → "Secret address in iCal format"). Read-only; nothing
# is ever written back. Standard library only: handles single events, all-day
# and multi-day events, time zones, and the common repeat rules (daily,
# weekly with weekdays, monthly by date or "2nd Tuesday", yearly), plus
# skipped or moved occurrences.

def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG)


def ics_url():
    return os.environ.get("TASKS_ICS_URL") or load_config().get("ics_url")


def _ics_lines(text):
    out = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        elif raw:
            out.append(raw)
    return out


def _ics_prop(line):
    q, cut = False, -1
    for i, ch in enumerate(line):
        if ch == '"':
            q = not q
        elif ch == ":" and not q:
            cut = i
            break
    if cut < 0:
        return None, {}, ""
    head, value = line[:cut], line[cut + 1:]
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _ics_text(v):
    return (v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",")
             .replace("\\;", ";").replace("\\\\", "\\")).strip()


def _tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def _ics_time(value, params):
    """-> date, or aware datetime."""
    v = value.strip()
    if params.get("VALUE") == "DATE" or (len(v) == 8 and v.isdigit()):
        return dt.date(int(v[:4]), int(v[4:6]), int(v[6:8]))
    utc = v.endswith("Z")
    v = v.rstrip("Z")
    d = dt.datetime(int(v[:4]), int(v[4:6]), int(v[6:8]),
                    int(v[9:11] or 0), int(v[11:13] or 0), int(v[13:15] or 0))
    if utc:
        return d.replace(tzinfo=dt.timezone.utc)
    tz = _tz(params["TZID"]) if "TZID" in params else None
    return d.replace(tzinfo=tz) if tz else d.astimezone()   # floating -> local


def _key(t):
    """Comparable identity for an occurrence start (EXDATE / RECURRENCE-ID)."""
    if isinstance(t, dt.datetime):
        return "T" + t.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    return "D" + t.isoformat()


def _duration(v):
    neg = v.startswith("-")
    v = v.lstrip("+-").lstrip("P")
    days = secs = 0
    num = ""
    in_time = False
    for ch in v:
        if ch == "T":
            in_time = True
        elif ch.isdigit():
            num += ch
        else:
            n = int(num or 0)
            num = ""
            if ch == "W":
                days += 7 * n
            elif ch == "D":
                days += n
            elif ch == "H":
                secs += 3600 * n
            elif ch == "M" and in_time:
                secs += 60 * n
            elif ch == "S":
                secs += n
    d = dt.timedelta(days=days, seconds=secs)
    return -d if neg else d


WD = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _add_months(t, n):
    y, m = divmod(t.month - 1 + n, 12)
    return y + t.year, m + 1


def _nth_weekday(y, m, wd, n):
    if n > 0:
        first = dt.date(y, m, 1)
        d = first + dt.timedelta(days=(wd - first.weekday()) % 7 + 7 * (n - 1))
    else:
        nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
        last = nxt - dt.timedelta(days=1)
        d = last - dt.timedelta(days=(last.weekday() - wd) % 7 + 7 * (-n - 1))
    return d if d.month == m else None


def _with_date(start, d):
    if isinstance(start, dt.datetime):
        return start.replace(year=d.year, month=d.month, day=d.day)
    return d


def _occurrences(start, rule, until_limit):
    """Yield occurrence starts from an RRULE, in order, up to until_limit (date)."""
    r = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq = r.get("FREQ", "")
    step = int(r.get("INTERVAL", "1") or 1)
    count = int(r["COUNT"]) if "COUNT" in r else None
    until = None
    if "UNTIL" in r:
        u = _ics_time(r["UNTIL"], {})
        until = u if isinstance(u, dt.date) and not isinstance(u, dt.datetime) else u.astimezone().date()
    byday = [x for x in r.get("BYDAY", "").split(",") if x]
    bymd = [int(x) for x in r.get("BYMONTHDAY", "").split(",") if x]
    sd = start.date() if isinstance(start, dt.datetime) else start
    emitted, k = 0, 0
    while k < 5000:
        if freq == "DAILY":
            cands = [sd + dt.timedelta(days=k * step)]
        elif freq == "WEEKLY":
            week0 = sd - dt.timedelta(days=sd.weekday()) + dt.timedelta(weeks=k * step)
            days = sorted(WD.index(x[-2:]) for x in byday) if byday else [sd.weekday()]
            cands = [week0 + dt.timedelta(days=w) for w in days]
        elif freq == "MONTHLY":
            y, m = _add_months(sd, k * step)
            cands = []
            if byday:
                for x in byday:
                    n = int(x[:-2]) if x[:-2] not in ("", "+") else 0
                    if n:
                        d = _nth_weekday(y, m, WD.index(x[-2:]), n)
                        cands += [d] if d else []
            else:
                for md in (bymd or [sd.day]):
                    try:
                        cands.append(dt.date(y, m, md) if md > 0 else
                                     dt.date(y + (m == 12), m % 12 + 1, 1) + dt.timedelta(days=md))
                    except ValueError:
                        pass
            cands.sort()
        elif freq == "YEARLY":
            try:
                cands = [sd.replace(year=sd.year + k * step)]
            except ValueError:   # 29 Feb
                cands = []
        else:
            yield start
            return
        for d in cands:
            if d < sd:
                continue
            if (until and d > until) or d > until_limit or (count is not None and emitted >= count):
                return
            emitted += 1
            yield _with_date(start, d)
        k += 1


def parse_ics(text, first, last):
    """Events touching [first, last) (dates) -> [{date, time, text, loc}]."""
    events, cur, depth = [], None, 0
    for line in _ics_lines(text):
        name, params, value = _ics_prop(line)
        if name == "BEGIN":
            if value.upper() == "VEVENT" and cur is None:
                cur = {"EXDATE": []}
            elif cur is not None:
                depth += 1
            continue
        if name == "END":
            if cur is not None and depth:
                depth -= 1
            elif cur is not None and value.upper() == "VEVENT":
                events.append(cur)
                cur = None
            continue
        if cur is None or depth:
            continue
        if name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            try:
                cur[name] = _ics_time(value, params)
            except (ValueError, IndexError):
                pass
        elif name == "EXDATE":
            for v in value.split(","):
                try:
                    cur["EXDATE"].append(_key(_ics_time(v, params)))
                except (ValueError, IndexError):
                    pass
        elif name in ("SUMMARY", "LOCATION", "RRULE", "STATUS", "UID", "DURATION"):
            cur[name] = value

    moved = {(e.get("UID"), _key(e["RECURRENCE-ID"])) for e in events if "RECURRENCE-ID" in e}
    out = []
    for e in events:
        if "DTSTART" not in e or e.get("STATUS", "").upper() == "CANCELLED":
            continue
        start = e["DTSTART"]
        allday = not isinstance(start, dt.datetime)
        if "DTEND" in e:
            length = e["DTEND"] - start
        elif "DURATION" in e:
            length = _duration(e["DURATION"])
        else:
            length = dt.timedelta(days=1) if allday else dt.timedelta(0)
        if "RRULE" in e and "RECURRENCE-ID" not in e:
            starts = _occurrences(start, e["RRULE"], last)
        else:
            starts = [start]
        text = _ics_text(e.get("SUMMARY", "")) or "(no title)"
        loc = _ics_text(e.get("LOCATION", ""))
        if loc == text:
            loc = ""
        for st in starts:
            k = _key(st)
            if k in e["EXDATE"] or ("RECURRENCE-ID" not in e and (e.get("UID"), k) in moved):
                continue
            if allday:
                d, end, n = st, st + max(length, dt.timedelta(days=1)), 0
                while d < end and n < 31:
                    if first <= d < last:
                        out.append({"date": d.isoformat(), "time": "", "text": text, "loc": loc})
                    d += dt.timedelta(days=1)
                    n += 1
            else:
                local = st.astimezone()
                if first <= local.date() < last:
                    out.append({"date": local.date().isoformat(), "time": local.strftime("%H:%M"),
                                "text": text, "loc": loc})
    out.sort(key=lambda x: (x["date"], x["time"] or "00:00", x["text"]))
    return out


_cal_cache = {"at": 0.0, "url": None, "text": None, "fetched": None, "error": None}
_cal_lock = threading.Lock()


def calendar_events(days=14, max_age=300):
    url = ics_url()
    if not url:
        return {"configured": False, "events": []}
    with _cal_lock:
        c = _cal_cache
        if c["url"] != url or time.time() - c["at"] > max_age:
            import urllib.request
            try:
                req = urllib.request.Request(url.replace("webcal://", "https://", 1),
                                             headers={"User-Agent": "taskboard"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    c["text"] = r.read().decode("utf-8", "replace")
                c["fetched"] = dt.datetime.now(dt.timezone.utc).isoformat()
                c["error"] = None
            except Exception as ex:   # keep the last good copy
                c["error"] = f"{type(ex).__name__}: {ex}"
            c["at"], c["url"] = time.time(), url
        first = today()
        evs = parse_ics(c["text"], first, first + dt.timedelta(days=days)) if c["text"] else []
        return {"configured": True, "events": evs, "fetchedAt": c["fetched"], "error": c["error"]}


# -------------------------------------------------------------- server ----
#
# A tiny local web server: the HTML view reads and writes the same tasks.json
# as the terminal board. Listens on 127.0.0.1 only, unless you pass --host.

def file_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        version = str(os.stat(path).st_mtime_ns)
    except (OSError, ValueError):
        d, version = {}, None
    items = d.get("items", []) if isinstance(d, dict) else d
    return {"items": items, "updated": d.get("updated") if isinstance(d, dict) else None,
            "version": version, "file": path.replace(os.path.expanduser("~"), "~", 1)}


def valid_items(items):
    return isinstance(items, list) and all(
        isinstance(i, dict) and isinstance(i.get("id"), str) and isinstance(i.get("text"), str)
        and i.get("section") in SEC_IDS for i in items)


def make_handler(path, allowed_hosts):
    import http.server
    write_lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "taskboard"

        def log_message(self, *a):
            pass

        def send_json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if allowed_hosts and host not in allowed_hosts:
                self.send_json(403, {"error": "unexpected Host header"})
                return False
            return True

        def do_GET(self):
            if not self.host_ok():
                return
            route = self.path.split("?", 1)[0]
            if route in ("/", "/index.html"):
                try:
                    with open(os.path.join(HERE, "index.html"), "rb") as f:
                        body = f.read()
                except OSError:
                    return self.send_json(500, {"error": "index.html not found next to tasks.py"})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route == "/api/tasks":
                self.send_json(200, file_state(path))
            elif route == "/api/events":
                self.send_json(200, calendar_events())
            else:
                self.send_json(404, {"error": "not found"})

        def do_PUT(self):
            if not self.host_ok():
                return
            if self.path.split("?", 1)[0] != "/api/tasks":
                return self.send_json(404, {"error": "not found"})
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self.send_json(415, {"error": "send JSON"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 5_000_000)) or b"{}")
            except ValueError:
                return self.send_json(400, {"error": "invalid JSON"})
            items = body.get("items")
            if not valid_items(items):
                return self.send_json(400, {"error": "items must be a list of {id, text, section}"})
            with write_lock:
                cur = file_state(path)
                if cur["version"] is not None and body.get("baseVersion") != cur["version"]:
                    return self.send_json(409, cur)   # changed in the terminal meanwhile
                write_items(path, items)
                self.send_json(200, file_state(path))

    return Handler


def serve(path, host, port, open_browser):
    import http.server
    import urllib.request
    import webbrowser
    url = f"http://{'localhost' if host in ('127.0.0.1', '::1', 'localhost') else host}:{port}/"
    try:   # already running? just open it
        with urllib.request.urlopen(url + "api/tasks", timeout=1) as r:
            if json.load(r).get("version") is not None:
                print(f"Task server already running at {url}")
                if open_browser:
                    webbrowser.open(url)
                return 0
    except Exception:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        Store(path)   # creates the file (seeded if a tasks.json sits next to this script)
    allowed = {"localhost", "127.0.0.1", "::1"} if host in ("127.0.0.1", "localhost", "::1") else set()
    try:
        httpd = http.server.ThreadingHTTPServer((host, port), make_handler(path, allowed))
    except OSError as ex:
        print(f"Couldn't start on port {port}: {ex}. Try: tasks web --port 8766")
        return 1
    print(f"Task board at {url}  (data: {path})")
    print("Calendar: " + ("linked" if ics_url() else 'not linked — run: tasks config ics "URL"'))
    print("Press Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


# ----------------------------------------------------------------- CLI ----

ANSI = {"urgent": "31", "soon": "33", "later": "32", "backlog": "35", "done": "90"}


def color(code, text):
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def cmd_ls(store, show_done=False):
    for sec in SEC_IDS:
        if sec == "done" and not show_done:
            continue
        lst = store.by_sec(sec)
        print(color("1;" + ANSI[sec], f"● {SEC_NAME[sec]}") + color("2", f" {len(lst)}"))
        for x in lst:
            extra = f"  ({rel_label(x['date'])[0]})" if x.get("date") else ""
            print(f"   {'[x]' if sec == 'done' else '[ ]'} {x['text']}{color('2', extra)}")
    if not show_done:
        print(color("2", f"   + {len(store.by_sec('done'))} done (tasks ls -a to show)"))


def usage():
    print(__doc__.strip())


def main(argv):
    if argv and argv[0] in ("-h", "--help", "help"):
        usage()
        return 0
    if argv and argv[0] == "web":
        rest, port, host = argv[1:], 8765, "127.0.0.1"
        for i, a in enumerate(rest):
            if a == "--port" and i + 1 < len(rest):
                port = int(rest[i + 1])
            elif a == "--host" and i + 1 < len(rest):
                host = rest[i + 1]
        return serve(DATA, host, port, "--no-open" not in rest)
    if argv and argv[0] == "config":
        cfg = load_config()
        if len(argv) >= 3 and argv[1] == "ics":
            cfg["ics_url"] = argv[2]
            save_config(cfg)
            print(f"Calendar linked. Saved to {CONFIG} (readable only by you).")
            return 0
        if len(argv) >= 2 and argv[1] == "ics":   # no URL: unlink
            cfg.pop("ics_url", None)
            save_config(cfg)
            print("Calendar unlinked.")
            return 0
        u = ics_url()
        print(f"Data file:  {DATA}")
        print(f"Config:     {CONFIG}")
        print("Calendar:   " + (u[:40] + "…  (secret, keep private)" if u else "not linked"))
        return 0
    store = Store(DATA)
    if not argv:
        if not sys.stdin.isatty():
            cmd_ls(store)
            return 0
        run_tui(store)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "ls":
        cmd_ls(store, show_done="-a" in rest)
    elif cmd == "add":
        sec, date, words = "backlog", None, []
        i = 0
        while i < len(rest):
            if rest[i] in ("-s", "--section") and i + 1 < len(rest):
                sec = next((x for x in SEC_IDS if x.startswith(rest[i + 1].lower())), None)
                if not sec:
                    print("Section must be one of: " + ", ".join(SEC_IDS[:-1]))
                    return 1
                i += 2
            elif rest[i] in ("-d", "--date") and i + 1 < len(rest):
                try:
                    date = parse_date(rest[i + 1])
                except ValueError:
                    print(f"Didn't understand date '{rest[i + 1]}'")
                    return 1
                i += 2
            else:
                words.append(rest[i])
                i += 1
        text = " ".join(words).strip()
        if not text:
            print('Usage: tasks add "text" [-s urgent|soon|later|backlog] [-d date]')
            return 1
        store.commit(lambda: store.add(text, sec, date=date))
        print(f"Added to {SEC_NAME[sec]}: {text}")
    elif cmd == "import":
        if not rest:
            print("Usage: tasks import FILE.json")
            return 1
        items = read_items(rest[0])
        store.commit(lambda: setattr(store, "items", items))
        print(f"Imported {len(items)} tasks into {store.path}")
    else:
        usage()
        return 1
    return 0


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # quiet when piped into head/less
    sys.exit(main(sys.argv[1:]))
