"""Graphical interface.

Views:
  Check          assessment and deviation from the baseline
  Licence        subscription / auth details readable from this machine
  Local data     inventory of the local history with per-entry deletion
  Working time   the timesheet the transcript timestamps add up to
  Observer view  what a mechanical triage over that data would surface
  Instructions   instruction files loaded into sessions

Meant for launching from a menu entry or by double-click, where terminal output
would vanish immediately. Tkinter is imported here and nowhere else, so the CLI
keeps working on machines without python3-tk.
"""

from __future__ import annotations

import json
import os
import subprocess
import webbrowser
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

from . import (about, core, data, instructions, license as licence, observer,
               worktime)
from .icons import SIZES, icon_png
from .i18n import (available_languages, current_language, save_preference,
                   set_language, t)

NAV_TABS = (
    ("check", "nav.check"),
    ("license", "nav.license"),
    ("data", "nav.data"),
    ("worktime", "nav.worktime"),
    ("observer", "nav.observer"),
    ("instructions", "nav.instructions"),
)

# The day log and the week list are long; these are what fits without the page
# turning into a spreadsheet. Both are stated in the interface, and the days
# have a button for the rest -- a cap nobody is told about reads as "that's all
# there was".
RECENT_DAYS = 14
RECENT_WEEKS = 12

LIGHT = {
    "bg": "#f2f3f5", "card": "#ffffff", "border": "#dcdfe3",
    "fg": "#1b1d20", "muted": "#6a7075", "accent": "#0f9b9b",
    "btn": "#ffffff", "btn_fg": "#1b1d20", "btn_active": "#e8eaed",
    "danger": "#c62828", "danger_bg": "#fdf0f0",
    "CRITICAL": "#c62828", "HIGH": "#e04f10", "MEDIUM": "#b8860b",
    "INFO": "#6a7075", "OK": "#2e7d32",
    "chip_fg": "#ffffff", "code_bg": "#f6f7f9",
}
DARK = {
    "bg": "#1e2124", "card": "#282b2f", "border": "#3a3e44",
    "fg": "#e8eaed", "muted": "#9aa0a6", "accent": "#2fd0d0",
    "btn": "#33373c", "btn_fg": "#e8eaed", "btn_active": "#43484e",
    "danger": "#ff6b6b", "danger_bg": "#332628",
    "CRITICAL": "#ff6b6b", "HIGH": "#ff9351", "MEDIUM": "#e8c05a",
    "INFO": "#9aa0a6", "OK": "#68d391",
    "chip_fg": "#1e2124", "code_bg": "#22252a",
}

# Instruction files fall into two kinds of thing: prose that shapes every
# answer, and packaged capabilities the assistant can invoke.
INSTRUCTION_GROUPS = (
    ("instructions.group.general", ("instructions", "memory")),
    ("instructions.group.capabilities", ("agent", "skill")),
)
# Organisation-pushed first -- that is the one nobody here wrote.
SCOPE_ORDER = {"org": 0, "user": 1, "project": 2, "parent": 3}


def _scope_rank(entry):
    return (SCOPE_ORDER.get(entry["scope"], 9), entry["name"])


STATUS_TONE = {"OK": "OK", "CHANGED": "MEDIUM", "CRITICAL": "CRITICAL",
               "NO_BASELINE": "INFO"}


def detect_dark():
    """Dark GTK theme? Decides the palette."""
    for schema, key in (("org.cinnamon.desktop.interface", "gtk-theme"),
                        ("org.gnome.desktop.interface", "gtk-theme")):
        try:
            out = subprocess.run(["gsettings", "get", schema, key],
                                 capture_output=True, text=True, timeout=3)
            if out.returncode == 0 and out.stdout.strip():
                return "dark" in out.stdout.lower()
        except (OSError, subprocess.SubprocessError):
            continue
    return False


class App:
    def __init__(self, root, start_view="check"):
        self.root = root
        self.c = DARK if detect_dark() else LIGHT
        self.result = None
        self.data = None
        self.report = None
        self.rules = None
        self.license = None
        self.worktime = None
        self.opened = set()   # instruction files with the body unfolded
        self.license_open = set()  # licence sections with details unfolded
        self.view = start_view
        self.expanded = set()        # projects with the session list unfolded
        self.all_days = False        # working time: whole log instead of recent
        self.wrappable = []          # labels whose wraplength follows resizing
        self.busy = False

        base = tkfont.nametofont("TkDefaultFont")
        family = base.actual("family")
        self.f_title = tkfont.Font(family=family, size=15, weight="bold")
        self.f_chip = tkfont.Font(family=family, size=10, weight="bold")
        self.f_head = tkfont.Font(family=family, size=11, weight="bold")
        self.f_body = tkfont.Font(family=family, size=10)
        self.f_small = tkfont.Font(family=family, size=9)
        self.f_mono = tkfont.Font(family=tkfont.nametofont("TkFixedFont").actual("family"),
                                  size=9)

        root.title(t("app.title"))
        root.geometry("880x680")
        root.minsize(700, 480)
        root.configure(bg=self.c["bg"])
        self._set_window_icon()

        self.lang_var = tk.StringVar(value=current_language())
        self._build_menu()
        self._build_header()
        self._build_nav()
        self._build_body()
        self._build_footer()

        root.bind("<F5>", lambda _e: self.refresh())
        root.bind("<Escape>", lambda _e: root.destroy())
        root.bind("<Next>", lambda _e: self.canvas.yview_scroll(1, "pages"))
        root.bind("<Prior>", lambda _e: self.canvas.yview_scroll(-1, "pages"))
        root.bind("<Home>", lambda _e: self.canvas.yview_moveto(0))
        root.bind("<End>", lambda _e: self.canvas.yview_moveto(1))
        root.bind("<Down>", lambda _e: self.canvas.yview_scroll(3, "units"))
        root.bind("<Up>", lambda _e: self.canvas.yview_scroll(-3, "units"))

        from . import install as app_install
        pending = app_install.take_gui_pending()
        if pending:
            self._start_with_install(pending)
        else:
            root.after(80, lambda: self.switch(self.view))

    def _set_window_icon(self):
        """Taskbar / window decoration: prefer several sizes for the WM."""
        photos = []
        for size in SIZES:
            path = icon_png(size)
            if path:
                try:
                    photos.append(tk.PhotoImage(file=path))
                except tk.TclError:
                    continue
        if not photos:
            return
        self.root.iconphoto(True, *photos)
        self._icon_photos = photos  # keep references alive

    def _start_with_install(self, pending):
        """Show a setup panel and run missing install steps with live status."""
        self.busy = True
        self.chip.configure(text=f"  {t('install.chip')}  ", bg=self.c["accent"])
        self.status_line.configure(text=t("install.status"))
        self.subtitle.configure(text=t("install.subtitle",
                                       count=len(pending)))
        self.btn_refresh.configure(state="disabled")
        self.btn_baseline.configure(state="disabled")

        self._clear_body()
        self._section(t("install.section"))
        card = self._card()
        self._install_log = tk.Label(
            card, text=t("install.progress.start"), font=self.f_body,
            bg=self.c["card"], fg=self.c["fg"], anchor="w", justify="left")
        self._install_log.pack(fill="x", padx=14, pady=(12, 4))
        self._install_detail = tk.Label(
            card, text="", font=self.f_small, bg=self.c["card"],
            fg=self.c["muted"], anchor="w", justify="left")
        self._install_detail.pack(fill="x", padx=14, pady=(0, 12))
        self.wrappable.append((self._install_log, 40))
        self.wrappable.append((self._install_detail, 40))
        self.foot_note.configure(text=t("install.footer"))

        lines = []

        def on_progress(message):
            lines.append(message)
            text = "\n".join(f"• {line}" for line in lines)

            def update():
                self._install_detail.configure(text=text)
                self._install_log.configure(text=message)
                self.status_line.configure(text=message)

            self.root.after(0, update)

        def work():
            from . import install as app_install
            error = None
            try:
                app_install.ensure(progress=on_progress)
            except Exception as exc:  # noqa: BLE001 -- shown in the UI
                error = exc
            self.root.after(0, lambda: self._install_done(error))

        threading.Thread(target=work, daemon=True).start()

    def _install_done(self, error):
        self.busy = False
        self.btn_refresh.configure(state="normal")
        if error is not None:
            self.chip.configure(text=f"  {t('status.error')}  ", bg=self.c["CRITICAL"])
            self.status_line.configure(text=str(error))
            messagebox.showerror(t("app.title"),
                                 t("install.failed", error=error))
        else:
            self.chip.configure(text=f"  {t('install.chip_done')}  ", bg=self.c["OK"])
            self.status_line.configure(text=t("install.progress.done"))
        self.switch(self.view)

    # ----------------------------------------------------------- structure

    def _build_menu(self):
        """Menu bar. Rebuilt wholesale when the language changes -- Tk cannot
        relabel a menu in place without bookkeeping that is not worth it here."""
        bar = tk.Menu(self.root)

        file_menu = tk.Menu(bar, tearoff=0)
        file_menu.add_command(label=t("menu.set_baseline"), command=self.set_baseline)
        file_menu.add_command(label=t("menu.copy_json"), command=self.copy_json)
        file_menu.add_separator()
        file_menu.add_command(label=t("menu.quit"), accelerator="Esc",
                              command=self.root.destroy)
        bar.add_cascade(label=t("menu.file"), menu=file_menu)

        view_menu = tk.Menu(bar, tearoff=0)
        view_menu.add_command(label=t("nav.check"), command=lambda: self.switch("check"))
        view_menu.add_command(label=t("nav.license"),
                              command=lambda: self.switch("license"))
        view_menu.add_command(label=t("nav.data"), command=lambda: self.switch("data"))
        view_menu.add_command(label=t("nav.worktime"),
                              command=lambda: self.switch("worktime"))
        view_menu.add_command(label=t("nav.observer"),
                              command=lambda: self.switch("observer"))
        view_menu.add_command(label=t("nav.instructions"),
                              command=lambda: self.switch("instructions"))
        view_menu.add_separator()
        view_menu.add_command(label=t("menu.reload"), accelerator="F5",
                              command=self.refresh)
        bar.add_cascade(label=t("menu.view"), menu=view_menu)

        lang_menu = tk.Menu(bar, tearoff=0)
        for code, label in available_languages():
            lang_menu.add_radiobutton(label=label, value=code, variable=self.lang_var,
                                      command=lambda c=code: self.change_language(c))
        bar.add_cascade(label=t("menu.language"), menu=lang_menu)

        help_menu = tk.Menu(bar, tearoff=0)
        help_menu.add_command(label=t("menu.about", app=about.APP_NAME),
                              command=self.show_about)
        bar.add_cascade(label=t("menu.help"), menu=help_menu)

        self.root.configure(menu=bar)
        self.menubar = bar

    def _build_header(self):
        head = tk.Frame(self.root, bg=self.c["card"], highlightthickness=0)
        head.pack(fill="x", side="top")
        inner = tk.Frame(head, bg=self.c["card"])
        inner.pack(fill="x", padx=20, pady=16)

        # Right column first -- otherwise the left one grabs all space with
        # expand=True and the status text overlaps the subtitle.
        right = tk.Frame(inner, bg=self.c["card"])
        right.pack(side="right", anchor="ne", padx=(24, 0))
        self.chip = tk.Label(right, text=" … ", font=self.f_chip,
                             bg=self.c["muted"], fg=self.c["chip_fg"], padx=12, pady=6)
        self.chip.pack(anchor="e")
        self.status_line = tk.Label(right, text="", font=self.f_small,
                                    bg=self.c["card"], fg=self.c["muted"],
                                    anchor="e", justify="right", wraplength=260)
        self.status_line.pack(anchor="e", pady=(6, 0))

        left = tk.Frame(inner, bg=self.c["card"])
        left.pack(side="left", fill="x", expand=True)
        self.title_label = tk.Label(left, text=t("app.title"), font=self.f_title,
                                    bg=self.c["card"], fg=self.c["fg"], anchor="w")
        self.title_label.pack(anchor="w")
        self.subtitle = tk.Label(left, text="…", font=self.f_small,
                                 bg=self.c["card"], fg=self.c["muted"], anchor="w",
                                 justify="left")
        self.subtitle.pack(anchor="w", pady=(3, 0))

    def _build_nav(self):
        nav = tk.Frame(self.root, bg=self.c["card"])
        nav.pack(fill="x", side="top")
        row = tk.Frame(nav, bg=self.c["card"])
        row.pack(fill="x", padx=20, pady=(0, 12))

        self.tabs = {}
        for key, label_key in NAV_TABS:
            # Tighter than it looks comfortable at: six tabs have to fit the
            # minimum window width, and a clipped tab is worse than a narrow one.
            btn = tk.Label(row, text=t(label_key), font=self.f_head, padx=9, pady=6,
                           cursor="hand2", bg=self.c["card"], fg=self.c["muted"])
            btn.pack(side="left", padx=(0, 3))
            btn.bind("<Button-1>", lambda _e, k=key: self.switch(k))
            self.tabs[key] = btn
        tk.Frame(self.root, bg=self.c["border"], height=1).pack(fill="x", side="top")

    def _build_body(self):
        wrap = tk.Frame(self.root, bg=self.c["bg"])
        wrap.pack(fill="both", expand=True)
        self.body_wrap = wrap        # carries the loading overlay
        self.overlay = None

        self.canvas = tk.Canvas(wrap, bg=self.c["bg"], highlightthickness=0, bd=0)
        bar = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.body = tk.Frame(self.canvas, bg=self.c["bg"])
        self.body_id = self.canvas.create_window((0, 0), window=self.body, anchor="nw")

        self.body.bind("<Configure>", lambda _e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        for seq, delta in (("<Button-4>", -1), ("<Button-5>", 1)):
            self.root.bind_all(seq,
                               lambda e, d=delta: self.canvas.yview_scroll(d * 3, "units"))
        self.root.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(
            -1 if e.delta > 0 else 1, "units"))

    def _build_footer(self):
        tk.Frame(self.root, bg=self.c["border"], height=1).pack(fill="x", side="bottom")
        foot = tk.Frame(self.root, bg=self.c["card"])
        foot.pack(fill="x", side="bottom")

        self.foot_note = tk.Label(foot, text="", font=self.f_small, bg=self.c["card"],
                                  fg=self.c["muted"], anchor="w", justify="left",
                                  wraplength=820)
        self.foot_note.pack(fill="x", padx=20, pady=(12, 8))
        self.wrappable.append((self.foot_note, 40))

        row = tk.Frame(foot, bg=self.c["card"])
        row.pack(fill="x", padx=20, pady=(0, 14))
        self.btn_refresh = self._button(row, "", self.refresh, primary=True)
        self.btn_refresh.pack(side="left")
        self.btn_baseline = self._button(row, t("btn.set_baseline"), self.set_baseline)
        self.btn_baseline.pack(side="left", padx=(8, 0))
        self.btn_json = self._button(row, t("btn.copy_json"), self.copy_json)
        self.btn_json.pack(side="left", padx=(8, 0))
        self.btn_close = self._button(row, t("btn.close"), self.root.destroy)
        self.btn_close.pack(side="right")

    def _button(self, parent, text, command, primary=False, danger=False):
        if primary:
            bg, fg = self.c["accent"], self.c["chip_fg"]
        elif danger:
            bg, fg = self.c["danger_bg"], self.c["danger"]
        else:
            bg, fg = self.c["btn"], self.c["btn_fg"]
        return tk.Button(parent, text=text, command=command, font=self.f_body,
                         bg=bg, fg=fg, activebackground=self.c["btn_active"],
                         activeforeground=self.c["btn_fg"], relief="flat",
                         highlightthickness=1, highlightbackground=self.c["border"],
                         padx=14, pady=7, cursor="hand2", bd=0)

    def _on_canvas_resize(self, event):
        self.canvas.itemconfigure(self.body_id, width=event.width)
        for label, margin in self.wrappable:
            label.configure(wraplength=max(240, event.width - margin))

    # ------------------------------------------------------------- language

    def change_language(self, code):
        if code == current_language():
            return
        set_language(code)
        save_preference(code)
        self.lang_var.set(code)
        self._build_menu()
        self.root.title(t("app.title"))
        self.title_label.configure(text=t("app.title"))
        for key, label_key in NAV_TABS:
            self.tabs[key].configure(text=t(label_key))
        self.btn_baseline.configure(text=t("btn.set_baseline"))
        self.btn_json.configure(text=t("btn.copy_json"))
        self.btn_close.configure(text=t("btn.close"))
        self.switch(self.view)

    # ----------------------------------------------------- loading overlay

    def show_loading(self, title, detail=""):
        """Centred card over the body while a view gathers its data.

        Without it the window comes up empty for as long as the collection
        takes -- on the observer sweep that is half a minute, which reads as a
        broken program rather than as work in progress.
        """
        if self.overlay is None:
            frame = tk.Frame(self.body_wrap, bg=self.c["border"])
            card = tk.Frame(frame, bg=self.c["card"])
            card.pack(padx=1, pady=1)
            self._overlay_title = tk.Label(card, text=title, font=self.f_head,
                                           bg=self.c["card"], fg=self.c["fg"])
            self._overlay_title.pack(padx=44, pady=(26, 6))
            self._overlay_detail = tk.Label(card, text=detail, font=self.f_small,
                                            bg=self.c["card"], fg=self.c["muted"],
                                            wraplength=340, justify="center")
            self._overlay_detail.pack(padx=44, pady=(0, 12))
            track = tk.Frame(card, bg=self.c["bg"], height=4, width=320)
            track.pack(padx=44, pady=(0, 26))
            track.pack_propagate(False)
            self._overlay_fill = tk.Frame(track, bg=self.c["accent"], height=4)
            self._overlay_fill.place(x=0, y=0, relwidth=0.18)
            self.overlay = frame
            self._overlay_step = 0
            self._overlay_determinate = False
            self._pulse_overlay()
        else:
            self._overlay_title.configure(text=title)
            self._overlay_detail.configure(text=detail)
        self.overlay.place(relx=0.5, rely=0.40, anchor="center")
        self.overlay.lift()

    def update_loading(self, detail, fraction=None):
        if self.overlay is None:
            return
        self._overlay_detail.configure(text=detail)
        if fraction is not None:
            # A real ratio arrived, so stop the indeterminate sweep.
            self._overlay_determinate = True
            self._overlay_fill.place_configure(
                relx=0, relwidth=max(0.02, min(1.0, fraction)))

    def hide_loading(self):
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None

    def _pulse_overlay(self):
        """Indeterminate sweep, until a real progress ratio takes over."""
        if self.overlay is None:
            return
        if not self._overlay_determinate:
            self._overlay_step = (self._overlay_step + 1) % 50
            self._overlay_fill.place_configure(relx=(self._overlay_step / 50) * 0.82)
        self.root.after(70, self._pulse_overlay)

    # ----------------------------------------------------------- fragments

    def _clear_body(self):
        self.wrappable = [(lbl, m) for lbl, m in self.wrappable
                          if not str(lbl).startswith(str(self.body))]
        for child in self.body.winfo_children():
            child.destroy()

    def _section(self, title, count_text=""):
        head = tk.Frame(self.body, bg=self.c["bg"])
        head.pack(fill="x", padx=20, pady=(18, 8))
        tk.Label(head, text=title.upper(), font=self.f_head, bg=self.c["bg"],
                 fg=self.c["fg"], anchor="w").pack(side="left")
        if count_text:
            tk.Label(head, text=count_text, font=self.f_small, bg=self.c["bg"],
                     fg=self.c["muted"], anchor="e").pack(side="right")

    def _card(self, pady=(0, 8)):
        outer = tk.Frame(self.body, bg=self.c["border"])
        outer.pack(fill="x", padx=20, pady=pady)
        card = tk.Frame(outer, bg=self.c["card"])
        card.pack(fill="both", padx=1, pady=1)
        return card

    def _ok_card(self, text):
        card = self._card()
        tk.Label(card, text="✓  " + text, font=self.f_body, bg=self.c["card"],
                 fg=self.c["OK"], anchor="w").pack(fill="x", padx=14, pady=12)

    def _note_card(self, text):
        card = self._card()
        lbl = tk.Label(card, text=text, font=self.f_body, bg=self.c["card"],
                       fg=self.c["muted"], anchor="w", justify="left")
        lbl.pack(fill="x", padx=14, pady=12)
        self.wrappable.append((lbl, 70))

    def _chip(self, parent, severity):
        return tk.Label(parent, text=f" {t('severity.' + severity)} ", font=self.f_chip,
                        bg=self.c[severity], fg=self.c["chip_fg"], padx=6, pady=2)

    # -------------------------------------------------------- view: check

    def render_check(self):
        self._clear_body()
        result = self.result
        if result is None:
            return

        tone = STATUS_TONE[result["status"]]
        self.chip.configure(text=f"  {t('status.' + result['status'])}  ", bg=self.c[tone])
        self.status_line.configure(text=t("status.detail." + result["status"]),
                                   fg=self.c["muted"])

        findings = result["findings"]
        self._section(t("section.assessment"),
                      t("count.findings", n=len(findings)) if findings else "")
        if findings:
            for f in findings:
                self._finding_card(f["severity"], core.finding_title(f),
                                   core.finding_detail(f))
        else:
            self._ok_card(t("result.no_monitoring"))

        changes = result["changes"]
        self._section(t("section.changes", time=result["baseline_time"] or "—"),
                      t("count.changes", n=len(changes)) if changes else "")
        if changes is None:
            self._note_card(t("baseline.none_yet"))
        elif changes:
            for ch in changes:
                self._change_card(ch)
        else:
            self._ok_card(t("result.no_changes"))

        hist = result["snapshot"].get("local_history") or {}
        if hist.get("transcript_files"):
            self._section(t("section.local_history"))
            self._note_card(
                t("history.summary", files=hist["transcript_files"],
                  mb=hist["megabytes"], oldest=hist["oldest"])
                + "\n" + t("history.hint_gui"))

        tk.Frame(self.body, bg=self.c["bg"], height=12).pack()

    def _finding_card(self, severity, title, detail):
        card = self._card()
        top = tk.Frame(card, bg=self.c["card"])
        top.pack(fill="x", padx=14, pady=(12, 4))
        self._chip(top, severity).pack(side="left")
        lbl_title = tk.Label(top, text=title, font=self.f_head, bg=self.c["card"],
                             fg=self.c["fg"], anchor="w", justify="left")
        lbl_title.pack(side="left", padx=(10, 0), fill="x", expand=True)
        self.wrappable.append((lbl_title, 160))

        lbl_detail = tk.Label(card, text=detail, font=self.f_body, bg=self.c["card"],
                              fg=self.c["muted"], anchor="w", justify="left")
        lbl_detail.pack(fill="x", padx=14, pady=(0, 12))
        self.wrappable.append((lbl_detail, 70))

    def _change_card(self, change):
        card = self._card()
        top = tk.Frame(card, bg=self.c["card"])
        top.pack(fill="x", padx=14, pady=(12, 6))
        self._chip(top, change["severity"]).pack(side="left")
        lbl_path = tk.Label(top, text=change["path"], font=self.f_mono,
                            bg=self.c["card"], fg=self.c["fg"], anchor="w", justify="left")
        lbl_path.pack(side="left", padx=(10, 0), fill="x", expand=True)
        self.wrappable.append((lbl_path, 160))

        for label, value, color in ((t("label.before"), change["before"], self.c["muted"]),
                                    (t("label.after"), change["after"], self.c["fg"])):
            row = tk.Frame(card, bg=self.c["code_bg"])
            row.pack(fill="x", padx=14, pady=(0, 4))
            tk.Label(row, text=label, font=self.f_small, bg=self.c["code_bg"],
                     fg=self.c["muted"], width=9, anchor="w").pack(side="left",
                                                                   padx=(8, 0), pady=6)
            val = tk.Label(row, text=str(value), font=self.f_mono, bg=self.c["code_bg"],
                           fg=color, anchor="w", justify="left")
            val.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=6)
            self.wrappable.append((val, 190))
        tk.Frame(card, bg=self.c["card"], height=8).pack()

    # ------------------------------------------------------ view: license

    def render_license(self):
        self._clear_body()
        report = self.license
        if report is None:
            return

        present = report["present"]
        if report.get("token_state") == "expired":
            tone, chip = "CRITICAL", "expired"
        elif present:
            tone, chip = "OK", "ok"
        else:
            tone, chip = "HIGH", "none"
        self.chip.configure(text=f"  {t('license.chip.' + chip)}  ", bg=self.c[tone])
        self.status_line.configure(text=t(licence.verdict_key(report)))
        self.subtitle.configure(
            text=self._account_line(report["account"], report.get("auth")) + "\n"
            + t("license.subtitle"))

        if not present:
            self._note_card(t("license.empty"))

        for section in report["sections"]:
            self._section(section["title"])
            self._license_section_card(section)

        self._section(t("license.section.raw"))
        self._note_card(t("license.raw_hint"))
        tk.Frame(self.body, bg=self.c["bg"], height=12).pack()

    def _license_section_card(self, section):
        card = self._card()
        top = tk.Frame(card, bg=self.c["card"])
        top.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(top, text=section["summary"], font=self.f_head, bg=self.c["card"],
                 fg=self.c["fg"], anchor="w").pack(side="left", fill="x", expand=True)
        open_ = section["id"] in self.license_open
        btn = self._button(
            top,
            t("btn.hide_details") if open_ else t("btn.show_details"),
            lambda s=section["id"]: self._toggle_license_section(s))
        btn.pack(side="right", padx=(12, 0))

        if not open_:
            preview = [r for r in section["rows"]
                       if r["raw"] not in (None, "", [], False)][:3]
            if not preview:
                preview = section["rows"][:2]
            for row in preview:
                line = tk.Frame(card, bg=self.c["card"])
                line.pack(fill="x", padx=14, pady=1)
                tk.Label(line, text=row["label"], font=self.f_small, bg=self.c["card"],
                         fg=self.c["muted"], width=22, anchor="w").pack(side="left")
                val = tk.Label(line, text=row["value"], font=self.f_small,
                               bg=self.c["card"], fg=self.c["fg"], anchor="w",
                               justify="left")
                val.pack(side="left", fill="x", expand=True)
                self.wrappable.append((val, 260))
            tk.Frame(card, bg=self.c["card"], height=10).pack()
            return

        box = tk.Frame(card, bg=self.c["code_bg"])
        box.pack(fill="x", padx=14, pady=(4, 12))
        for row in section["rows"]:
            line = tk.Frame(box, bg=self.c["code_bg"])
            line.pack(fill="x", padx=8, pady=2)
            tk.Label(line, text=row["label"], font=self.f_small, bg=self.c["code_bg"],
                     fg=self.c["muted"], width=22, anchor="w").pack(side="left")
            val = tk.Label(line, text=row["value"], font=self.f_mono,
                           bg=self.c["code_bg"], fg=self.c["fg"], anchor="w",
                           justify="left")
            val.pack(side="left", fill="x", expand=True)
            self.wrappable.append((val, 260))

    def _toggle_license_section(self, section_id):
        if section_id in self.license_open:
            self.license_open.discard(section_id)
        else:
            self.license_open.add(section_id)
        self.render_license()

    def reload_license(self):
        if self.busy:
            return
        self.busy = True
        self.show_loading(t("loading.license.title"), t("loading.license.detail"))
        self.chip.configure(text=f"  {t('status.reading')}  ", bg=self.c["muted"])
        self.btn_refresh.configure(state="disabled")

        def work():
            try:
                payload, error = licence.build_report(), None
            except Exception as exc:  # noqa: BLE001 -- surfaced in the UI
                payload, error = None, exc
            self.root.after(0, lambda: self._license_done(payload, error))

        threading.Thread(target=work, daemon=True).start()

    def _license_done(self, report, error):
        self.hide_loading()
        self.busy = False
        self.btn_refresh.configure(state="normal")
        if error is not None:
            messagebox.showerror(t("app.title"), t("error.license_failed", error=error))
            return
        self.license = report
        self.render_license()

    # --------------------------------------------------------- view: data

    def render_data(self):
        self._clear_body()
        inventory = self.data
        if inventory is None:
            return

        total = data.human_bytes(inventory["total_bytes"])
        self.chip.configure(text=f"  {t('chip.local', size=total)}  ", bg=self.c["accent"])
        self.status_line.configure(text=t("data.status_line"))
        if self.result is None:      # started directly in this view
            account, _mcp = core.collect_account_and_mcp()
            auth = core.collect_auth()
            self.subtitle.configure(text=self._account_line(account, auth)
                                    + "\n" + t("data.subtitle"))

        self._section(t("section.transcripts"),
                      t("count.projects", n=len(inventory["projects"])))
        if not inventory["projects"]:
            self._note_card(t("data.no_transcripts"))
        for project in inventory["projects"]:
            self._project_card(project)

        self._section(t("section.stores"))
        if not inventory["stores"]:
            self._note_card(t("data.no_stores"))
        for store in inventory["stores"]:
            self._store_card(store)

        self._section(t("section.everything"))
        card = self._card()
        row = tk.Frame(card, bg=self.c["card"])
        row.pack(fill="x", padx=14, pady=12)
        lbl = tk.Label(row, text=t("data.delete_all_line", size=total,
                                   projects=len(inventory["projects"]),
                                   stores=len(inventory["stores"])),
                       font=self.f_body, bg=self.c["card"], fg=self.c["fg"],
                       anchor="w", justify="left")
        lbl.pack(side="left", fill="x", expand=True)
        self.wrappable.append((lbl, 220))
        self._button(row, t("btn.delete_all"), self.delete_everything,
                     danger=True).pack(side="right", padx=(12, 0))

        # The delete note is already in the footer -- not repeated here.
        tk.Frame(self.body, bg=self.c["bg"], height=12).pack()

    def _project_card(self, project):
        card = self._card()
        row = tk.Frame(card, bg=self.c["card"])
        row.pack(fill="x", padx=14, pady=(12, 10))

        info = tk.Frame(row, bg=self.c["card"])
        info.pack(side="left", fill="x", expand=True)
        title_row = tk.Frame(info, bg=self.c["card"])
        title_row.pack(fill="x", anchor="w")
        lbl = tk.Label(title_row, text=project["label"], font=self.f_head,
                       bg=self.c["card"], fg=self.c["fg"], anchor="w", justify="left")
        lbl.pack(side="left")
        self.wrappable.append((lbl, 260))
        if project["has_active"]:
            tk.Label(title_row, text=f" {t('label.running')} ", font=self.f_small,
                     bg=self.c["MEDIUM"], fg=self.c["chip_fg"],
                     padx=6, pady=1).pack(side="left", padx=(8, 0))

        tk.Label(info, text=t("data.project_line", sessions=len(project["sessions"]),
                              size=data.human_bytes(project["bytes"]),
                              oldest=project["oldest"], newest=project["newest"]),
                 font=self.f_small, bg=self.c["card"], fg=self.c["muted"],
                 anchor="w").pack(fill="x", anchor="w", pady=(2, 0))

        actions = tk.Frame(row, bg=self.c["card"])
        actions.pack(side="right", padx=(12, 0))
        toggle_text = (t("btn.hide_sessions") if project["name"] in self.expanded
                       else t("btn.show_sessions"))
        self._button(actions, toggle_text,
                     lambda p=project["name"]: self.toggle(p)).pack(side="left")
        self._button(actions, t("btn.delete"), danger=True,
                     command=lambda p=project: self.delete_project(p)).pack(
                         side="left", padx=(8, 0))

        if project["name"] in self.expanded:
            for session in project["sessions"]:
                self._session_row(card, session)
            tk.Frame(card, bg=self.c["card"], height=8).pack()

    def _session_row(self, parent, session):
        row = tk.Frame(parent, bg=self.c["code_bg"])
        row.pack(fill="x", padx=14, pady=(0, 4))
        text = (f"{session['date']}  ·  {data.human_bytes(session['bytes']):>9}"
                f"  ·  {session['id']}")
        tk.Label(row, text=text, font=self.f_mono, bg=self.c["code_bg"],
                 fg=self.c["MEDIUM"] if session["active"] else self.c["fg"],
                 anchor="w").pack(side="left", padx=(10, 0), pady=6)
        btn = self._button(row, t("btn.delete"), danger=True,
                           command=lambda s=session: self.delete_session(s))
        btn.configure(font=self.f_small, padx=10, pady=3)
        btn.pack(side="right", padx=(8, 8), pady=4)
        if session["active"]:
            tk.Label(row, text=t("label.running"), font=self.f_small,
                     bg=self.c["code_bg"], fg=self.c["MEDIUM"]).pack(side="right", pady=6)

    def _store_card(self, store):
        card = self._card()
        row = tk.Frame(card, bg=self.c["card"])
        row.pack(fill="x", padx=14, pady=12)
        info = tk.Frame(row, bg=self.c["card"])
        info.pack(side="left", fill="x", expand=True)
        tk.Label(info, text=t(store["label_key"] + ".name"), font=self.f_head,
                 bg=self.c["card"], fg=self.c["fg"], anchor="w").pack(fill="x", anchor="w")
        tk.Label(info, text=t(store["label_key"] + ".desc") + " · "
                 + t("data.store_line", files=store["files"],
                     size=data.human_bytes(store["bytes"])),
                 font=self.f_small, bg=self.c["card"], fg=self.c["muted"],
                 anchor="w").pack(fill="x", anchor="w", pady=(2, 0))
        self._button(row, t("btn.delete"), danger=True,
                     command=lambda s=store: self.delete_store(s)).pack(
                         side="right", padx=(12, 0))

    # ----------------------------------------------------- view: worktime

    def render_worktime(self):
        report = self.worktime
        if report is None:
            return
        self._clear_body()

        total = worktime.human_minutes(report["total_active"])
        marked = bool(report["off_hours_active"] or report["weekend_active"])
        if not report["active_days"]:
            self.chip.configure(text=f"  {t('worktime.chip.empty')}  ", bg=self.c["INFO"])
        else:
            self.chip.configure(
                text=f"  {t('worktime.chip', hours=total, days=report['active_days'])}  ",
                bg=self.c["MEDIUM"] if marked else self.c["accent"])
        self.status_line.configure(text=t(worktime.verdict_key(report)))
        if self.result is None:      # started directly in this view
            account, _mcp = core.collect_account_and_mcp()
            auth = core.collect_auth()
            self.subtitle.configure(
                text=self._account_line(account, auth) + "\n"
                + t("worktime.subtitle", first=report["first_day"] or "—",
                    last=report["last_day"] or "—", sessions=report["sessions"],
                    stamps=report["stamps"]))

        if not report["active_days"]:
            self._section(t("worktime.section.overview"))
            self._note_card(t("worktime.empty"))
            return

        self._section(t("worktime.section.overview"))
        self._stat_tiles(self._worktime_tiles(report))
        self._note_card(t("worktime.method", gap=report["idle_gap"]) + "\n"
                        + t("worktime.method.floor"))

        self._section(t("worktime.section.weekday"))
        card = self._meter_card()
        peak = max(report["weekday_active"].values()) or 1
        for day in range(7):
            minutes = report["weekday_active"][day]
            self._meter(card, t(f"weekday.{day}"),
                        worktime.human_minutes(minutes) if minutes else "—",
                        minutes / peak, tone="MEDIUM" if day >= 5 else "accent",
                        label_width=4)
        tk.Frame(card, bg=self.c["card"], height=10).pack()
        self._note_card(t("worktime.weekday.note"))

        self._section(t("worktime.section.hours"))
        card = self._meter_card()
        peak = max(report["hour_active"].values()) or 1
        for hour in range(24):
            minutes = report["hour_active"][hour]
            off = (hour < report["business_start"] or hour >= report["business_end"])
            self._meter(card, f"{hour:02d}",
                        worktime.human_minutes(minutes) if minutes else "—",
                        minutes / peak, tone="MEDIUM" if off else "accent",
                        label_width=4)
        tk.Frame(card, bg=self.c["card"], height=10).pack()
        self._note_card(t("worktime.hours.note", start=report["business_start"],
                          end=report["business_end"]))

        weeks = report["weeks"][-RECENT_WEEKS:]
        self._section(t("worktime.section.weeks"))
        card = self._meter_card()
        peak = max(w["active"] for w in weeks) or 1
        for week in weeks:
            over = week["active"] > report["week_target"] * 60
            self._meter(card, t("worktime.weeks.label", week=week["week"]),
                        worktime.human_minutes(week["active"]) + "  ·  "
                        + t("worktime.short.days", n=week["days"]),
                        week["active"] / peak,
                        tone="MEDIUM" if over else "accent", label_width=8)
        tk.Frame(card, bg=self.c["card"], height=10).pack()
        self._note_card(t("worktime.weeks.note", shown=len(weeks),
                          total=len(report["weeks"]),
                          target=f"{report['week_target']:.0f}",
                          over=report["long_weeks"]))

        days = report["days"] if self.all_days else report["days"][:RECENT_DAYS]
        self._section(t("worktime.section.days"))
        card = self._card()
        tk.Frame(card, bg=self.c["card"], height=8).pack()
        for day in days:
            self._worktime_day_row(card, day)
        if len(report["days"]) > RECENT_DAYS:
            row = tk.Frame(card, bg=self.c["card"])
            row.pack(fill="x", padx=14, pady=(6, 12))
            self._button(row, t("btn.show_recent_days") if self.all_days
                         else t("btn.show_all_days"),
                         self.toggle_all_days).pack(side="left")
        else:
            tk.Frame(card, bg=self.c["card"], height=8).pack()
        self._note_card(t("worktime.days.note", shown=len(days),
                          total=len(report["days"])))

        self._section(t("worktime.section.projects"),
                      t("count.projects", n=len(report["projects"])))
        card = self._meter_card()
        peak = report["projects"][0]["active"] or 1
        for project in report["projects"]:
            # The compact day count, not the sentence the terminal prints -- the
            # value column is a fixed width and a long one gets cut off.
            self._meter(card, os.path.basename(project["label"]) or project["label"],
                        worktime.human_minutes(project["active"]) + "  ·  "
                        + t("worktime.short.days", n=project["days"]),
                        project["active"] / peak, label_width=20)
        tk.Frame(card, bg=self.c["card"], height=10).pack()
        self._note_card(t("worktime.projects.note"))

        self._note_card(t("worktime.note.clock", tab=t("nav.data")))
        tk.Frame(self.body, bg=self.c["bg"], height=12).pack()

    def _worktime_tiles(self, report):
        """The headline figures. Neutral unless the number is the point."""
        longest = report["longest_day"] or {"date": "—", "active": 0}
        earliest = report["earliest_start"] or {"date": "—", "time": "—"}
        latest = report["latest_end"] or {"date": "—", "time": "—"}
        night = report["off_hours_active"]
        weekend = report["weekend_active"]
        return [
            (worktime.human_minutes(report["total_active"]),
             t("worktime.stat.total"), "fg"),
            (str(report["active_days"]), t("worktime.stat.days"), "fg"),
            (worktime.human_minutes(report["average_active"]),
             t("worktime.stat.average"), "fg"),
            (worktime.human_minutes(report["median_active"]),
             t("worktime.stat.median"), "fg"),
            (worktime.human_minutes(longest["active"]),
             t("worktime.stat.longest", date=longest["date"]), "fg"),
            (worktime.human_minutes(report["longest_block"]),
             t("worktime.stat.block"), "fg"),
            (earliest["time"], t("worktime.stat.earliest", date=earliest["date"]), "fg"),
            (latest["time"], t("worktime.stat.latest", date=latest["date"]), "fg"),
            (worktime.human_minutes(report["total_pause"]),
             t("worktime.stat.pause"), "fg"),
            (worktime.human_minutes(night),
             t("worktime.stat.offhours", start=report["business_start"],
               end=report["business_end"]), "MEDIUM" if night else "fg"),
            (worktime.human_minutes(weekend), t("worktime.stat.weekend"),
             "MEDIUM" if weekend else "fg"),
            (str(report["total_prompts"]), t("worktime.stat.prompts"), "fg"),
        ]

    def _stat_tiles(self, tiles, columns=3):
        """Figure over label, in a grid.

        The values wear text colours, not chart colours -- they are not a series.
        A tile turns to the warning tone only where the number itself is the
        finding, which is the one thing this view is pointing at.
        """
        card = self._card()
        grid = tk.Frame(card, bg=self.c["card"])
        grid.pack(fill="x", padx=14, pady=(14, 4))
        for column in range(columns):
            grid.columnconfigure(column, weight=1, uniform="stat")
        for index, (value, label, tone) in enumerate(tiles):
            cell = tk.Frame(grid, bg=self.c["card"])
            cell.grid(row=index // columns, column=index % columns, sticky="ew",
                      pady=(0, 12), padx=(0, 12))
            tk.Label(cell, text=value, font=self.f_title, bg=self.c["card"],
                     fg=self.c[tone], anchor="w").pack(fill="x", anchor="w")
            caption = tk.Label(cell, text=label, font=self.f_small, bg=self.c["card"],
                               fg=self.c["muted"], anchor="w", justify="left")
            caption.pack(fill="x", anchor="w")
            self.wrappable.append((caption, 120 + 260 * (columns - 1)))

    def _meter_card(self):
        card = self._card()
        tk.Frame(card, bg=self.c["card"], height=10).pack()
        return card

    def _meter(self, parent, label, value, fraction, tone="accent", label_width=6):
        """One bar: label, track, value.

        Magnitude, so a single hue throughout; the reserved warning tone marks
        the rows a section is pointing at (weekend, hours outside the window, a
        week over the nominal target) and nothing else.
        """
        row = tk.Frame(parent, bg=self.c["card"])
        row.pack(fill="x", padx=14, pady=(0, 3))
        tk.Label(row, text=label, font=self.f_small, bg=self.c["card"],
                 fg=self.c["fg"], width=label_width, anchor="w").pack(side="left")
        tk.Label(row, text=value, font=self.f_mono, bg=self.c["card"],
                 fg=self.c["muted"], width=20, anchor="e").pack(side="right")
        track = tk.Frame(row, bg=self.c["bg"], height=10)
        track.pack(side="left", fill="x", expand=True, padx=(8, 10))
        track.pack_propagate(False)
        if fraction > 0:
            tk.Frame(track, bg=self.c[tone], height=10).place(
                x=0, y=0, relwidth=min(1.0, max(0.015, fraction)), relheight=1)

    def _worktime_day_row(self, parent, day):
        row = tk.Frame(parent, bg=self.c["code_bg"])
        row.pack(fill="x", padx=14, pady=(0, 4))

        head = tk.Frame(row, bg=self.c["code_bg"])
        head.pack(fill="x", padx=10, pady=(7, 0))
        tk.Label(head, text=f"{day['date']}  {t('weekday.' + str(day['weekday']))}",
                 font=self.f_mono, bg=self.c["code_bg"], fg=self.c["fg"],
                 anchor="w").pack(side="left")
        for flag, key in ((day["weekend"], "worktime.day.weekend"),
                          (day["off_hours"], "worktime.day.night")):
            if flag:
                tk.Label(head, text=f" {t(key)} ", font=self.f_small,
                         bg=self.c["MEDIUM"], fg=self.c["chip_fg"], padx=5,
                         pady=1).pack(side="right", padx=(6, 0))

        line = tk.Label(row, text=t("worktime.day.line", start=day["start"],
                                    end=day["end"],
                                    active=worktime.human_minutes(day["active"]),
                                    pause=worktime.human_minutes(day["pause"]),
                                    blocks=day["blocks"]),
                        font=self.f_small, bg=self.c["code_bg"], fg=self.c["fg"],
                        anchor="w", justify="left")
        line.pack(fill="x", padx=10, pady=(2, 0))
        self.wrappable.append((line, 90))

        projects = ", ".join(os.path.basename(p) or p for p in day["projects"])
        meta = tk.Label(row, text=t("worktime.day.meta", prompts=day["prompts"],
                                    projects=projects or "—"),
                        font=self.f_small, bg=self.c["code_bg"], fg=self.c["muted"],
                        anchor="w", justify="left")
        meta.pack(fill="x", padx=10, pady=(0, 7))
        self.wrappable.append((meta, 90))

    def toggle_all_days(self):
        self.all_days = not self.all_days
        self.render_worktime()
        self.canvas.yview_moveto(0)

    def reload_worktime(self):
        if self.busy:
            return
        self.busy = True
        self.show_loading(t("loading.worktime.title"), t("loading.worktime.detail"))
        self.chip.configure(text=f"  {t('status.reading')}  ", bg=self.c["muted"])
        self.btn_refresh.configure(state="disabled")

        def note(done, total):
            # Called from the worker thread; hand it to Tk's thread.
            text = t("observer.scanning", done=done, total=total)
            share = (done / total) if total else None
            self.root.after(0, lambda: (self.status_line.configure(text=text),
                                        self.update_loading(text, share)))

        def work():
            try:
                payload, error = worktime.build_report(progress=note), None
            except Exception as exc:               # noqa: BLE001 -- surfaced in the UI
                payload, error = None, exc
            self.root.after(0, lambda: self._worktime_done(payload, error))

        threading.Thread(target=work, daemon=True).start()

    def _worktime_done(self, report, error):
        self.hide_loading()
        self.busy = False
        self.btn_refresh.configure(state="normal")
        if error is not None:
            messagebox.showerror(t("app.title"), t("error.data_failed", error=error))
            return
        self.worktime = report
        self.render_worktime()

    # ----------------------------------------------------- view: observer

    def render_observer(self):
        self._clear_body()
        report = self.report
        if report is None:
            return

        verdict = observer.verdict_key(report)
        tone = "CRITICAL" if verdict.endswith("credentials") else \
               "MEDIUM" if verdict.endswith("mentions") else "OK"
        self.chip.configure(text=f"  {t('observer.chip')}  ", bg=self.c[tone])
        self.status_line.configure(text=t(verdict))
        self.subtitle.configure(
            text=self._account_line(report["account"], core.collect_auth()) + "\n"
            + t("observer.pattern.line",
                sessions=report["sessions"],
                days=report["active_days"],
                size=data.human_bytes(report["bytes"])))

        self._section(t("observer.section.identity"))
        account = report["account"]
        plan = core.plan_label(account)
        self._note_card(
            t("observer.identity.line", org=account.get("organizationName", "—"),
              plan=plan, role=account.get("organizationRole", "—"),
              email=account.get("emailAddress", "—"))
            + "\n" + t("observer.identity.note"))

        # No list here any more. The inventory -- names, sizes, dates -- belongs
        # to the history view; repeating it made the two tabs look like the same
        # thing. What stays is the point this view is making: the directory
        # names give the topics away before a single file is opened.
        self._section(t("observer.section.projects"),
                      t("count.projects", n=len(report["projects"])))
        self._note_card(t("observer.projects.note") + "\n"
                        + t("observer.projects.pointer", tab=t("nav.data")))

        self._section(t("observer.section.pattern"))
        self._note_card(
            t("observer.pattern.hours", start=observer.BUSINESS_START,
              end=observer.BUSINESS_END, count=report["off_hours"])
            + " · " + t("observer.pattern.weekend", count=report["weekend"])
            + "\n" + t("observer.pattern.note")
            + "\n" + t("observer.pattern.pointer", tab=t("nav.worktime")))

        self._section(t("observer.section.sweep"))
        for category in report["categories"]:
            self._sweep_card(category)
        self._note_card(t("observer.sweep.note"))
        tk.Frame(self.body, bg=self.c["bg"], height=12).pack()

    def _sweep_card(self, category):
        card = self._card()
        top = tk.Frame(card, bg=self.c["card"])
        top.pack(fill="x", padx=14, pady=(12, 4))
        hit = bool(category["count"])
        tone = ("CRITICAL" if category["confidence"] == "high" else "MEDIUM") \
            if hit else "OK"
        tk.Label(top, text=" ! " if hit else " ✓ ", font=self.f_chip,
                 bg=self.c[tone], fg=self.c["chip_fg"], padx=4, pady=2).pack(side="left")
        tk.Label(top, text=t(category["key"]), font=self.f_head, bg=self.c["card"],
                 fg=self.c["fg"], anchor="w").pack(side="left", padx=(10, 0))

        summary = (t("observer.sweep.hit", count=category["count"],
                     sessions=category["sessions"])
                   + "  (" + t("observer.conf." + category["confidence"]) + ")"
                   if hit else t("observer.sweep.clean"))
        tk.Label(card, text=summary, font=self.f_small, bg=self.c["card"],
                 fg=self.c["muted"], anchor="w").pack(fill="x", padx=14, pady=(0, 8))

        for sample in category["samples"]:
            row = tk.Frame(card, bg=self.c["code_bg"])
            row.pack(fill="x", padx=14, pady=(0, 4))
            tk.Label(row, text=f"{sample['date']}  {sample['project']}",
                     font=self.f_small, bg=self.c["code_bg"], fg=self.c["muted"],
                     anchor="w").pack(fill="x", padx=8, pady=(6, 0))
            excerpt = tk.Label(row, text=sample["excerpt"], font=self.f_mono,
                               bg=self.c["code_bg"], fg=self.c["fg"], anchor="w",
                               justify="left")
            excerpt.pack(fill="x", padx=8, pady=(0, 6))
            self.wrappable.append((excerpt, 90))
        tk.Frame(card, bg=self.c["card"], height=6).pack()

    def reload_observer(self):
        if self.busy:
            return
        self.busy = True
        self.show_loading(t("loading.observer.title"), t("loading.observer.detail"))
        self.btn_refresh.configure(state="disabled")
        self.chip.configure(text=f"  {t('status.reading')}  ", bg=self.c["muted"])

        def note(done, total):
            # Called from the worker thread; hand it to Tk's thread.
            text = t("observer.scanning", done=done, total=total)
            share = (done / total) if total else None
            self.root.after(0, lambda: (self.status_line.configure(text=text),
                                        self.update_loading(text, share)))

        def work():
            try:
                payload, error = observer.build_report(progress=note), None
            except Exception as exc:               # noqa: BLE001 -- surfaced in the UI
                payload, error = None, exc
            self.root.after(0, lambda: self._observer_done(payload, error))

        threading.Thread(target=work, daemon=True).start()

    def _observer_done(self, report, error):
        self.hide_loading()
        self.busy = False
        self.btn_refresh.configure(state="normal")
        if error is not None:
            messagebox.showerror(t("app.title"), t("error.data_failed", error=error))
            return
        self.report = report
        self.render_observer()

    # -------------------------------------------------- view: instructions

    def render_rules(self):
        self._clear_body()
        report = self.rules
        if report is None:
            return
        tone = "CRITICAL" if report["org_controlled"] else \
               "HIGH" if report["foreign_owner"] else "OK"
        self.chip.configure(
            text="  " + t("instructions.summary", count=len(report["entries"]),
                          size=data.human_bytes(report["total_bytes"])) + "  ",
            bg=self.c[tone])
        warnings = []
        if report["org_controlled"]:
            warnings.append(t("instructions.org_warn", count=report["org_controlled"]))
        if report["foreign_owner"]:
            warnings.append(t("instructions.foreign_warn",
                              count=report["foreign_owner"]))
        self.status_line.configure(text=" ".join(warnings))
        if self.result is None:      # started directly in this view
            account, _mcp = core.collect_account_and_mcp()
            auth = core.collect_auth()
            self.subtitle.configure(text=self._account_line(account, auth) + "\n"
                                    + t("instructions.intro")[:110] + "…")

        if not report["entries"]:
            self._section(t("nav.instructions"))
            self._note_card(t("instructions.none"))
            return

        # Grouped by what a file *is*, not by where it sits. Prose that steers
        # every answer and a packaged capability are different things; the scope
        # (organisation / user / project) rides along on each card instead.
        for group, kinds in INSTRUCTION_GROUPS:
            entries = [e for e in report["entries"] if e["kind"] in kinds]
            if not entries:
                continue
            self._section(t(group), t("count.files", n=len(entries)))
            for entry in sorted(entries, key=_scope_rank):
                self._rule_card(entry)
        leftover = [e for e in report["entries"]
                    if not any(e["kind"] in kinds for _g, kinds in INSTRUCTION_GROUPS)]
        if leftover:
            self._section(t("instructions.group.other"),
                          t("count.files", n=len(leftover)))
            for entry in sorted(leftover, key=_scope_rank):
                self._rule_card(entry)
        tk.Frame(self.body, bg=self.c["bg"], height=12).pack()

    def _rule_card(self, entry):
        card = self._card()
        row = tk.Frame(card, bg=self.c["card"])
        row.pack(fill="x", padx=14, pady=(12, 10))
        info = tk.Frame(row, bg=self.c["card"])
        info.pack(side="left", fill="x", expand=True)

        head = tk.Frame(info, bg=self.c["card"])
        head.pack(fill="x", anchor="w")
        tone = "CRITICAL" if entry["origin"] == "org" else \
               "HIGH" if entry["foreign"] else "INFO"
        tk.Label(head, text=f" {t('instructions.kind.' + entry['kind'])} ",
                 font=self.f_small, bg=self.c[tone], fg=self.c["chip_fg"],
                 padx=6, pady=1).pack(side="left")
        tk.Label(head, text=t("instructions.scope." + entry["scope"]),
                 font=self.f_small, bg=self.c["card"],
                 fg=self.c["muted"]).pack(side="left", padx=(6, 0))
        name = tk.Label(head, text=entry["name"], font=self.f_head,
                        bg=self.c["card"], fg=self.c["fg"], anchor="w")
        name.pack(side="left", padx=(8, 0))

        path = tk.Label(info, text=entry["path"], font=self.f_mono,
                        bg=self.c["card"], fg=self.c["muted"], anchor="w",
                        justify="left")
        path.pack(fill="x", anchor="w", pady=(3, 0))
        self.wrappable.append((path, 260))
        tk.Label(info, text=t("instructions.meta",
                              size=data.human_bytes(entry["bytes"]),
                              modified=entry["modified"], owner=entry["owner"]),
                 font=self.f_small, bg=self.c["card"], fg=self.c["muted"],
                 anchor="w").pack(fill="x", anchor="w", pady=(2, 0))

        opened = entry["path"] in self.opened
        self._button(row, t("btn.hide_content") if opened else t("btn.show_content"),
                     lambda p=entry["path"]: self.toggle_rule(p)).pack(
                         side="right", padx=(12, 0))

        if opened:
            box = tk.Frame(card, bg=self.c["code_bg"])
            box.pack(fill="x", padx=14, pady=(0, 12))
            body = entry["preview"] + (
                "\n" + t("instructions.truncated") if entry["truncated"] else "")
            content = tk.Label(box, text=body, font=self.f_mono, bg=self.c["code_bg"],
                               fg=self.c["fg"], anchor="w", justify="left")
            content.pack(fill="x", padx=10, pady=8)
            self.wrappable.append((content, 100))

    def toggle_rule(self, path):
        self.opened.symmetric_difference_update({path})
        self.render_rules()

    def reload_rules(self):
        if self.busy:
            return
        self.busy = True
        self.show_loading(t("loading.instructions.title"), t("loading.instructions.detail"))
        self.btn_refresh.configure(state="disabled")
        self.chip.configure(text=f"  {t('status.reading')}  ", bg=self.c["muted"])
        baseline = core.load_baseline()
        projects = (baseline or {}).get("project_dirs") or [os.getcwd()]

        def work():
            try:
                payload, error = instructions.collect(projects), None
            except Exception as exc:               # noqa: BLE001 -- surfaced in the UI
                payload, error = None, exc
            self.root.after(0, lambda: self._rules_done(payload, error))

        threading.Thread(target=work, daemon=True).start()

    def _rules_done(self, report, error):
        self.hide_loading()
        self.busy = False
        self.btn_refresh.configure(state="normal")
        if error is not None:
            messagebox.showerror(t("app.title"), t("error.data_failed", error=error))
            return
        self.rules = report
        self.render_rules()

    # ------------------------------------------------------------- actions

    def _account_line(self, account, auth=None):
        org = (account.get("organizationName")
               or account.get("emailAddress")
               or account.get("displayName")
               or "—")
        return t("header.account", org=org, plan=core.plan_label(account, auth),
                 role=account.get("organizationRole", "—"))

    def switch(self, view):
        self.view = view
        for key, btn in self.tabs.items():
            active = key == view
            btn.configure(fg=self.c["fg"] if active else self.c["muted"],
                          bg=self.c["btn_active"] if active else self.c["card"])
        self.btn_baseline.configure(state="normal" if view == "check" else "disabled")
        self.foot_note.configure(
            text=t("caveat.server_export") if view == "check"
            else t("license.intro") if view == "license"
            else t("worktime.intro") if view == "worktime"
            else t("observer.intro") if view == "observer"
            else t("instructions.intro") if view == "instructions"
            else t("delete.note"))
        self.btn_refresh.configure(text=t("btn.recheck") if view == "check"
                                   else t("btn.reload"))
        self.btn_baseline.configure(state="normal" if view == "check" else "disabled")
        if view == "data":
            self.reload_data() if self.data is None else self.render_data()
        elif view == "worktime":
            self.reload_worktime() if self.worktime is None else self.render_worktime()
        elif view == "license":
            self.reload_license() if self.license is None else self.render_license()
        elif view == "observer":
            self.reload_observer() if self.report is None else self.render_observer()
        elif view == "instructions":
            self.reload_rules() if self.rules is None else self.render_rules()
        else:
            self.refresh() if self.result is None else self.render_check()
        self.canvas.yview_moveto(0)

    def refresh(self):
        if self.view == "data":
            self.reload_data()
            return
        if self.view == "worktime":
            self.worktime = None
            self.reload_worktime()
            return
        if self.view == "license":
            self.license = None
            self.reload_license()
            return
        if self.view == "observer":
            self.report = None
            self.reload_observer()
            return
        if self.view == "instructions":
            self.rules = None
            self.reload_rules()
            return
        if self.busy:
            return
        self.busy = True
        self.show_loading(t("loading.check.title"), t("loading.check.detail"))
        self.chip.configure(text=f"  {t('status.checking')}  ", bg=self.c["muted"])
        self.status_line.configure(text=t("status.checking.detail"))
        self.btn_refresh.configure(state="disabled")

        def work():
            try:
                payload, error = core.run_check(), None
            except Exception as exc:               # noqa: BLE001 -- surfaced in the UI
                payload, error = None, exc
            self.root.after(0, lambda: self._check_done(payload, error))

        threading.Thread(target=work, daemon=True).start()

    def _check_done(self, result, error):
        self.hide_loading()
        self.busy = False
        self.btn_refresh.configure(state="normal")
        if error is not None:
            self.chip.configure(text=f"  {t('status.error')}  ", bg=self.c["CRITICAL"])
            self.status_line.configure(text=str(error))
            messagebox.showerror(t("app.title"), t("error.check_failed", error=error))
            return
        self.result = result
        snapshot = result["snapshot"]
        self.subtitle.configure(
            text=self._account_line(snapshot["account"], snapshot.get("auth")) + "\n"
            + t("header.baseline", time=result["baseline_time"] or t("value.none"),
                projects=", ".join(snapshot["project_dirs"]) or "—"))
        if self.view == "check":
            self.render_check()

    def reload_data(self):
        if self.busy:
            return
        self.busy = True
        self.show_loading(t("loading.data.title"), t("loading.data.detail"))
        self.chip.configure(text=f"  {t('status.reading')}  ", bg=self.c["muted"])
        self.btn_refresh.configure(state="disabled")

        def work():
            try:
                payload, error = data.list_local_data(), None
            except Exception as exc:               # noqa: BLE001 -- surfaced in the UI
                payload, error = None, exc
            self.root.after(0, lambda: self._data_done(payload, error))

        threading.Thread(target=work, daemon=True).start()

    def _data_done(self, inventory, error):
        self.hide_loading()
        self.busy = False
        self.btn_refresh.configure(state="normal")
        if error is not None:
            messagebox.showerror(t("app.title"), t("error.data_failed", error=error))
            return
        self.data = inventory
        self.render_data()

    def toggle(self, name):
        self.expanded.symmetric_difference_update({name})
        self.render_data()

    # -------------------------------------------------------------- delete

    def _confirm_delete(self, headline, detail, paths, warning=None):
        parts = [headline, detail]
        if warning:
            parts.append("⚠  " + warning)
        parts += [t("delete.note"), t("delete.confirm")]
        if not messagebox.askyesno(t("app.title"), "\n\n".join(parts),
                                   icon="warning", default="no"):
            return
        deleted, errors = data.delete_paths(paths)
        if errors:
            lines = "\n".join(
                f"• {os.path.basename(p)}: "
                f"{t(e.key, **e.params) if isinstance(e, data.NotDeletable) else e}"
                for p, e in errors[:8])
            messagebox.showerror(t("app.title"),
                                 t("delete.partial", done=deleted, total=len(paths))
                                 + "\n\n" + lines)
        else:
            self.status_line.configure(text=t("delete.done", count=deleted))
        self.reload_data()

    def delete_project(self, project):
        self._confirm_delete(
            t("delete.project.title", label=project["label"]),
            t("data.project_line", sessions=len(project["sessions"]),
              size=data.human_bytes(project["bytes"]),
              oldest=project["oldest"], newest=project["newest"]),
            [project["path"]],
            t("delete.warn.project_active") if project["has_active"] else None)

    def delete_session(self, session):
        self._confirm_delete(
            t("delete.session.title"),
            f"{session['id']}\n{session['date']} · {data.human_bytes(session['bytes'])}",
            [session["path"]],
            t("delete.warn.session_active") if session["active"] else None)

    def delete_store(self, store):
        self._confirm_delete(
            t("delete.store.title", label=t(store["label_key"] + ".name")),
            t(store["label_key"] + ".desc") + "\n"
            + t("data.store_line", files=store["files"],
                size=data.human_bytes(store["bytes"])),
            [store["path"]])

    def delete_everything(self):
        inventory = self.data
        paths = ([p["path"] for p in inventory["projects"]]
                 + [s["path"] for s in inventory["stores"]])
        warning = t("delete.warn.irreversible")
        if inventory["active_sessions"]:
            warning += " " + t("delete.warn.sessions_running")
        self._confirm_delete(
            t("delete.all.title"),
            t("delete.all.detail", projects=len(inventory["projects"]),
              stores=len(inventory["stores"]),
              size=data.human_bytes(inventory["total_bytes"])),
            paths, warning)

    # --------------------------------------------------------------- misc

    def set_baseline(self):
        if self.result is None:
            return
        existing = core.load_baseline()
        known = (existing or {}).get("project_dirs") or []
        if not messagebox.askyesno(t("app.title"), t("baseline.confirm"),
                                   icon="warning", default="no"):
            return
        try:
            core.save_baseline(core.collect(known))
        except OSError as exc:
            messagebox.showerror(t("app.title"), t("baseline.write_failed", error=exc))
            return
        self.refresh()

    def show_about(self):
        """Modal About window with clickable links.

        A plain messagebox cannot carry links, so this is a small Toplevel that
        follows the same palette as the rest of the window.
        """
        dialog = tk.Toplevel(self.root)
        dialog.title(t("about.title", app=about.APP_NAME))
        dialog.configure(bg=self.c["card"])
        dialog.transient(self.root)
        dialog.resizable(False, False)

        body = tk.Frame(dialog, bg=self.c["card"])
        body.pack(fill="both", expand=True, padx=24, pady=20)
        tk.Label(body, text=about.APP_NAME, font=self.f_title, bg=self.c["card"],
                 fg=self.c["fg"], anchor="w").pack(anchor="w")
        tk.Label(body, text=t("about.tagline"), font=self.f_body, bg=self.c["card"],
                 fg=self.c["muted"], anchor="w", justify="left",
                 wraplength=420).pack(anchor="w", pady=(6, 14))

        for key, value, link in about.about_rows():
            row = tk.Frame(body, bg=self.c["card"])
            row.pack(fill="x", anchor="w", pady=1)
            tk.Label(row, text=f"{t(key)}:", font=self.f_small, bg=self.c["card"],
                     fg=self.c["muted"], width=10, anchor="w").pack(side="left")
            if link:
                label = tk.Label(row, text=value, font=self.f_small,
                                 bg=self.c["card"], fg=self.c["accent"],
                                 cursor="hand2", anchor="w")
                label.bind("<Button-1>", lambda _e, u=link: webbrowser.open(u))
            else:
                label = tk.Label(row, text=value, font=self.f_small,
                                 bg=self.c["card"], fg=self.c["fg"], anchor="w")
            label.pack(side="left")

        self._button(body, t("btn.close"), dialog.destroy).pack(anchor="e", pady=(18, 0))
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_width()) // 2
        y = self.root.winfo_rooty() + 120
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.grab_set()

    def copy_json(self):
        payload = ({"check": self.result, "license": self.license, "data": self.data,
                    "worktime": self.worktime, "observer": self.report,
                    "instructions": self.rules}.get(self.view))
        if payload is None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        self.status_line.configure(text=t("status.copied"))


def run(start_view="check"):
    try:
        # className sets WM_CLASS so the menu entry (StartupWMClass) finds the
        # window and the taskbar shows the right icon.
        root = tk.Tk(className="claude-privacy-check")
    except tk.TclError as exc:
        print(t("error.no_display", error=exc), file=sys.stderr)
        return 1
    App(root, start_view)
    root.mainloop()
    return 0
