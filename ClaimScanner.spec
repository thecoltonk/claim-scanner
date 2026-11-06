# PyInstaller build:  pyinstaller ClaimScanner.spec   ->  dist/ClaimScanner/ClaimScanner.exe
from PyInstaller.utils.hooks import collect_all

datas = [("phone.html", "."), ("review.html", "."), ("config.example.py", "."), ("icon.png", "."), ("eclaim-prefill.user.js", ".")]
binaries, hidden = [], []
for pkg in ("rapidocr_onnxruntime", "onnxocr", "onnxruntime", "segno", "pillow_heif", "pypdfium2", "pystray"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h

a = Analysis(["app.py"], datas=datas, binaries=binaries, hiddenimports=hidden + ["yaml"],
             excludes=["tkinter", "matplotlib", "torch", "transformers", "IPython", "pytest"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ClaimScanner",
          console=False,                     # no terminal window; output goes to claim-scanner.log
          icon="icon.ico" if __import__("os").path.exists("icon.ico") else None)
coll = COLLECT(exe, a.binaries, a.datas, name="ClaimScanner")
