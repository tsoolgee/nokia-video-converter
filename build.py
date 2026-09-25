# -*- coding: utf-8 -*-
"""בונה את NokiaConverter.exe: קובץ יחיד עם ffmpeg וממשק מובנים.

דרישות: pip install pywebview pyinstaller imageio-ffmpeg
"""
import os
import shutil
import subprocess
import sys

import imageio_ffmpeg

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

ff = os.path.join(HERE, "ffmpeg.exe")
if not os.path.exists(ff):
    shutil.copy(imageio_ffmpeg.get_ffmpeg_exe(), ff)

EXCLUDE = ["PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "gi", "cefpython3", "matplotlib",
           "numpy", "PIL", "tkinter", "tkinterdnd2", "pandas", "scipy", "IPython", "jedi",
           "imageio_ffmpeg", "cv2", "torch"]

cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
       "--name", "NokiaConverter", "--icon", os.path.join(HERE, "icon.ico"),
       "--add-data", os.path.join(HERE, "ui.html") + ";.",
       "--add-binary", ff + ";.",
       "--distpath", "dist", "--workpath", "build", "--specpath", "build"]
for m in EXCLUDE:
    cmd += ["--exclude-module", m]
cmd.append("nokia_converter.py")
subprocess.check_call(cmd)
print("\nמוכן: dist\\NokiaConverter.exe")
