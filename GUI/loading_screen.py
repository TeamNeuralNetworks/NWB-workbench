from tkinter.scrolledtext import ScrolledText
import tkinter as tk
from tkinter import ttk

def ask_three_options(title, message, button1="Overwrite", button2="New folder", button3="Cancel", parent=None):
    result = {"value": None}

    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.transient(parent)
    dialog.grab_set()

    # put window in front
    dialog.attributes("-topmost", True)
    dialog.lift()
    dialog.focus_force()

    frame = tk.Frame(dialog, padx=20, pady=20)
    frame.pack(fill="both", expand=True)

    label = tk.Label(frame, text=message, justify="left", wraplength=500)
    label.pack(pady=(0, 20))

    button_frame = tk.Frame(frame)
    button_frame.pack()

    def choose(value):
        result["value"] = value
        dialog.destroy()

    tk.Button(button_frame, text=button1, width=15, command=lambda: choose(button1)).pack(side="left", padx=5)
    tk.Button(button_frame, text=button2, width=15, command=lambda: choose(button2)).pack(side="left", padx=5)
    tk.Button(button_frame, text=button3, width=15, command=lambda: choose(button3)).pack(side="left", padx=5)

    # center on parent or screen
    dialog.update_idletasks()
    w = dialog.winfo_width()
    h = dialog.winfo_height()

    if parent is not None:
        x = parent.winfo_rootx() + (parent.winfo_width() // 2) - (w // 2)
        y = parent.winfo_rooty() + (parent.winfo_height() // 2) - (h // 2)
    else:
        x = (dialog.winfo_screenwidth() // 2) - (w // 2)
        y = (dialog.winfo_screenheight() // 2) - (h // 2)

    dialog.geometry(f"+{x}+{y}")

    dialog.wait_window()
    return result["value"]

class LoadingScreen:
    def __init__(self, parent=None, title="Processing", width=700, height=420):
        self.parent = parent
        self.title = title
        self.width = width
        self.height = height

        self.win = None
        self.status_var = tk.StringVar(value="")
        self.file_var = tk.StringVar(value="")
        self.progress = None
        self.log_text = None

        self._queue = []

    def open(self, total=1, initial_text="Preparing..."):
        self.win = tk.Toplevel(self.parent)
        self.win.title(self.title)
        self.win.geometry(f"{self.width}x{self.height}")

        main = ttk.Frame(self.win, padding=10)
        main.pack(fill="both", expand=True)

        ttk.Label(main, textvariable=self.status_var).pack(anchor="w", pady=(0,2))
        ttk.Label(main, textvariable=self.file_var, wraplength=self.width-40).pack(anchor="w", pady=(0,4))

        self.progress = ttk.Progressbar(main, mode="determinate", maximum=max(1, total))
        self.progress.pack(fill="x", pady=(0, 4))

        self.log_text = ScrolledText(main, height=5, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)

        self.status_var.set(initial_text)
        self.file_var.set("")

        self.win.after(100, self._drain_events)
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.focus_force()
        self.win.after(200, lambda: self.win.attributes("-topmost", False))
        self.refresh()

    def post(self, kind, payload):
        self._queue.append((kind, payload))

    def _append_log(self, text):
        if self.log_text is None:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _drain_events(self):
        while self._queue:
            kind, payload = self._queue.pop(0)

            if kind == "status":
                self.status_var.set(payload.get("text", ""))

            elif kind == "file":
                self.file_var.set(payload.get("path", ""))

            elif kind == "progress":
                if self.progress is not None:
                    self.progress["value"] = payload.get("i", 0)

            elif kind == "log":
                self._append_log(payload.get("text", ""))

            elif kind == "done":
                self.status_var.set(payload.get("text", "Done"))
                self._append_log(payload.get("text", "Done"))

            elif kind == "error":
                txt = payload.get("text", "Error")
                detail = payload.get("detail", "")
                self.status_var.set(txt)
                self._append_log(f"ERROR: {txt}")
                if detail:
                    self._append_log(detail)

        if self.win is not None and self.win.winfo_exists():
            self.win.after(100, self._drain_events)
    
    def refresh(self):
        if self.win is None or not self.win.winfo_exists():
            return
        try:
            self.win.update_idletasks()
            self.win.update()
        except tk.TclError:
            pass
    
    def close(self):
        if self.win is not None and self.win.winfo_exists():
            try:
                self.win.destroy()
            except tk.TclError:
                pass
        self.win = None