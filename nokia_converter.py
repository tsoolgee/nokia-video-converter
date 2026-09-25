# -*- coding: utf-8 -*-
"""ממיר לנוקיה: גוררים תיקייה או קבצים, וכל הסרטונים מומרים ל-MP4 שנוקיה מנגנת
(MPEG-4 320x240, AAC), באותו מבנה תיקיות בתוך תיקייה חדשה שנוצרת ליד המקור."""

import json
import os
import re
import subprocess
import sys
import threading
import time

import webview
from webview.dom import DOMEventHandler

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


def is_video(path):
    return os.path.splitext(path)[1].lower() in VIDEO_EXT


def plan_jobs(paths):
    """מחזיר רשימת (מקור, יעד, תיאור מיקום) לכל סרטון, ואת התיקיות שייווצרו."""
    jobs, outs = [], []
    for p in paths:
        p = os.path.normpath(p)
        if os.path.isdir(p):
            if p.endswith(OUT_SUFFIX):
                continue
            out_root = p.rstrip("\\/") + OUT_SUFFIX
            outs.append(out_root)
            base = os.path.basename(p)
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for f in sorted(files):
                    src = os.path.join(root, f)
                    if is_video(src):
                        rel = os.path.relpath(src, p)
                        dst = os.path.join(out_root, os.path.splitext(rel)[0] + ".mp4")
                        where = os.path.join(base, os.path.dirname(rel)).rstrip("\\/")
                        jobs.append((src, dst, where))
        elif os.path.isfile(p) and is_video(p):
            stem = os.path.splitext(p)[0]
            if not stem.endswith(OUT_SUFFIX):
                jobs.append((p, stem + OUT_SUFFIX + ".mp4", os.path.dirname(p)))
                outs.append(os.path.dirname(p))
    return jobs, outs


def duration_of(src):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", src], capture_output=True,
                       creationflags=NO_WINDOW)
    m = re.search(rb"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr)
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


class Api:
    def __init__(self):
        self._window = None
        self._proc = None
        self._cancelled = False
        self._busy = False
        self._outs = []

    # --- נקרא מהממשק ---
    def pick(self, kind):
        if self._busy:
            return ""
        if kind == "folder":
            r = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        else:
            exts = ";".join("*" + e for e in sorted(VIDEO_EXT))
            r = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=(f"סרטונים ({exts})", "כל הקבצים (*.*)"))
        if not r:
            return ""
        return self.start(list(r) if not isinstance(r, str) else [r])

    def cancel(self):
        self._cancelled = True
        p = self._proc
        if p:
            p.kill()

    def open_output(self):
        for d in self._outs:
            if os.path.isdir(d):
                os.startfile(d)

    # --- עבודה ---
    def start(self, paths):
        if self._busy:
            return ""
        jobs, outs = plan_jobs(paths)
        if not jobs:
            return "לא נמצאו סרטונים במה שנבחר"
        self._busy = True
        self._cancelled = False
        self._outs = list(dict.fromkeys(outs))
        self.js("onJobs", [{"id": i, "name": os.path.basename(s), "rel": w}
                           for i, (s, _, w) in enumerate(jobs)])
        threading.Thread(target=self.run, args=(jobs,), daemon=True).start()
        return ""

    def run(self, jobs):
        done = skipped = failed = 0
        total = len(jobs)
        try:
            for i, (src, dst, _) in enumerate(jobs):
                if self._cancelled:
                    break
                if os.path.exists(dst) and os.path.getsize(dst) > 0:
                    skipped += 1
                    self.js("onItem", i, "skip", 100)
                else:
                    ok = self.convert(src, dst, i, total)
                    if ok:
                        done += 1
                        self.js("onItem", i, "done", 100)
                    elif self._cancelled:
                        self.js("onItem", i, "wait", 0)
                    else:
                        failed += 1
                        self.js("onItem", i, "fail", 0)
                self.js("onTotal", (i + 1) * 100 / total, self.counts(i + 1, total, done, skipped, failed))
        finally:
            self._busy = False
            self._proc = None

        if self._cancelled:
            title = "ההמרה נעצרה"
        elif failed:
            title = "הסתיים, עם שגיאות"
        else:
            title = "הכל מוכן!"
        parts = [f"{done} סרטונים הומרו"]
        if skipped:
            parts.append(f"{skipped} כבר היו מומרים")
        if failed:
            parts.append(f"{failed} לא הצליחו (קובץ פגום או מוגן)")
        banner = ", ".join(parts) + ". העתק את התיקייה \"- נוקיה\" לטלפון."
        self.js("onDone", title, banner, not failed and not self._cancelled)

    @staticmethod
    def counts(n, total, done, skipped, failed):
        s = f"{n} מתוך {total}"
        if failed:
            s += f" · {failed} נכשלו"
        return s

    def convert(self, src, dst, i, total):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst[:-4] + ".partial.mp4"
        dur = duration_of(src)
        self.js("onItem", i, "run", 0)
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
                    self.js("onItem", i, "run", frac * 100)
                    self.js("onTotal", (i + frac) * 100 / total, f"{i + 1} מתוך {total}")
            rc = self._proc.wait()
        except OSError:
            rc = -1
        finally:
            self._proc = None
        if rc == 0 and not self._cancelled and os.path.exists(tmp):
            os.replace(tmp, dst)
            return True
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

    def js(self, fn, *args):
        if self._window:
            try:
                self._window.evaluate_js(f"window.{fn} && window.{fn}(...{json.dumps(args)})")
            except Exception:
                pass


def main():
    api = Api()
    with open(resource("ui.html"), encoding="utf-8") as f:
        html = f.read()
    window = webview.create_window(
        "ממיר לנוקיה", html=html, js_api=api, width=760, height=640, min_size=(560, 520),
        background_color="#eef2f8")
    api._window = window

    def on_drop(e):
        files = e.get("dataTransfer", {}).get("files", [])
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        if paths:
            msg = api.start(paths)
            if msg:
                api.js("onToast", msg)

    def on_loaded():
        window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        # גרירה על קובץ ה-EXE עצמו
        args = [a for a in sys.argv[1:] if os.path.exists(a)]
        if args:
            api.start(args)

    window.events.loaded += on_loaded
    webview.start(private_mode=True)


if __name__ == "__main__":
    main()
