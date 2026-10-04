# tasks

A personal task board with two views of the **same** `tasks.json`:

- **Terminal** (`tasks`): a full-screen keyboard board. Every change saves instantly.
- **Web** (`tasks web`): the same board in the browser, with drag-and-drop, a month calendar and a "next two weeks" list that merges dated tasks with your Google Calendar events.

Change something in one view and the other picks it up within a few seconds. One Python file, standard library only, no database, no accounts.

```
~/.local/share/taskboard/tasks.json   ← your tasks (both views read and write this)
~/.config/taskboard/config.json       ← settings, e.g. your calendar's secret link
```

## Install

```bash
git clone https://github.com/AndreiUser/tasks ~/code/tasks
mkdir -p ~/.local/bin
ln -sf ~/code/tasks/tasks.py ~/.local/bin/tasks
chmod +x ~/code/tasks/tasks.py
```

Needs Python 3.9+ (for time zones). Make sure `~/.local/bin` is on your `PATH`. To update later: `git -C ~/code/tasks pull`.

## Use

```bash
tasks                          # terminal board (press ? for keys)
tasks ls                       # quick list
tasks add "repair bike" -s soon -d fri
tasks web                      # start the web view and open the browser (Ctrl-C stops it)
```

The web view has a **Save** button. The terminal saves on every key. If you edit in both at once, the web view won't overwrite the terminal's change. Instead it tells you, and you choose to save over it or discard your edit.

## Link your Google Calendar (read-only)

1. Open Google Calendar on the web, then **Settings** and your calendar under *Settings for my calendars*.
2. Go to **Integrate calendar** and copy the **Secret address in iCal format**.
3. Run:

   ```bash
   tasks config ics "https://calendar.google.com/calendar/ical/…/basic.ics"
   ```

The web view then shows the next 14 days of events, refreshed every 5 minutes. Nothing is written back to your calendar.

Anyone with the secret address can read your calendar, so it lives only in `~/.config/taskboard/config.json` (readable only by you) and never in this repo. If it ever leaks, reset it in the same Google settings page. `tasks config` shows the current settings, and `tasks config ics` with no URL unlinks the calendar. Any other calendar that offers an ICS/iCal link works the same way.

Supported in calendar feeds: single, all-day and multi-day events, time zones, repeats (daily, weekly on chosen days, monthly by date or "2nd Tuesday", yearly), and skipped or moved occurrences.

## Keep the web view running (optional)

`tasks web` runs only while its terminal is open. To have it always available at <http://localhost:8765>, run it as a user service:

```ini
# ~/.config/systemd/user/taskboard.service
[Unit]
Description=Task board web view

[Service]
ExecStart=%h/.local/bin/tasks web --no-open
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now taskboard
```

## Security

The server listens on `127.0.0.1` only, so nothing outside your laptop can reach it, and it has no login. Before running it on a home server or VPS with `--host`, put it behind a reverse proxy with authentication, or reach it only over a VPN such as Tailscale or WireGuard.

## Data format

```json
{
  "items": [
    {"id": "t1", "text": "Print Abmeldung papers", "section": "soon", "date": "2026-10-06"},
    {"id": "t2", "text": "Reply to Mr Berger", "section": "done", "doneOn": "2026-09-30"}
  ],
  "updated": "2026-10-04T08:50:00+00:00"
}
```

`section` is one of `urgent`, `soon`, `later`, `backlog`, `done`. The order of `items` is the order on the board. `date` (due date) and `doneOn` are optional. To restore from a backup: `tasks import tasks.json.bak`.
