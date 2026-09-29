"""The agent's cockpit: work in a window the person can watch, and hand control back and forth.

* **Visible agent.** The agent's own cursor glides to each button before it clicks, and a bar at
  the top of the window says what it's doing.
* **Take over any time.** Clicking, typing or scrolling in the window pauses the agent at once
  (so nobody fights it for the mouse); "Resume" hands control back. "Stop" ends the run.
* **Ask for help.** When a button can't be found (platforms move things), the agent pauses and
  asks the person to click it. It watches that click, finishes the step, and remembers the new
  label for next time.
* **Teach mode.** The person does a process once; every click, entry, shortcut, list choice and
  new window is written down as plain-language steps ("Click "Block".", "Press Ctrl+K.") that the
  agent can follow later and the person can edit.

The agent only ever controls its own browser window, never the rest of the computer.
Password and code fields are never recorded.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

OVERLAY = r"""
(() => {
  if (window.__bp) return;
  const S = { acting: false, mode: "off", picking: false };
  window.__bp = S;
  const signal = m => { try { window.__bpSignal && window.__bpSignal(m); } catch (e) {} };
  const css = `
    #__bpBar{position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:2147483647;display:flex;align-items:center;gap:10px;
      background:#161038;color:#fff;border:1px solid #7b5cff;border-radius:999px;padding:6px 8px 6px 14px;box-shadow:0 8px 24px rgba(0,0,0,.45);
      font:600 13px/1.3 -apple-system,Segoe UI,Roboto,sans-serif;max-width:92vw}
    #__bpBar .dot{width:9px;height:9px;border-radius:50%;background:#7bc47f;flex:none}
    #__bpBar.paused .dot{background:#ffb84d} #__bpBar.help .dot,#__bpBar.rec .dot{background:#e0687a;animation:__bpBlink 1s infinite}
    @keyframes __bpBlink{50%{opacity:.25}}
    #__bpBar button{font:600 12px/1 inherit;border:0;border-radius:999px;padding:7px 12px;cursor:pointer;background:#7b5cff;color:#fff}
    #__bpBar button.ghost{background:transparent;border:1px solid #6a6399;color:#ddd}
    #__bpMsg{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    #__bpCur{position:fixed;left:-40px;top:-40px;z-index:2147483646;pointer-events:none;transition:left .45s cubic-bezier(.3,.7,.3,1),top .45s cubic-bezier(.3,.7,.3,1)}
    #__bpCur span{position:absolute;left:20px;top:18px;background:#7b5cff;color:#fff;font:600 11px/1 -apple-system,Segoe UI,sans-serif;padding:3px 6px;border-radius:6px;white-space:nowrap}
    .__bpGlow{outline:3px solid #ffb84d !important;outline-offset:3px !important}`;
  function build() {
    if (document.getElementById("__bpBar") || !document.body) return;
    const st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
    const bar = document.createElement("div"); bar.id = "__bpBar";
    bar.innerHTML = `<span class="dot"></span><span id="__bpMsg">Bot Purge agent</span>
      <button id="__bpTake" class="ghost">Take over</button><button id="__bpGo">Resume</button>
      <button id="__bpSkip" class="ghost">Skip</button><button id="__bpDone">Done</button><button id="__bpStop" class="ghost">Stop</button>`;
    document.body.appendChild(bar);
    const cur = document.createElement("div"); cur.id = "__bpCur";
    cur.innerHTML = `<svg width="22" height="22" viewBox="0 0 24 24"><path d="M3 2l7.5 19 2.6-7.6L21 11z" fill="#9d7bff" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg><span>Bot Purge</span>`;
    document.body.appendChild(cur);
    for (const [id, type] of [["__bpTake", "takeover"], ["__bpGo", "resume"], ["__bpSkip", "skip"], ["__bpDone", "done"], ["__bpStop", "stop"]])
      bar.querySelector("#" + id).addEventListener("click", e => { e.stopPropagation(); signal({ type }); }, true);
    render();
  }
  function render() {
    const bar = document.getElementById("__bpBar"); if (!bar) return;
    bar.className = { paused: "paused", help: "help", rec: "rec" }[S.mode] || "";
    bar.style.display = S.mode === "off" ? "none" : "flex";
    document.getElementById("__bpMsg").textContent = S.msg || "";
    const show = (id, on) => { document.getElementById(id).style.display = on ? "" : "none"; };
    show("__bpTake", S.mode === "run"); show("__bpGo", S.mode === "paused" || S.mode === "help");
    show("__bpSkip", S.mode === "help"); show("__bpDone", S.mode === "rec"); show("__bpStop", S.mode !== "rec");
  }
  S.set = (mode, msg) => { S.mode = mode; S.msg = msg; build(); render(); };
  S.point = (x, y) => { build(); const c = document.getElementById("__bpCur"); if (c) { c.style.left = x + "px"; c.style.top = y + "px"; } };
  S.glow = sel => { document.querySelectorAll(".__bpGlow").forEach(e => e.classList.remove("__bpGlow")); };
  const inBar = e => e.target && e.target.closest && e.target.closest("#__bpBar");

  // What a person would call this element: the label a written step would use.
  function labelOf(el) {
    const t = el.closest("button,a,[role=button],[role=menuitem],[role=option],[role=tab],[role=radio],[role=checkbox],label,li,input,textarea,select") || el;
    const txt = (t.getAttribute("aria-label") || (t.innerText || "").trim().split("\n")[0] || t.getAttribute("title") ||
                 t.getAttribute("placeholder") || t.getAttribute("alt") || t.value || "").trim();
    return { text: txt.slice(0, 60), role: t.getAttribute("role") || t.tagName.toLowerCase(), placeholder: t.getAttribute("placeholder") || "",
             label: fieldLabel(t) };
  }
  function fieldLabel(el) {
    if (el.labels && el.labels[0]) return el.labels[0].innerText.trim().slice(0, 60);
    return (el.getAttribute && (el.getAttribute("aria-label") || el.getAttribute("name"))) || "";
  }
  const secret = el => /password|passcode|otp|one.?time|2fa|verif|code|pin|cvc|card/i.test(
    [el.type, el.name, el.id, el.autocomplete, el.getAttribute && el.getAttribute("aria-label"), el.placeholder].join(" "));

  // A person's own input (not the agent's) pauses the agent: whoever touches the window is in charge.
  const human = e => {
    if (S.acting || inBar(e)) return;
    if (S.mode === "run") signal({ type: "takeover" });
  };
  for (const t of ["mousedown", "keydown", "wheel"]) window.addEventListener(t, human, true);

  // Help and teach: watch what the person clicks (the click still goes through).
  window.addEventListener("click", e => {
    if (S.acting || inBar(e) || !e.isTrusted) return;
    const d = labelOf(e.target);
    if (S.mode === "help") signal({ type: "picked", target: d });
    else if (S.mode === "rec" && !["input", "textarea", "select"].includes(d.role)) signal({ type: "rec", step: { op: "click", text: d.text } });
  }, true);
  window.addEventListener("change", e => {
    if (S.mode !== "rec" || S.acting) return;
    const el = e.target;
    if (el.tagName === "SELECT") {
      signal({ type: "rec", step: { op: "select", option: el.options[el.selectedIndex].text.trim(), label: fieldLabel(el) } });
    } else if ((el.tagName === "INPUT" || el.tagName === "TEXTAREA") && !["checkbox", "radio", "file"].includes(el.type)) {
      if (secret(el)) { signal({ type: "rec", step: { op: "secret" } }); return; }
      signal({ type: "rec", step: { op: "type", value: el.value, placeholder: el.placeholder || "", label: fieldLabel(el) } });
    } else if (el.type === "checkbox" || el.type === "radio") {
      signal({ type: "rec", step: { op: "click", text: labelOf(el).label || labelOf(el).text } });
    }
  }, true);
  window.addEventListener("keydown", e => {
    if (S.mode !== "rec" || S.acting || inBar(e)) return;
    const special = ["Escape", "Enter", "Tab", "Delete", "Backspace", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End"];
    const mod = e.ctrlKey || e.metaKey || e.altKey;
    const inField = /INPUT|TEXTAREA/.test(e.target.tagName) || e.target.isContentEditable;
    if (!mod && !(special.includes(e.key) && (!inField || e.key === "Enter" || e.key === "Escape"))) return;
    if (inField && e.key === "Enter") {    // the value typed so far goes first, then the Enter
      const el = e.target; if (!secret(el)) signal({ type: "rec", step: { op: "type", value: el.value, placeholder: el.placeholder || "", label: fieldLabel(el), flush: true } });
    }
    const keys = [e.ctrlKey && "Ctrl", e.metaKey && "Cmd", e.altKey && "Alt", e.shiftKey && mod && "Shift",
                  e.key.length === 1 ? e.key.toUpperCase() : ({ Escape: "Esc" }[e.key] || e.key)].filter(Boolean);
    if (["Control", "Meta", "Alt", "Shift"].includes(e.key)) return;
    signal({ type: "rec", step: { op: "press", keys: keys.join("+") } });
  }, true);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build); else build();
})();
"""


class Stopped(Exception):
    """The person pressed Stop."""


class Skipped(Exception):
    """The person chose to skip the current account."""


@dataclass
class Cockpit:
    """Controls one agent browser context. Events from the page arrive through a binding."""

    context: object
    help_timeout: float = 300.0
    state: str = "run"                       # run | paused | help | rec | stopped
    picked: Optional[dict] = None
    recorded: list = field(default_factory=list)
    finished: bool = False
    skip: bool = False
    person: Optional[object] = None          # tests: a stand-in for the person, called while the agent waits
    msg: str = "Bot Purge agent is working…"
    _installed: bool = False
    _later: Optional[tuple] = None

    def _wait(self, page) -> None:
        self._flush()
        if self.person:
            self.person(page, self.state)
        page.wait_for_timeout(200)
        self._flush()

    def install(self) -> "Cockpit":
        if not self._installed:
            self.context.expose_binding("__bpSignal", lambda _src, m: self._on(m))
            self.context.add_init_script(OVERLAY)
            self.context.on("page", self._on_page)
            for p in self.context.pages:
                p.evaluate(OVERLAY)
            self._installed = True
        return self

    # events from the page ----------------------------------------------------------
    def _on(self, m: dict) -> None:
        # Runs inside the page's call, so it must not call back into the page: it only records
        # the change, and the bar is redrawn on the agent's next beat (_flush).
        t = m.get("type")
        if t == "takeover" and self.state == "run":
            self.state = "paused"
            self._later = ("paused", "You're in control. Press Resume to hand back to the agent.")
        elif t == "resume" and self.state in ("paused", "help"):
            self.state = "run"
            self._later = ("run", self.msg)
        elif t == "stop":
            self.state = "stopped"
            self._later = ("off", "")
        elif t == "skip" and self.state == "help":
            self.skip = True
        elif t == "picked" and self.state == "help":
            self.picked = m.get("target") or {}
        elif t == "rec" and self.state == "rec":
            self.recorded.append(m.get("step") or {})
        elif t == "done" and self.state == "rec":
            self.finished = True

    def _flush(self) -> None:
        if self._later:
            mode, msg = self._later
            self._later = None
            self._show(mode, msg)

    def _on_page(self, page) -> None:
        if self.state == "rec":
            self.recorded.append({"op": "switch", "to": "new"})
            page.on("close", lambda _p: self.state == "rec" and self.recorded.append({"op": "close"}))

    def _pages(self):
        return [p for p in self.context.pages if not p.is_closed()]

    def _show(self, mode: str, msg: str) -> None:
        for p in self._pages():
            try:
                p.evaluate("([m, t]) => window.__bp && window.__bp.set(m, t)", [mode, msg])
            except Exception:
                pass

    def say(self, msg: str) -> None:
        """Update the bar ("Removing @lucy1 · 3 of 12")."""
        self.msg = msg
        if self.state == "run":
            self._show("run", msg)

    # the agent's side --------------------------------------------------------------
    def checkpoint(self, page) -> None:
        """Between steps: wait while the person is in control; raise if they stopped."""
        self._flush()
        while self.state in ("paused", "help"):
            self._wait(page)
        self._flush()
        if self.state == "stopped":
            raise Stopped()

    def point_at(self, page, loc) -> None:
        """Glide the agent's visible cursor to what it's about to use."""
        try:
            b = loc.bounding_box()
            if b:
                page.evaluate("([x, y]) => window.__bp && window.__bp.point(x, y)", [b["x"] + b["width"] / 2, b["y"] + b["height"] / 2])
                page.wait_for_timeout(500)
        except Exception:
            pass

    def acting(self, page, on: bool) -> None:
        """Mark the agent's own clicks and keys so they don't count as the person taking over."""
        # Also re-applies the bar after a page load (each new page starts with it hidden).
        mode = self.state if self.state in ("run", "paused", "help", "rec") else "off"
        for p in self._pages() or [page]:
            try:
                p.evaluate("([v, m, t]) => { const b = window.__bp; if (!b) return; b.acting = v; if (b.mode !== m) b.set(m, t); }",
                           [on, mode, self.msg if mode == "run" else ""])
            except Exception:
                pass

    def ask_help(self, page, step: dict) -> Optional[dict]:
        """Ask the person to click the thing the agent couldn't find; returns what they clicked."""
        what = step.get("text") or step.get("option") or step.get("name") or " / ".join(step.get("any", [])[:3]) or "the next button"
        self.state, self.picked, self.skip = "help", None, False
        self._show("help", f"I can't find “{what}”. Click it for me, and I'll learn it for next time.")
        deadline = time.time() + self.help_timeout
        while self.picked is None and not self.skip and self.state == "help" and time.time() < deadline:
            self._wait(page)
        if self.state == "stopped":
            raise Stopped()
        if self.skip:
            self.state = "run"
            self._show("run", "Skipped. Agent working…")
            raise Skipped()
        picked, self.picked = self.picked, None
        if picked is not None:
            self.state = "run"
            self._show("run", "Thanks! Agent working…")
            page.wait_for_timeout(400)
        return picked

    # teach mode ----------------------------------------------------------------------
    def record(self, page, start_url: Optional[str] = None, timeout: float = 900.0) -> list[dict]:
        """Let the person demonstrate; returns the raw recorded steps when they press Done."""
        self.state, self.recorded, self.finished = "rec", [], False
        if start_url:
            page.goto(start_url, wait_until="domcontentloaded")
        self._show("rec", "Recording: do the steps once, then press Done. Passwords are never recorded.")
        page.on("dialog", lambda d: (self.recorded.append({"op": "dialog", "action": "accept"}), d.accept()))
        deadline = time.time() + timeout
        while not self.finished and self.state == "rec" and time.time() < deadline:
            live = [p for p in self._pages()]
            self._wait(live[-1] if live else page)
        stopped = self.state == "stopped"
        self.state = "run"
        self._show("off", "")
        if stopped:
            raise Stopped()
        return list(self.recorded)


# ---- turning a recording into steps people can read, edit and replay ----------------------

def _q(s: str) -> str:
    return '"' + s.replace('"', "'") + '"'


def steps_from_recording(raw: list[dict], values: Optional[dict] = None) -> list[str]:
    """Plain-language steps that ``compile_steps`` turns back into the same program.

    Values that match the demo account (its handle) become ``{handle}`` so the steps work for
    any account. Typing is merged per field; password and code fields are dropped.
    """
    values = values or {}
    handle = (values.get("handle") or "").lstrip("@").lower()
    lines: list[str] = []
    last_type_key = None
    typed: set = set()
    for s in raw:
        op = s.get("op")
        if op == "secret":
            continue
        if op == "type":
            val = s.get("value", "")
            if not val:
                continue
            if handle and val.lstrip("@").lower() == handle:
                val = "{handle}"
            box = s.get("placeholder") or s.get("label") or ""
            key = box.lower()
            if (key, val) in typed and last_type_key != key:
                continue
            typed.add((key, val))
            line = f"Type {_q(val)} in the {_q(box)} box." if box else f"Type {_q(val)}."
            if lines and last_type_key == key and lines[-1].startswith("Type "):
                lines[-1] = line                     # the field's final value, not every keystroke
            else:
                lines.append(line)
            last_type_key = key
            continue
        last_type_key = None
        if op == "click":
            t = (s.get("text") or "").strip()
            if t:
                if lines and lines[-1] == f"Click {_q(t)}.":
                    continue                          # a double click counts once
                lines.append(f"Click {_q(t)}.")
        elif op == "press":
            lines.append(f"Press {s['keys']}.")
        elif op == "select":
            lines.append(f"Select {_q(s['option'])} from the {_q(s['label'])} dropdown." if s.get("label") else f"Select {_q(s['option'])}.")
        elif op == "switch":
            lines.append("Switch to the new window.")
        elif op == "close":
            lines.append("Close this tab.")
            lines.append("Go back to the main window.")
        elif op == "dialog":
            lines.append("Accept the confirmation.")
    # A Type that is immediately followed by Press Enter was recorded twice (flush on Enter).
    out: list[str] = []
    for ln in lines:
        if out and ln == out[-1] and ln.startswith("Type "):
            continue
        out.append(ln)
    return ["Open their profile."] + out[:19]


def learned_program(program: list[dict], learned: dict[int, str]) -> list[dict]:
    """Add labels the person showed the agent as alternatives for those steps."""
    out = []
    for i, s in enumerate(program):
        s = dict(s)
        if i in learned and s.get("op") in ("click", "hover", "select", "expect"):
            label = learned[i]
            names = s.get("any") or [x for x in (s.get("text"), s.get("option")) if x]
            if label not in names:
                s["any"] = [label, *names]
                s.pop("text", None)
        out.append(s)
    return out


SAFE_LABEL = re.compile(r"^[^\n<>]{1,60}$")
