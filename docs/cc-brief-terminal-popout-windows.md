# <span style="color:#5baee8">CC Brief — Terminals open in their own browser windows</span>

**Date:** 2026-08-25
**Severity:** low — UX; no data or security impact
**Origin:** Todd, chat session 2026-08-25
**Component:** `static/index.html` (Machines tab terminal management) → new `static/term.html`
**Status:** done

---

## <span style="color:#5baee8">Symptom</span>

Every "Terminal" / "Attach" / "+ Terminal" click on the Machines tab creates an xterm.js instance inside a full-screen overlay in the SPA (`createTerminal()` → `#term-overlay`, tab strip via `renderTermTabs()`). All terminals share the SPA's browser window. Todd wants **each terminal in its own OS-level browser window** so the window manager, not a tab strip, arranges them.

## <span style="color:#5baee8">Root cause</span>

Not a bug — a design choice from the Phase 2 terminal PRD (`docs/rialu-phase2-terminal-prd.md` §4e, "full-screen overlay … multiple terminals can be open simultaneously (tabbed)"). Two things block a trivial `window.open` of the existing overlay:

1. The overlay is a DOM subtree of the SPA, not a page.
2. `openTerminalFor` / `openPaneTerminal` go through `loadXtermJS(cb)`, which lazy-loads xterm.js asynchronously on first use. `window.open` inside that callback is outside the click's user activation and will be popup-blocked.

## <span style="color:#5baee8">Fix</span>

Split the terminal out into a standalone page and have the SPA open it with `window.open`.

### <span style="color:#8ecef8">1. New `static/term.html`</span>

Standalone single-terminal page. No backend change: `/static` is already mounted in `main.py` and sits behind `CanonicalHostMiddleware`, so the popup is same-origin and carries the Cloudflare Access cookie.

- Reads `?machine=X` and optional `&pane=Y` from the query string.
- Loads xterm.js + fit addon itself via `<script>` tags in `<head>`.
- Connects to the **existing** endpoints: `/ws/terminal/{machine}` (fresh shell) or `/ws/pane/{machine}/{pane}` (attach).
- Same message protocol as today: writes `terminal_data` / `pane_data`, prints `[Session ended]` on `terminal_closed`, `[Disconnected]` on close.
- `document.title` = `machine:pane` or `machine` — this is what shows in the window title / taskbar.
- Fits to the window on load and on `resize`; sends `{type:'resize'}` on open and on `term.onResize`.
- Input (`term.onData`) wired **only for fresh shells**, exactly as now. Pane windows remain read-only; keystrokes to panes still go via `POST /api/machines/{m}/send`.
- Bottom status bar with a **reconnect** link shown on disconnect / session end.
- `beforeunload` → `ws.close()`.

```html
<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><title>rialú term</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
<style>
html,body{margin:0;height:100%;background:#0b0f14;overflow:hidden}
#term{position:absolute;inset:0;padding:4px}#term .xterm{height:100%}
#bar{position:absolute;left:0;right:0;bottom:0;font:10px monospace;color:#546880;background:#0d1018;padding:2px 8px;display:none}
#bar.on{display:block}#bar a{color:#5baee8;cursor:pointer}
</style>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
</head><body>
<div id="term"></div>
<div id="bar"><span id="bar-msg"></span> <a onclick="connect()">reconnect</a></div>
<script>
const q = new URLSearchParams(location.search);
const machine = q.get('machine'), pane = q.get('pane');
if (!machine) throw new Error('machine required');
document.title = pane ? `${machine}:${pane}` : machine;
const wsUrl = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host
  + (pane ? `/ws/pane/${machine}/${pane}` : `/ws/terminal/${machine}`);

const term = new Terminal({fontSize:13, fontFamily:'monospace', theme:{background:'#0b0f14'}, cursorBlink:true});
const fit = new FitAddon.FitAddon();
term.loadAddon(fit); term.open(document.getElementById('term')); fit.fit();

let ws = null;
function status(msg){
  const b = document.getElementById('bar');
  if (msg){ document.getElementById('bar-msg').textContent = msg; b.classList.add('on'); } else b.classList.remove('on');
  fit.fit();
}
function connect(){
  if (ws && ws.readyState <= WebSocket.OPEN) return;
  status(null);
  ws = new WebSocket(wsUrl);
  ws.onopen = () => { fit.fit(); ws.send(JSON.stringify({type:'resize', cols:term.cols, rows:term.rows})); term.focus(); };
  ws.onmessage = evt => {
    const d = JSON.parse(evt.data);
    if (d.type === 'terminal_data' || d.type === 'pane_data') term.write(d.data);
    else if (d.type === 'terminal_closed') { term.write('\r\n\x1b[31m[Session ended]\x1b[0m\r\n'); status('session ended'); }
  };
  ws.onclose = () => { term.write('\r\n\x1b[33m[Disconnected]\x1b[0m\r\n'); status('disconnected'); };
}
if (!pane) term.onData(d => { if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'data', data:d})); });
term.onResize(({cols,rows}) => { if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'resize', cols, rows})); });
window.addEventListener('resize', () => fit.fit());
window.addEventListener('beforeunload', () => ws?.close());
connect();
</script></body></html>
```

### <span style="color:#8ecef8">2. `static/index.html` — replace the terminal management block</span>

Replace the whole `// ── terminal management (xterm.js)` section (`terminalInstances`, `activeTerminalId`, `xtermLoaded`, `loadXtermJS`, `wsBase`, `openNewTerminal`, `openTerminalFor`, `openPaneTerminal`, `createTerminal`, `switchTerminal`, `renderTermTabs`, `closeTerminal`, `closeAllTerminals`) with:

```js
// ── terminal windows ──────────────────────────────────────────────────────
const termWindows = {};  // key -> Window; pane keys dedupe, shell keys are unique
function openTermWindow(machine, paneId){
  const key = paneId ? `${machine}:${paneId}` : `${machine}:shell:${Date.now()}`;
  const w0 = termWindows[key];
  if (w0 && !w0.closed){ w0.focus(); return; }
  const url = `/static/term.html?machine=${encodeURIComponent(machine)}` + (paneId ? `&pane=${encodeURIComponent(paneId)}` : '');
  const w = window.open(url, 'rialu-' + key.replace(/[^\w-]/g,'_'), 'popup,width=1000,height=640');
  if (!w){ alert('Popup blocked — allow popups for rialu.ie'); return; }
  termWindows[key] = w;
}
function openNewTerminal(){
  const cards = document.querySelectorAll('.m-card:not(.offline) .m-name');
  if (!cards.length){ alert('No machines online'); return; }
  openTermWindow(cards[0].textContent);
}
function openTerminalFor(machine){ openTermWindow(machine); }
function openPaneTerminal(machine, paneId){ openTermWindow(machine, paneId); }
```

The three existing call sites (`openNewTerminal()` on the "+ Terminal" button, `openTerminalFor(...)` on machine cards, `openPaneTerminal(...)` on Claude Code rows and tmux pane rows) keep their signatures — no changes to `renderMachineCards`, `renderClaudePanel`, or `loadTmuxSessions`.

### <span style="color:#8ecef8">3. Remove the dead overlay</span>

- Delete the `<!-- TERMINAL OVERLAY -->` div (`#term-overlay`, `#term-tabs`, `#term-body`).
- Delete the `/* terminal overlay */` CSS block (`.term-overlay`, `.term-hdr`, `.term-tabs`, `.term-tab`, `.term-tab-close`, `.term-body`).
- Delete the xterm CSS `<link>` in `<head>` — index.html no longer hosts a terminal.

## <span style="color:#5baee8">Behaviour and trade-offs</span>

- **Window vs tab is decided by the features string.** `popup` is honoured by Chrome; Firefox and Safari infer a popup window from `width`/`height`. Drop the features and you get a tab. A popup has no address/tab bar, which is right for a terminal.
- **`window.open` must run synchronously inside the click.** Moving the xterm load into `term.html` removes the async gap that the old `loadXtermJS` introduced. This is the real reason for the page split.
- **Terminals survive SPA reloads.** Each window owns its WebSocket, so reloading or closing `rialu.ie` no longer kills every open terminal. Hub load is unchanged: N windows = N WS = N agent sub-channels, same as N overlay tabs.
- **Dedupe on panes only.** Attaching a pane already open focuses its window. Fresh shells are never deduped — every "+ Terminal" / "Terminal" click is a new shell, by design.
- **No tab strip.** The OS window manager replaces it. That is the point.
- **Pane windows stay read-only** — unchanged behaviour, flagged so nobody "fixes" it in passing.
- **Faire/Tauri — unverified inference.** If the Rialú SPA is ever loaded inside Faire's Tauri webview, `window.open` there needs Tauri window creation rather than a browser popup. Browser-only today; not in scope, but check Faire's config before assuming it carries over.

## <span style="color:#5baee8">Acceptance</span>

- "+ Terminal", machine-card "Terminal", Claude-row "Open terminal", and tmux "Attach" each open a **separate browser window** titled `machine` or `machine:pane`.
- Two clicks on "+ Terminal" produce two windows with two independent shells.
- Two clicks on the same pane's "Attach" produce **one** window (second click focuses it).
- A fresh-shell window accepts keyboard input and resizes the remote pty on window resize.
- A pane window streams output and does not send keystrokes.
- Reloading `rialu.ie` leaves open terminal windows connected.
- Closing a window closes its WebSocket (hub-side `terminal_sessions.closed_at` populated as before).
- No `#term-overlay` markup, CSS, or terminal-management JS remains in `index.html`; `grep -c xterm static/index.html` is 0.
- `tests/test_pty_terminal.py` still passes (no backend change).

## <span style="color:#5baee8">Notes for the implementer</span>

- Do **not** add a backend route for `/term`; `/static/term.html` is already served and already behind Cloudflare Access via `CanonicalHostMiddleware`. Adding a route would just be a second thing to keep in sync.
- Do **not** pass `noopener` — the SPA needs the `Window` handle for pane dedupe/focus. Same-origin, so `opener` is harmless.
- Do not try to reuse `createTerminal` by `window.open`-ing the overlay; the async xterm lazy-load makes it popup-blocker bait.
- Keep the message types (`terminal_data`, `pane_data`, `terminal_closed`, `data`, `resize`) exactly as `ws_hub.py` emits/consumes them.

---

*FoxxeLabs · 2026-08-25*
