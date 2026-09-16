import ctypes
from ctypes import wintypes
import math
import os
import subprocess
import sys
import time

import numpy as np
from PIL import Image, ImageFilter

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(PROJECT_DIR, "orb_status.txt")
LEVEL_FILE = os.path.join(PROJECT_DIR, "orb_level.txt")
RESTART_FILE = os.path.join(PROJECT_DIR, "orb_restart.txt")

# How opaque the blob's interior is (1.0 = solid, 0.0 = invisible). This is
# what actually makes the desktop show through, unlike the old chroma-key
# window which could only ever be 0% or 100% opaque per pixel.
BLOB_OPACITY = 0.75

IDLE_COLOR = ((90, 90, 90), (150, 150, 150))

# Color pairs (dark, light) per state. Set THEME below to switch, or
# right-click the orb and choose "Cycle Theme" while it's running.
THEMES = {
    "pink/red/purple": {
        "listening": ((109, 40, 217), (167, 139, 250)),
        "speaking": ((185, 28, 28), (244, 114, 182)),
    },
    "purple/pink": {
        "listening": ((109, 40, 217), (167, 139, 250)),
        "speaking": ((219, 39, 119), (244, 114, 182)),
    },
    "red/orange": {
        "listening": ((185, 28, 28), (248, 113, 113)),
        "speaking": ((234, 88, 12), (251, 146, 60)),
    },
}
THEME_NAMES = list(THEMES.keys())
THEME = "red/orange"

def get_colors(status, theme):
    if status == "idle":
        return IDLE_COLOR
    return THEMES[theme][status]

def set_status(status):
    with open(STATUS_FILE, "w") as f:
        f.write(status)

def get_status():
    try:
        with open(STATUS_FILE) as f:
            status = f.read().strip()
    except FileNotFoundError:
        return "idle"
    return status if status in ("idle", "listening", "speaking") else "idle"

def set_level(level):
    with open(LEVEL_FILE, "w") as f:
        f.write(f"{level:.3f}")

def get_level():
    try:
        with open(LEVEL_FILE) as f:
            return max(0.0, min(1.0, float(f.read().strip())))
    except (FileNotFoundError, ValueError):
        return 0.0

def request_restart():
    """Asks a running orb.py to reload itself (e.g. after code mode edits
    it). Polled from the animate loop rather than acted on immediately,
    since this is called from claus.py, a separate process."""
    with open(RESTART_FILE, "w") as f:
        f.write("1")

def _lerp_color(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))

claus_process = None

def launch_claus():
    global claus_process
    if claus_process is None or claus_process.poll() is not None:
        claus_process = subprocess.Popen(["py", "-3.12", "claus.py"], cwd=PROJECT_DIR)

# --- Win32 layered window plumbing -----------------------------------------
# Tk's own "-transparentcolor" trick only supports binary (all-or-nothing)
# transparency: a pixel is either the exact key color (invisible) or fully
# opaque. Real translucency, where blob pixels partially blend with whatever
# is actually behind the window, needs a proper alpha-blended layered window
# (WS_EX_LAYERED + UpdateLayeredWindow), which isn't exposed by Tk and has to
# be driven directly via ctypes.

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]

user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(SIZE),
    wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]

class LayeredWindow:
    """Owns the Win32-side resources needed to push an RGBA frame to a
    WS_EX_LAYERED window with real per-pixel alpha, so its pixels blend with
    whatever is actually behind the window on the desktop."""

    def __init__(self, hwnd, width, height):
        self.hwnd = wintypes.HWND(hwnd)
        self.width = width
        self.height = height

        ex_style = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, ex_style | WS_EX_LAYERED)

        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # negative = top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        bits_ptr = ctypes.c_void_p()
        self.bitmap = gdi32.CreateDIBSection(
            self.mem_dc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0
        )
        self.old_bitmap = gdi32.SelectObject(self.mem_dc, self.bitmap)

        # A numpy view directly onto the DIB's pixel memory (B, G, R, A per
        # pixel) so each frame can just be written straight into it.
        buf = (ctypes.c_ubyte * (width * height * 4)).from_address(bits_ptr.value)
        self.pixels = np.frombuffer(buf, dtype=np.uint8).reshape((height, width, 4))

    def present(self, rgba):
        """rgba: HxWx4 uint8 array (straight, i.e. non-premultiplied, alpha)."""
        alpha = rgba[..., 3:4].astype(np.float32) / 255.0
        premult_rgb = (rgba[..., :3].astype(np.float32) * alpha).astype(np.uint8)
        self.pixels[..., 0] = premult_rgb[..., 2]  # B
        self.pixels[..., 1] = premult_rgb[..., 1]  # G
        self.pixels[..., 2] = premult_rgb[..., 0]  # R
        self.pixels[..., 3] = rgba[..., 3]

        size = SIZE(self.width, self.height)
        src_pt = wintypes.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(
            self.hwnd, self.screen_dc, None, ctypes.byref(size),
            self.mem_dc, ctypes.byref(src_pt), 0, ctypes.byref(blend), ULW_ALPHA,
        )

    def close(self):
        try:
            gdi32.SelectObject(self.mem_dc, self.old_bitmap)
            gdi32.DeleteObject(self.bitmap)
            gdi32.DeleteDC(self.mem_dc)
            user32.ReleaseDC(None, self.screen_dc)
        except Exception:
            pass

def main():
    import tkinter as tk

    # Clear any stale restart request left over from a previous run so we
    # don't immediately self-restart on startup.
    if os.path.exists(RESTART_FILE):
        try:
            os.remove(RESTART_FILE)
        except OSError:
            pass

    size = 195
    pad = 12

    window_size = size + pad * 2

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="black")
    root.geometry(f"{window_size}x{window_size}+100+100")
    root.update_idletasks()  # make sure the window/HWND actually exists before we touch it

    layered = LayeredWindow(root.winfo_id(), window_size, window_size)
    root.bind("<Destroy>", lambda e: layered.close() if e.widget is root else None)

    # The blob is rendered as a raster image (not vector shapes) so it can have
    # a soft blurred edge and a real color gradient. Each frame we build:
    #  - a boundary function r(theta) = base radius + drifting sine harmonics,
    #    evaluated per-pixel against precomputed distance/angle grids, to get
    #    an organic (non-circular) silhouette that flows over time;
    #  - a blurred alpha mask from that silhouette, for a soft edge;
    #  - a gradient color field blending 2-3 theme colors, whose blend shifts
    #    over time and reacts to audio level.
    RENDER_SCALE = 2
    render_size = int(size * RENDER_SCALE)
    render_center = render_size / 2
    BLUR_PX = 8 * RENDER_SCALE

    _ys, _xs = np.mgrid[0:render_size, 0:render_size].astype(np.float32)
    dist_grid = np.hypot(_xs - render_center, _ys - render_center)
    theta_grid = np.arctan2(_ys - render_center, _xs - render_center)

    base_r = 32 * RENDER_SCALE
    # Sine harmonics whose phases drift independently over time to make the
    # blob's outline flow and morph rather than just rotate rigidly.
    HARMONICS = [
        {"freq": 2, "amp": 0.10, "speed": 0.45, "phase": 0.0},
        {"freq": 3, "amp": 0.06, "speed": -0.30, "phase": 0.0},
        {"freq": 5, "amp": 0.04, "speed": 0.70, "phase": 0.0},
    ]
    swirl = {"phase": 0.0}

    motion = {"x_phase": 0.0, "y_phase": 0.0}

    drag = {"x": 0, "y": 0, "moved": False}

    def start_drag(event):
        drag["x"], drag["y"] = event.x, event.y
        drag["moved"] = False

    def do_drag(event):
        if abs(event.x - drag["x"]) > 3 or abs(event.y - drag["y"]) > 3:
            drag["moved"] = True
        x = root.winfo_x() + event.x - drag["x"]
        y = root.winfo_y() + event.y - drag["y"]
        root.geometry(f"+{x}+{y}")

    def end_drag(event):
        if not drag["moved"]:
            launch_claus()

    state = {"theme": THEME}

    def cycle_theme():
        idx = THEME_NAMES.index(state["theme"])
        state["theme"] = THEME_NAMES[(idx + 1) % len(THEME_NAMES)]
        print(f"Orb theme: {state['theme']}")

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="Cycle Theme", command=cycle_theme)
    menu.add_separator()
    menu.add_command(label="Quit", command=root.destroy)

    def show_menu(event):
        menu.tk_popup(event.x_root, event.y_root)

    root.bind("<ButtonPress-1>", start_drag)
    root.bind("<B1-Motion>", do_drag)
    root.bind("<ButtonRelease-1>", end_drag)
    root.bind("<ButtonPress-3>", show_menu)

    start = time.time()
    smoothed_level = {"value": 0.0}
    frame_time = {"last": start}

    def animate():
        if os.path.exists(RESTART_FILE):
            try:
                os.remove(RESTART_FILE)
            except OSError:
                pass
            print("Orb: restarting to load code changes...")
            layered.close()
            os.execv(sys.executable, [sys.executable] + sys.argv)
            return

        now = time.time()
        dt = min(now - frame_time["last"], 0.1)  # clamp huge gaps (e.g. window drag stall)
        frame_time["last"] = now
        status = get_status()
        dark, light = get_colors(status, state["theme"])

        # Smooth the raw level so size/color/speed reactions don't look jittery.
        raw_level = get_level() if status != "idle" else 0.0
        smoothed_level["value"] += (raw_level - smoothed_level["value"]) * 0.3
        level = smoothed_level["value"]

        # A third, continuously drifting color (blended from the theme's dark
        # and light ends) used for the flowing swirl highlight. Its period
        # shortens with audio level so colors move faster when louder.
        period = max(0.6, (3.0 if status == "idle" else 1.2) / (1 + level * 2))
        breathe = (math.sin(2 * math.pi * (now - start) / period) + 1) / 2
        color_c = _lerp_color(dark, light, breathe)

        # Phases accumulate incrementally from elapsed frame time (dt) rather
        # than being recomputed from total elapsed time, so a change in level
        # only affects speed going forward instead of jumping the whole
        # animation history (which reads as jagged/choppy).
        level_boost = 1 + level * 2.4
        for h in HARMONICS:
            h["phase"] = (h["phase"] + dt * h["speed"] * level_boost) % (2 * math.pi)
        swirl["phase"] = (swirl["phase"] + dt * 0.5 * level_boost) % (2 * math.pi)

        # Organic, morphing silhouette: base radius pulses with level, and an
        # angle-dependent wobble (sum of drifting sine harmonics) bulges and
        # flows around the boundary instead of tracing a rigid circle/ring.
        core_r = base_r * (1 + level * 0.7)
        wobble = sum(h["amp"] * np.sin(h["freq"] * theta_grid + h["phase"]) for h in HARMONICS)
        amp_scale = 1 + level * 1.8
        boundary = core_r * (1 + wobble * amp_scale)
        mask_arr = (dist_grid <= boundary).astype(np.uint8) * 255
        mask_img = Image.fromarray(mask_arr, mode="L").filter(ImageFilter.GaussianBlur(BLUR_PX))

        # Soft gradient blend across 2-3 theme colors: light at the center
        # fading to dark at the edge, with a rotating/flowing swirl band of
        # the drifting third color mixed in, more pronounced when louder.
        t_radial = np.clip(dist_grid / core_r, 0.0, 1.0)[..., None]
        arr_dark = np.array(dark, dtype=np.float32)
        arr_light = np.array(light, dtype=np.float32)
        arr_c = np.array(color_c, dtype=np.float32)
        base_color = arr_light * (1 - t_radial) + arr_dark * t_radial

        swirl_t = (0.5 + 0.5 * np.sin(2 * theta_grid + swirl["phase"]))[..., None]
        swirl_strength = 0.3 + level * 0.5
        blended = base_color * (1 - swirl_t * swirl_strength) + arr_c * (swirl_t * swirl_strength)
        blended *= 1 + level * 0.25  # brighter overall when louder
        color_arr = np.clip(blended, 0, 255).astype(np.uint8)
        color_img = Image.fromarray(color_arr, mode="RGB")

        # Build a straight-alpha RGBA blob: color from the gradient, alpha from
        # the blurred silhouette mask scaled by BLOB_OPACITY. Because the
        # window is now a real alpha-blended layered window (not a chroma-key
        # one), this alpha genuinely lets the desktop show through, both at
        # the soft edge and across the whole interior of the blob.
        alpha_arr = (np.array(mask_img, dtype=np.float32) * BLOB_OPACITY).astype(np.uint8)
        blob_rgba = color_img.convert("RGBA")
        blob_rgba.putalpha(Image.fromarray(alpha_arr, mode="L"))
        blob_small = blob_rgba.resize((size, size), Image.LANCZOS)

        # Drift the whole blob around within its padded window so it visibly
        # moves (not just pulses in place) while listening/speaking; the two
        # axes use different, level-scaled frequencies so the path reads as an
        # organic wander rather than a rigid orbit.
        motion["x_phase"] = (motion["x_phase"] + dt * 0.9 * level_boost) % (2 * math.pi)
        motion["y_phase"] = (motion["y_phase"] + dt * 1.3 * level_boost) % (2 * math.pi)
        move_amp = pad * level
        move_x = move_amp * math.sin(motion["x_phase"])
        move_y = move_amp * math.cos(motion["y_phase"])
        px = int(round(pad + move_x))
        py = int(round(pad + move_y))

        # Compose the blob into a window-sized buffer (fully transparent
        # outside it) and push it straight to the layered window.
        window_rgba = np.zeros((window_size, window_size, 4), dtype=np.uint8)
        window_rgba[py:py + size, px:px + size] = np.array(blob_small)
        layered.present(window_rgba)

        root.after(33, animate)

    animate()
    root.mainloop()

if __name__ == "__main__":
    main()
