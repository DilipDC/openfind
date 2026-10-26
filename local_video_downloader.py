"""OpenFind Local Video Downloader.

Runs extraction on the user's own computer instead of a hosted cloud server.
Only download content you own or are authorized to save.
"""
from __future__ import annotations

import os
import queue
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


class LocalDownloader(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("OpenFind — Local Video Downloader")
        self.geometry("640x390")
        self.minsize(560, 350)
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.output_dir = tk.StringVar(value=str(Path.home() / "Downloads"))
        self.browser = tk.StringVar(value="None")
        self.url = tk.StringVar()
        self.status = tk.StringVar(value="Ready. Downloads run locally on this computer.")
        self._build()
        self.after(150, self._poll_events)

    def _build(self) -> None:
        root = ttk.Frame(self, padding=20)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="OpenFind Local Downloader", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            root,
            text="Uses your computer's network, not OpenFind's cloud server. Some sites may still restrict downloads.",
            wraplength=580,
        ).pack(anchor="w", pady=(5, 18))

        ttk.Label(root, text="Video URL").pack(anchor="w")
        url_row = ttk.Frame(root)
        url_row.pack(fill="x", pady=(4, 12))
        ttk.Entry(url_row, textvariable=self.url).pack(side="left", fill="x", expand=True)

        ttk.Label(root, text="Save folder").pack(anchor="w")
        folder_row = ttk.Frame(root)
        folder_row.pack(fill="x", pady=(4, 12))
        ttk.Entry(folder_row, textvariable=self.output_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(folder_row, text="Browse…", command=self._choose_folder).pack(side="left", padx=(8, 0))

        options = ttk.Frame(root)
        options.pack(fill="x", pady=(0, 12))
        ttk.Label(options, text="Use your signed-in browser session (optional):").pack(side="left")
        ttk.Combobox(
            options, textvariable=self.browser, state="readonly", width=12,
            values=("None", "Chrome", "Edge", "Firefox", "Brave", "Opera"),
        ).pack(side="left", padx=(8, 0))

        self.button = ttk.Button(root, text="Download video", command=self._start)
        self.button.pack(anchor="w")
        ttk.Label(root, textvariable=self.status, wraplength=580).pack(anchor="w", pady=(12, 0))
        ttk.Label(
            root,
            text="Privacy: URLs and browser cookies are handled on this computer; this helper does not send cookies to OpenFind.",
            wraplength=580,
        ).pack(anchor="w", pady=(14, 0))

    def _choose_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.output_dir.get() or str(Path.home()))
        if chosen:
            self.output_dir.set(chosen)

    def _start(self) -> None:
        if yt_dlp is None:
            messagebox.showerror(
                "yt-dlp not installed",
                "Install the local requirements first:\n\npython -m pip install -U \"yt-dlp[default]\"",
            )
            return
        url = self.url.get().strip()
        if not url.startswith(("https://", "http://")):
            messagebox.showerror("Invalid URL", "Enter a full http:// or https:// video URL.")
            return
        folder = Path(self.output_dir.get()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Save folder error", str(exc))
            return

        options = {
            "outtmpl": str(folder / "%(title).150B-%(id)s.%(ext)s"),
            "format": "bv*+ba/b" if shutil.which("ffmpeg") else "best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "retries": 3,
            "fragment_retries": 3,
            "quiet": True,
            "noprogress": True,
            "progress_hooks": [self._progress],
        }
        browser = self.browser.get()
        if browser != "None":
            options["cookiesfrombrowser"] = (browser.lower(),)
        self.button.configure(state="disabled")
        self.status.set("Starting local download…")
        threading.Thread(target=self._download, args=(url, options), daemon=True).start()

    def _progress(self, data: dict) -> None:
        state = data.get("status")
        if state == "downloading":
            percent = str(data.get("_percent_str", "")).strip()
            self.events.put(("status", f"Downloading… {percent}"))
        elif state == "finished":
            self.events.put(("status", "Download finished; processing/merging file…"))

    def _download(self, url: str, options: dict) -> None:
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.download([url])
            self.events.put(("success", "Download completed. Check the selected save folder."))
        except Exception as exc:
            # Keep details local and show a short, actionable error.
            detail = str(exc).strip().splitlines()
            message = detail[-1][:500] if detail else "Unknown download error."
            self.events.put(("error", message))
        finally:
            self.events.put(("enable", ""))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, message = self.events.get_nowait()
                if kind == "status":
                    self.status.set(message)
                elif kind == "success":
                    self.status.set(message)
                    messagebox.showinfo("OpenFind", message)
                elif kind == "error":
                    self.status.set("Download failed.")
                    messagebox.showerror("Download failed", message)
                elif kind == "enable":
                    self.button.configure(state="normal")
        except queue.Empty:
            pass
        self.after(150, self._poll_events)


if __name__ == "__main__":
    app = LocalDownloader()
    if yt_dlp is None:
        app.status.set('Install yt-dlp with: python -m pip install -U "yt-dlp[default]"')
    app.mainloop()
