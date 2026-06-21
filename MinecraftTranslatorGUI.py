import os
import tkinter as tk

from gui.main_window import ModTranslatorApp


def main() -> None:
    """啟動 Minecraft 模組翻譯器 GUI。"""
    if os.name == "nt":
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    root = tk.Tk()
    try:
        root.tk.call('tk', 'scaling', root.winfo_fpixels('1i') / 72.0)
    except Exception:
        pass
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w = min(1500, max(1180, sw - 156))
    h = min(900, max(720, sh - 84))
    w = min(w, sw - 40)
    h = min(h, sh - 80)
    root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
    root.minsize(min(1100, sw - 80), min(640, sh - 120))
    app = ModTranslatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

