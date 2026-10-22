# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Klearing. Build from the repo root:

    pyinstaller build/klearing.spec --noconfirm

Produces dist/Klearing (onedir) with Klearing.exe on Windows, and
dist/Klearing.app on macOS. Onedir on purpose: it starts faster than
onefile and trips far fewer antivirus heuristics (the onefile
self-extracting stub is what scanners flag).

The one exe is the sole packaged launch target (Klearing.exe, no flags for
normal use). It is windowed, starts its backend only as a supervised private
child, and routes output to the data-dir logs the Logs page shows.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ASSETS = os.path.abspath(os.path.join(SPECPATH, "..", "assets"))
ICO = os.path.join(ASSETS, "klearing.ico")
ICNS = os.path.join(ASSETS, "klearing.icns")

# uvicorn resolves its loops/protocols dynamically; keyring discovers
# backends via entry points; pywebview picks its GUI backend at
# runtime. All three patterns are invisible to static analysis, so
# spell them out. (pyinstaller-hooks-contrib's hook-webview already
# collects the WebView2 interop DLLs on Windows, and the clr/
# clr_loader + pyobjc hooks cover pythonnet and the Cocoa bridge.)
hiddenimports = collect_submodules("uvicorn") + [
    "anyio._backends._asyncio",
    "keyring.backends",
    "keyring.backends.fail",
    "keyring.backends.chainer",
]
if sys.platform == "win32":
    hiddenimports += ["keyring.backends.Windows", "win32ctypes.pywin32",
                      "win32ctypes.core",
                      "webview.platforms.winforms",
                      "webview.platforms.edgechromium",
                      "clr", "clr_loader"]
    hiddenimports += collect_submodules("windows_toasts")
    hiddenimports += collect_submodules("winrt") + [
        "winrt.windows.data.xml.dom",
        "winrt.windows.foundation",
        "winrt.windows.foundation.collections",
        "winrt.windows.ui.notifications",
    ]
elif sys.platform == "darwin":
    hiddenimports += ["keyring.backends.macOS",
                      "webview.platforms.cocoa",
                      "AppKit", "Foundation", "WebKit", "Quartz",
                      "Security", "objc", "PyObjCTools.AppHelper"]
else:
    hiddenimports += ["keyring.backends.SecretService",
                      "webview.platforms.gtk"]

a = Analysis(
    ["../main.py"],
    pathex=[".."],
    binaries=[],
    # The .ico ships inside the bundle so the web app can serve it as
    # the browser-tab favicon (see paths.resource_path).
    datas=[(ICO, "assets"),
           # The whole migrations directory, not one named file. This
           # used to bundle add_tax_tables.sql alone, which meant a
           # frozen build shipped without the four ledger migrations
           # that run_migrations.py registers — db.init_db() would fail
           # to find them on a packaged install.
           (os.path.abspath(os.path.join(SPECPATH, "..", "migrations")),
            "migrations"),
           (os.path.abspath(os.path.join(SPECPATH, "..", "frontend", "tax")),
            "frontend/tax"),
           (os.path.abspath(os.path.join(SPECPATH, "..", "frontend", "insights")),
            "frontend/insights"),
           (os.path.abspath(os.path.join(SPECPATH, "..", "frontend", "fonts")),
            "frontend/fonts"),
           (os.path.abspath(os.path.join(SPECPATH, "..", "frontend", "app")),
            "frontend/app")],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Klearing",
    debug=False,
    strip=False,
    upx=False,           # UPX-packed exes are another AV heuristic magnet
    console=False,       # windowed; logs go to the data dir instead
    icon=ICO if sys.platform == "win32" else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Klearing",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Klearing.app",
        icon=ICNS,
        bundle_identifier="com.klearing.app",
        info_plist={
            "NSHighResolutionCapable": True,
            # A background-ish app: it lives in the browser, so no Dock
            # bounce beyond launch is needed.
            "LSMinimumSystemVersion": "11.0",
        },
    )
