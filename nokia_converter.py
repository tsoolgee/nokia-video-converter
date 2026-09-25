# -*- coding: utf-8 -*-
"""ממיר סרטונים לנוקיה: גוררים תיקייה או קבצים, וכל הסרטונים מומרים ל-MP4 שנוקיה מנגנת
(MPEG-4 320x240, AAC), באותו מבנה תיקיות.

נוצר על ידי צול גאה · tsoolgee.uk"""

import base64
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from ctypes import wintypes

import webview
from webview.dom import DOMEventHandler

VERSION = "0.0.2"
SITE = "https://tsoolgee.uk"
REPO = "tsoolgee/nokia-video-converter"
DOWNLOAD_URL = f"https://github.com/{REPO}/releases/latest/download/NokiaConverter.exe"

VIDEO_EXT = {
    ".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mov", ".qt", ".wmv", ".asf", ".flv", ".f4v",
    ".mpg", ".mpeg", ".mpe", ".mpv", ".m2v", ".mp2v", ".m1v", ".vob", ".ts", ".mts", ".m2ts",
    ".m2t", ".trp", ".tod", ".mod", ".3gp", ".3g2", ".3gpp", ".ogv", ".ogm",
    ".rm", ".rmvb", ".divx", ".xvid", ".dv", ".mxf", ".amv", ".nsv", ".wtv", ".dvr-ms",
    ".y4m", ".h264", ".264", ".h265", ".265", ".hevc", ".mjpeg", ".mjpg", ".ivf",
    ".bik", ".roq", ".smk", ".nut",
}
OUT_SUFFIX = " - נוקיה"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ההגדרות של "2.mp4", שנבדקו על Nokia 215
ENCODE_ARGS = [
    "-map", "0:v:0", "-map", "0:a:0?",
    "-vf", "scale=320:240:force_original_aspect_ratio=decrease,"
           "pad=320:240:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25,format=yuv420p",
    "-c:v", "mpeg4", "-vtag", "mp4v", "-b:v", "400k",
    "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "96k",
    "-movflags", "+faststart", "-sn", "-dn", "-map_metadata", "-1",
]

SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "NokiaConverter")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
DEFAULTS = {"theme": "dark", "mode": "copy"}  # mode: copy = תיקייה חדשה, replace = החלפת המקור


def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def find_ffmpeg():
    bundled = resource("ffmpeg.exe")
    if os.path.exists(bundled):
        return bundled
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


FFMPEG = find_ffmpeg()


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s = json.load(f)
        s = {k: s.get(k, v) for k, v in DEFAULTS.items()}
        if s["theme"] not in ("dark", "light"):
            s["theme"] = "dark"
        return s
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(s):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
    except OSError:
        pass


def is_video(path):
    return os.path.splitext(path)[1].lower() in VIDEO_EXT


def free_name(path, avoid):
    """אם הנתיב תפוס (ואינו avoid) מוסיף (1), (2)..."""
    if not os.path.exists(path) or os.path.normcase(path) == os.path.normcase(avoid):
        return path
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def plan_jobs(paths, mode):
    """מחזיר רשימת (מקור, יעד, תיאור מיקום) לכל סרטון, ואת התיקיות לפתיחה בסוף.
    במצב replace היעד הוא ליד המקור, והמקור עובר לסל המחזור אחרי המרה מוצלחת."""
    jobs, outs = [], []
    for p in paths:
        p = os.path.normpath(p)
        if os.path.isdir(p):
            if mode == "copy" and p.endswith(OUT_SUFFIX):
                continue
            out_root = p if mode == "replace" else p.rstrip("\\/") + OUT_SUFFIX
            outs.append(out_root)
            base = os.path.basename(p)
            for root, dirs, files in os.walk(p):
                dirs.sort()
                if mode == "copy":
                    dirs[:] = [d for d in dirs if not d.endswith(OUT_SUFFIX)]
                for f in sorted(files):
                    src = os.path.join(root, f)
                    if not is_video(src) or f.endswith(".partial.mp4"):
                        continue
                    rel = os.path.relpath(src, p)
                    dst = os.path.join(out_root, os.path.splitext(rel)[0] + ".mp4")
                    jobs.append((src, dst, os.path.join(base, os.path.dirname(rel)).rstrip("\\/")))
        elif os.path.isfile(p) and is_video(p):
            stem = os.path.splitext(p)[0]
            if mode == "replace":
                jobs.append((p, stem + ".mp4", os.path.dirname(p)))
            elif not stem.endswith(OUT_SUFFIX):
                jobs.append((p, stem + OUT_SUFFIX + ".mp4", os.path.dirname(p)))
            outs.append(os.path.dirname(p))
    return jobs, list(dict.fromkeys(outs))


def probe(src):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", src], capture_output=True,
                       creationflags=NO_WINDOW)
    return r.stderr.decode("utf-8", "replace")


def duration_of(info):
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", info)
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def already_nokia(info):
    """הקובץ כבר בפורמט של נוקיה? (כדי לא להמיר שוב במצב החלפה)"""
    return (re.search(r"Video: mpeg4 .*\b320x240\b", info) is not None
            and (re.search(r"Audio: aac", info) is not None or "Audio:" not in info))


def ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def latest_release():
    """מחזיר (גרסה, הערות) של הגרסה האחרונה בגיטהאב, או None אם אין חיבור."""
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"User-Agent": "NokiaConverter", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    return data.get("tag_name", "").lstrip("v"), data.get("body") or ""


# --- סל מחזור (ולא מחיקה סופית) ---
class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


def to_recycle_bin(path):
    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, os.path.abspath(path) + "\0\0", None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
                         False, None, None)
    ok = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0
    return ok and not op.fAnyOperationsAborted and not os.path.exists(path)


def kill_children_on_exit():
    """כל תהליך ffmpeg שייפתח נסגר יחד עם התוכנה, גם אם היא נסגרה בכוח."""
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)

        class LIMITS(ctypes.Structure):
            _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                        ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD),
                        ("f", ctypes.c_size_t), ("g", wintypes.DWORD), ("h", wintypes.DWORD)]

        class EXT(ctypes.Structure):
            _fields_ = [("basic", LIMITS), ("io", ctypes.c_uint64 * 6), ("m1", ctypes.c_size_t),
                        ("m2", ctypes.c_size_t), ("m3", ctypes.c_size_t), ("m4", ctypes.c_size_t)]

        info = EXT()
        info.basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(k32.GetCurrentProcess()))
        return job
    except Exception:
        return None


class Api:
    def __init__(self):
        self._window = None
        self._proc = None
        self._cancelled = False
        self._busy = False
        self._outs = []
        self._settings = load_settings()

    # --- נקרא מהממשק ---

    def set_setting(self, key, value):
        if key in DEFAULTS:
            self._settings[key] = value
            save_settings(self._settings)

    def open_site(self):
        webbrowser.open(SITE)

    def download_update(self):
        webbrowser.open(DOWNLOAD_URL)

    def check_update(self, manual=False):
        threading.Thread(target=self._check_update, args=(manual,), daemon=True).start()

    def _check_update(self, manual):
        try:
            latest, notes = latest_release()
        except Exception:
            if manual:
                self._js("onToast", "אין חיבור לבדיקת עדכונים")
            return
        if latest and ver_tuple(latest) > ver_tuple(VERSION):
            self._js("onUpdate", latest, notes)
        elif manual:
            self._js("onToast", "יש לך את הגרסה האחרונה")

    def pick(self, kind):
        if self._busy:
            return
        if kind == "folder":
            r = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        else:
            exts = ";".join("*" + e for e in sorted(VIDEO_EXT))
            r = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=(f"סרטונים ({exts})", "כל הקבצים (*.*)"))
        if r:
            self._start(list(r) if not isinstance(r, str) else [r])

    def cancel(self):
        self._cancelled = True
        p = self._proc
        if p:
            p.kill()

    def open_output(self):
        for d in self._outs:
            if os.path.isdir(d):
                os.startfile(d)

    # --- עבודה (הכל ברקע, כדי שהחלון לא ייתקע) ---
    def _start(self, paths):
        if self._busy:
            return
        self._busy = True
        self._cancelled = False
        threading.Thread(target=self._run, args=(paths,), daemon=True).start()

    def _run(self, paths):
        try:
            mode = self._settings["mode"]
            self._js("onScanning")
            jobs, self._outs = plan_jobs(paths, mode)
            if not jobs:
                self._js("onNothing", "לא נמצאו סרטונים במה שנבחר")
                return
            self._js("onJobs", [{"id": i, "name": os.path.basename(s), "rel": w}
                                for i, (s, _, w) in enumerate(jobs)], mode)
            self._convert_all(jobs, mode)
        finally:
            self._busy = False
            self._proc = None

    def _convert_all(self, jobs, mode):
        done = skipped = failed = 0
        total = len(jobs)
        for i, (src, dst, _) in enumerate(jobs):
            if self._cancelled:
                break
            self._js("onCurrent", i, os.path.basename(src))
            self._js("onTotal", i * 100 / total)
            if mode == "copy" and os.path.exists(dst) and os.path.getsize(dst) > 0:
                skipped += 1
                self._js("onItem", i, "skip", 100)
                continue
            info = probe(src)
            if mode == "replace" and already_nokia(info):
                skipped += 1
                self._js("onItem", i, "skip", 100)
                continue
            if self._convert(src, dst, info, i, total, mode):
                done += 1
                self._js("onItem", i, "done", 100)
            elif self._cancelled:
                self._js("onItem", i, "wait", 0)
            else:
                failed += 1
                self._js("onItem", i, "fail", 0)

        if self._cancelled:
            title = "ההמרה נעצרה"
        elif failed:
            title = "הסתיים, עם שגיאות"
        else:
            title = "הכל מוכן!"
        parts = [f"{done} סרטונים הומרו"]
        if skipped:
            parts.append(f"{skipped} כבר היו מוכנים")
        if failed:
            parts.append(f"{failed} לא הצליחו (קובץ פגום או מוגן)")
        if mode == "replace":
            tail = " הקבצים המקוריים הועברו לסל המחזור." if done else ""
        else:
            tail = " העתק את התיקייה \"- נוקיה\" לטלפון."
        processed = done + skipped + failed
        self._js("onDone", title, ", ".join(parts) + "." + tail,
                 not failed and not self._cancelled, processed * 100 / total)

    def _convert(self, src, dst, info, i, total, mode):
        if mode == "replace":
            dst = free_name(dst, src)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = os.path.splitext(dst)[0] + ".partial.mp4"
        dur = duration_of(info)
        self._js("onItem", i, "run", 0)
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-i", src, *ENCODE_ARGS,
               "-progress", "pipe:1", "-nostats", "-loglevel", "error", tmp]
        last = 0.0
        rc = -1
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          creationflags=NO_WINDOW)
            for line in self._proc.stdout:
                if line.startswith(b"out_time_us=") and dur > 0:
                    try:
                        t = int(line.split(b"=")[1]) / 1e6
                    except ValueError:
                        continue
                    now = time.monotonic()
                    if now - last < 0.25:
                        continue
                    last = now
                    frac = max(0.0, min(1.0, t / dur))
                    self._js("onItem", i, "run", frac * 100)
                    self._js("onTotal", (i + frac) * 100 / total)
            rc = self._proc.wait()
        except OSError:
            rc = -1
        finally:
            self._proc = None
        if rc != 0 or self._cancelled or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        if mode == "replace" and not to_recycle_bin(src):
            # המקור לא זז לסל המחזור: לא נוגעים בו, והקובץ החדש לא נשמר
            os.remove(tmp)
            return False
        os.replace(tmp, dst)
        return True

    def _js(self, fn, *args):
        if self._window:
            try:
                self._window.evaluate_js(f"window.{fn} && window.{fn}(...{json.dumps(args)})")
            except Exception:
                pass


def main():
    _job = kill_children_on_exit()  # noqa: F841 (חייב להישאר חי עד הסוף)
    api = Api()
    with open(resource("ui.html"), encoding="utf-8") as f:
        html = f.read()
    with open(resource("logo.png"), "rb") as f:
        logo = "data:image/png;base64," + base64.b64encode(f.read()).decode()
    s = api._settings
    html = (html.replace("{{LOGO}}", logo).replace("{{VERSION}}", VERSION)
            .replace("{{THEME}}", s["theme"]).replace("{{SETTINGS}}", json.dumps(s)))
    dark = api._settings["theme"] != "light"
    window = webview.create_window(
        "ממיר סרטונים לנוקיה", html=html, js_api=api, width=780, height=700, min_size=(580, 580),
        background_color="#0f1420" if dark else "#eef2f8")
    api._window = window

    def on_drop(e):
        files = e.get("dataTransfer", {}).get("files", [])
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        if paths:
            api._start(paths)

    def on_loaded():
        window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        # גרירה על קובץ ה-EXE עצמו
        args = [a for a in sys.argv[1:] if os.path.exists(a)]
        if args:
            api._start(args)
        api.check_update()

    window.events.loaded += on_loaded
    window.events.closing += api.cancel
    webview.start(private_mode=True)


if __name__ == "__main__":
    main()
