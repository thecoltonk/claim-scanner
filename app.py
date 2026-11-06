"""Claim scanner: phone snaps auth forms -> local OCR -> review queue -> eClaim userscript fills.
Everything stays on this laptop. Run: python app.py
"""
import copy, functools, importlib.util, io, json, os, queue, re, secrets, shutil, socket, sqlite3, subprocess, sys, threading, time, urllib.request, zipfile
from datetime import datetime, timedelta

from flask import Flask, Response, abort, jsonify, make_response, redirect, request, send_file
from PIL import Image, ImageOps
from rapidfuzz import fuzz

# Packaged as ClaimScanner.exe: your data (queue, certs, settings) lives next to the exe;
# the app's own files (phone page, models) live inside the bundle.
FROZEN = getattr(sys, "frozen", False)
BASE = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
RES = getattr(sys, "_MEIPASS", BASE)
if FROZEN:                                            # no console window: write what it would have said to a log
    _log = os.path.join(BASE, "claim-scanner.log")
    if os.path.exists(_log) and os.path.getsize(_log) > 5_000_000:
        os.replace(_log, _log + ".old")
    sys.stdout = sys.stderr = open(_log, "a", buffering=1, encoding="utf-8")

def full_speed():
    """Windows 11 throttles apps with no visible window (power throttling / Efficiency mode), which made
    reads ~3x slower as the exe than in a terminal. Opt out for this process."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        class PPTS(ctypes.Structure):
            _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        h = ctypes.c_void_p(k.GetCurrentProcess())
        st = PPTS(1, 0x1 | 0x4, 0)        # control EXECUTION_SPEED + timer resolution, state 0 = never throttle
        ok = k.SetProcessInformation(h, 4, ctypes.byref(st), ctypes.sizeof(st))   # 4 = ProcessPowerThrottling
        k.SetPriorityClass(h, 0x8000)     # ABOVE_NORMAL: keep up while you're busy in Firefox
        print(f"  Power throttling: {'off' if ok else 'could not turn off'}")
    except Exception as e:
        print(f"  Power throttling: could not turn off ({e})")

full_speed()

def _load_config():
    """config.py next to the exe/app.py (made from config.example.py the first time)."""
    path = os.path.join(BASE, "config.py")
    if not os.path.exists(path):
        shutil.copy(os.path.join(RES, "config.example.py"), path)
    spec = importlib.util.spec_from_file_location("config", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules["config"] = mod
    return mod

config = _load_config()

VERSION = "2026-11-05.6"

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

SCANS = os.path.join(BASE, "scans")
DB = os.path.join(BASE, "claims.db")
os.makedirs(SCANS, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024

# ---------------- token for the phone page ----------------
TOKEN_FILE = os.path.join(BASE, "token.txt")
if not os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, "w") as f:
        f.write(secrets.token_urlsafe(8))
TOKEN = open(TOKEN_FILE).read().strip()

# ---------------- storage ----------------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

with db() as c:
    c.execute("CREATE TABLE IF NOT EXISTS claims (id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " created REAL, status TEXT, img TEXT, data TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS billed (auth TEXT PRIMARY KEY, at REAL)")   # auth #s already billed
    c.execute("DELETE FROM billed WHERE at < ?", (time.time() - 180 * 86400,))       # keep ~6 months

def row_dict(r):
    return {"id": r["id"], "created": r["created"], "status": r["status"], "img": r["img"],
            "data": json.loads(r["data"] or "{}")}

def get_claim(cid):
    with db() as c:
        r = c.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone()
    return row_dict(r) if r else None

def update(cid, **kw):
    if "data" in kw:
        kw["data"] = json.dumps(kw["data"])
    sets = ", ".join(f"{k}=?" for k in kw)
    with db() as c:
        c.execute(f"UPDATE claims SET {sets} WHERE id=?", (*kw.values(), cid))

def delete_claim(cid):
    cl = get_claim(cid)
    if cl and cl["img"] and os.path.exists(cl["img"]):
        os.remove(cl["img"])
    with db() as c:
        c.execute("DELETE FROM claims WHERE id=?", (cid,))

def auth_of(data):
    return re.sub(r"\D", "", ((data or {}).get("auth") or {}).get("v", "") or "")

def duplicate_of(cid, auth):
    """'queue' if another scan in the queue has this auth #, 'billed' if it was already billed, else None"""
    if not auth:
        return None
    with db() as c:
        if c.execute("SELECT 1 FROM billed WHERE auth=?", (auth,)).fetchone():
            return "billed"
        for r in c.execute("SELECT id, data FROM claims WHERE status='ready' AND id<>?", (cid,)):
            if auth_of(json.loads(r["data"] or "{}")) == auth:
                return "queue"
    return None

def last_dos():
    with db() as c:
        for r in c.execute("SELECT data FROM claims ORDER BY id DESC LIMIT 20"):
            v = json.loads(r["data"] or "{}").get("dos", {}).get("v")
            if v:
                return v
    return None

# ---------------- name -> gender (SSA baby names, kept local) ----------------
NAMES_JSON = os.path.join(BASE, "names.json")

def load_names():
    """name -> [share female, how many babies got the name]"""
    if os.path.exists(NAMES_JSON):
        with open(NAMES_JSON) as f:
            data = json.load(f)
        if data and isinstance(next(iter(data.values())), list):
            return data                              # current format; older files get rebuilt below
    local_zip = os.path.join(BASE, "names.zip")
    try:
        if os.path.exists(local_zip):
            raw = open(local_zip, "rb").read()
        else:
            print("Downloading SSA name data (one time)...")
            req = urllib.request.Request("https://www.ssa.gov/oact/babynames/names.zip",
                                         headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=90).read()
    except Exception as e:
        print(f"  Couldn't get name data ({e}). Download 'National data' (names.zip) from "
              "ssa.gov/oact/babynames/limits.html into this folder and restart. Gender guesses are off until then.")
        return {}
    counts = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for n in z.namelist():
            m = re.match(r"yob(\d{4})\.txt$", n)
            if not m or int(m.group(1)) < 1930:
                continue
            for line in z.read(n).decode("utf-8", "ignore").splitlines():
                name, sex, cnt = line.split(",")
                fem, tot = counts.get(name.lower(), (0, 0))
                counts[name.lower()] = (fem + (int(cnt) if sex == "F" else 0), tot + int(cnt))
    out = {k: [round(f / t, 3), t] for k, (f, t) in counts.items() if t >= 50}
    with open(NAMES_JSON, "w") as f:
        json.dump(out, f)
    return out

NAMES = load_names()

# ---------------- OCR (PaddleOCR, runs locally) ----------------
_ocr = None
_ocr_err = None

def get_ocr():
    """RapidOCR (PaddleOCR models on ONNX Runtime), set up once.
    - text_score 0.2: handwriting reads at low confidence; the default 0.5 threw those lines away
    - detection capped at 1600px: finding the text doesn't need full resolution (4K went from ~37s to ~2s),
      but each line is still READ from the full-resolution photo"""
    global _ocr, _ocr_err
    if _ocr is not None:
        return _ocr
    if _ocr_err:
        raise RuntimeError(_ocr_err + " (fix it, then restart app.py)")
    try:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR(text_score=0.2)
    except Exception as e:
        _ocr_err = f"{type(e).__name__}: {e}"
        raise
    _ocr.use_angle_cls = False                        # photos are always upright; skip the per-line rotation check
    global READER
    READER = upgrade_reader(_ocr)
    try:                                              # speed-up; RapidOCR versions name this part differently
        det = getattr(_ocr, "text_detector", None) or getattr(_ocr, "text_det", None)
        sized = False
        for op in getattr(det, "preprocess_op", None) or []:
            if type(op).__name__ == "DetResizeForTest":
                op.limit_side_len, op.limit_type = 1600, "max"
                sized = True
        if not sized and det is not None and hasattr(det, "limit_side_len"):
            det.limit_side_len, det.limit_type = 1600, "max"
            sized = True
        if not sized:
            print("  (OCR speed-up not available for this RapidOCR version; it'll be slower. "
                  "Fix: py -m pip install rapidocr_onnxruntime==1.2.3)")
    except Exception:
        pass
    return _ocr

READER = "built-in"
HW_REC = None                                         # PP-OCRv5 line reader for handwriting, if available

def upgrade_reader(o):
    """Load the PP-OCRv5 line reader (pip package onnxocr-ppocrv5) for the handwritten lines: newer model,
    much better on handwriting than the built-in one (which stays for printed text: it's faster).
    Sanity-checked on a printed test date; falls back to the built-in reader if anything's off."""
    try:
        import onnxocr
        mdir = os.path.join(os.path.dirname(onnxocr.__file__), "models", "ppocrv5")
        rec_p, keys_p = os.path.join(mdir, "rec", "rec.onnx"), os.path.join(mdir, "ppocrv5_dict.txt")
        if not (os.path.exists(rec_p) and os.path.exists(keys_p)):
            return "built-in (PP-OCRv5 model files not found in onnxocr-ppocrv5)"
        import copy, yaml, rapidocr_onnxruntime as RO
        from rapidocr_onnxruntime.ch_ppocr_v3_rec import TextRecognizer
        cfg = copy.deepcopy(yaml.safe_load(open(os.path.join(os.path.dirname(RO.__file__), "config.yaml")))["Rec"])
        cfg["model_path"], cfg["keys_path"] = rec_p, keys_p
        new = TextRecognizer(cfg)
        # sanity check on a printed test date before trusting it
        from PIL import ImageDraw, ImageFont
        import numpy as np
        test = Image.new("RGB", (260, 48), "white")
        font = None
        for f in ("arial.ttf", "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            try:
                font = ImageFont.truetype(f, 32)
                break
            except Exception:
                pass
        if font is None:
            try:
                font = ImageFont.load_default(size=32)
            except TypeError:
                font = ImageFont.load_default()
        ImageDraw.Draw(test).text((8, 6), "10/26/26", fill="black", font=font)
        out = new([np.ascontiguousarray(np.array(test)[:, :, ::-1])])
        out = out[0] if isinstance(out, tuple) else out
        got = re.sub(r"\s", "", out[0][0]) if out and out[0] else ""
        if got != "10/26/26":
            return f"built-in (PP-OCRv5 test read {got!r}, not using it)"
        global HW_REC
        HW_REC = new                                  # used for the handwritten lines only (printed text is
        return "PP-OCRv5 for handwriting"             # fine with the faster built-in reader)
    except ImportError:
        return "built-in (PP-OCRv5 not installed: py -m pip install onnxocr-ppocrv5)"
    except Exception as e:
        return f"built-in (PP-OCRv5 not available: {type(e).__name__}: {e})"

def recognizer():
    """Line reader for re-reading handwriting: PP-OCRv5 if loaded, else RapidOCR's built-in one."""
    o = get_ocr()
    return HW_REC or getattr(o, "text_recognizer", None) or getattr(o, "text_rec", None)

def ocr_image(path):
    """Returns [{t: text, s: score, x1, y1, x2, y2}, ...]"""
    result, _ = get_ocr()(path)
    boxes = []
    for pts, t, sc in (result or []):
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        boxes.append({"t": t, "s": float(sc), "x1": float(min(xs)), "y1": float(min(ys)),
                      "x2": float(max(xs)), "y2": float(max(ys))})
    return boxes

def reread(img, b, digits=False):
    """Handwriting: re-read one line several ways (cleanups x sizes). One reading is often wrong;
    together they point at the right answer."""
    import cv2, numpy as np
    H, W = img.shape[:2]
    lh = max(8, int(b["y2"] - b["y1"]))
    py_, px_ = max(2, int(lh * 0.06)), max(6, int(lh * 0.5))
    x1, y1 = max(0, int(b["x1"]) - px_), max(0, int(b["y1"]) - py_)
    x2, y2 = min(W, int(b["x2"]) + px_), min(H, int(b["y2"]) + py_)
    crop = img[y1:y2, x1:x2]
    if crop.size == 0 or crop.shape[0] < 8:
        return []
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    bg = cv2.GaussianBlur(g, (0, 0), 25)
    flat = cv2.divide(g, bg, scale=255)                       # remove shadows
    otsu = cv2.threshold(flat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    variants = [crop, cv2.createCLAHE(2.0, (8, 8)).apply(g), flat, otsu, cv2.erode(otsu, np.ones((2, 2), np.uint8))]
    heights = (48, 64, 96)
    if HW_REC is not None:                            # PP-OCRv5 needs fewer tries (and each one costs more);
        variants, heights = variants[:3], (48,)       # it scales every crop to 48px tall, so other heights were repeats
    imgs = []
    for v in variants:
        v = v if v.ndim == 3 else cv2.cvtColor(v, cv2.COLOR_GRAY2BGR)
        for h in heights:
            sc = h / v.shape[0]
            imgs.append(cv2.resize(v, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC))
    reads = []
    try:
        rec = recognizer()
        if rec is not None:
            out = rec(imgs)
            out = out[0] if isinstance(out, tuple) else out
            reads = [r[0] for r in out if r and isinstance(r[0], str) and r[0]]
    except Exception:
        reads = []
    if digits:                                        # also force the answer to be digits and "/" only
        reads += [r for r in digits_only(imgs) if r]
    dig = lambda r: sum(ch.isdigit() or ch in "/lI|()" for ch in r)
    need = max(3, int(dig(b["t"]) * 0.6))            # a re-read with far fewer digit-ish marks lost the line
    return [r for r in reads if dig(r) >= need]

def digits_only(imgs, allowed="0123456789/"):
    """Same recognizer, but it may only pick digits or '/' (it otherwise reads handwriting as 'ibla/26')."""
    import numpy as np
    try:
        rec = recognizer()
        chars = rec.postprocess_op.character
    except Exception:
        return []
    keep = [0] + [i for i, c in enumerate(chars) if c in allowed]   # 0 = CTC blank
    out = []
    for img in imgs:
        try:
            h, w = img.shape[:2]
            x = rec.resize_norm_img(img, w / h)[np.newaxis, :].astype(np.float32)
            best = rec.session(x)[0][0][:, keep].argmax(1)
            s, last = [], 0
            for k in best:
                if k != last and k != 0:
                    s.append(chars[keep[k]])
                last = k
            out.append("".join(s))
        except Exception:
            pass
    return out

# ---- choosing the best fit for handwritten dates ----
LOOKS = {"0": "oOD()uUQ6", "1": "lI|i/!(7)", "2": "zZ73", "3": "8B2", "4": "9yuAh", "5": "sS6", "6": "bG(50",
         "7": "1)>T2", "8": "3B&", "9": "4gq(yaA1", "/": "1lI|(i/\\)"}

# one handwritten digit read as TWO characters (a 6's stem and loop split into "10", etc.)
SPLITS = {"6": ("10", "l0", "lo", "b0", "1o", "(0", "60"), "4": ("41", "4l", "u1"), "9": ("91", "9l", "q1"),
          "0": ("01", "()"), "8": ("00", "3o"), "1": ("11",)}

def wdist(cand, read):
    """Edit distance where likely handwriting misreads (9->4, /->1, ...) are cheap and slashes may vanish."""
    n, m = len(cand), len(read)
    d = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        d[i][0] = d[i - 1][0] + (0.6 if cand[i - 1] == "/" else 1.0)
    for j in range(1, m + 1):
        d[0][j] = j * 0.8
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            a, b = cand[i - 1], read[j - 1]
            sub = 0.0 if a == b else (0.35 if b in LOOKS.get(a, "") else 1.0)
            d[i][j] = min(d[i - 1][j] + (0.6 if a == "/" else 1.0), d[i][j - 1] + 0.8, d[i - 1][j - 1] + sub)
            if j >= 2 and read[j - 2:j] in SPLITS.get(a, ()):
                d[i][j] = min(d[i][j], d[i - 1][j - 2] + 0.4)
    return d[n][m]

def best_fit(cands, readings):
    """cands: [(value, [ways it could be written])]. Returns (value, sure?) or (None, False)."""
    readings = [re.sub(r"\s+", "", r) for r in readings if r and r.strip()]
    if not readings or not cands:
        return None, False
    scored = sorted((sum(min(wdist(f, r) for f in forms) for r in readings) + bias, val)
                    for val, forms, bias in cands)
    best, second = scored[0], (scored[1] if len(scored) > 1 else (scored[0][0] * 2 + 1, None))
    margin = second[0] - best[0]
    sure = (len(readings) >= 5 and margin >= 0.15 * max(best[0], 1)) or (best[0] < 0.5 and margin >= 0.8)
    return best[1], sure

def date_forms(dt):
    return {f"{dt.month}/{dt.day}/{dt:%y}", f"{dt:%m}/{dt:%d}/{dt:%y}", f"{dt.month}/{dt.day}/{dt.year}"}

# ---------------- parsing ----------------
AUTH_LABELS = getattr(config, "AUTH_LABELS", ["authorization", "auth #"])
NAME_LABELS = getattr(config, "NAME_LABELS", ["patient name"])
MEMBER_LABELS = ["member name"]
DOB_LABELS = ["birth date", "date of birth"]
COPAY_LABELS = ["copayment", "copay"]
DOS_LABELS = ["date of service"]
ALL_LABELS = AUTH_LABELS + NAME_LABELS + MEMBER_LABELS + DOB_LABELS + COPAY_LABELS + DOS_LABELS + [
    "relationship", "benefit", "client name", "eligibility", "coverage", "report prepared"]
NAME_KEYS = list(NAMES)

def norm(s):
    s = s.upper().replace("S", "5").replace("O", "0").replace("I", "1").replace("L", "1")
    return re.sub(r"[^A-Z0-9]", "", s)

DIGITISH = str.maketrans({"S": "5", "s": "5", "O": "0", "o": "0", "D": "0", "I": "1", "l": "1", "i": "1",
                          "|": "1", "B": "8", "Z": "2", "z": "2", "g": "9", "q": "9"})

def label_hit(text, labels):
    """Fuzzy-find a printed label ("Patien+ Name" still counts). Returns (score, index just past it) or None."""
    idx = [i for i, ch in enumerate(text) if not ch.isspace()]
    sq = "".join(text[i] for i in idx).lower()
    for lab in labels:                               # list order = priority
        l = lab.replace(" ", "").lower()
        if len(sq) < 3 or len(l) < 3:
            continue
        al = fuzz.partial_ratio_alignment(l, sq)
        if al and al.score >= 80:
            return al.score, (idx[al.dest_end - 1] + 1 if al.dest_end > 0 else 0)
    return None

def label_end(text, labels):
    h = label_hit(text, labels)
    return h[1] if h else None

def label_boxes(boxes, labels):
    """Boxes containing a label, best match first: [(box, end_index, score)]"""
    hits = [(b, h[1], h[0]) for b in boxes for h in [label_hit(b["t"], labels)] if h]
    return sorted(hits, key=lambda x: -x[2])

def is_any_label(t):
    return label_hit(t, ALL_LABELS) is not None

def value_from(boxes, b, e, parse):
    """Value after the label inside box b, else the nearest box on the same line / to the right / just below."""
    v = parse(b["t"][e:])
    if v:
        return v
    cy, h = (b["y1"] + b["y2"]) / 2, max(b["y2"] - b["y1"], 1)
    best, best_d = None, 1e9
    for o in boxes:
        same_line = abs((o["y1"] + o["y2"]) / 2 - cy) < h and o["x1"] >= b["x1"]
        below = o["x2"] > b["x1"] and o["y1"] > b["y1"] and o["y1"] <= b["y2"] + 2 * h
        if o is b or not (same_line or below) or is_any_label(o["t"]):
            continue
        v = parse(o["t"])
        if not v:
            continue
        d = ((o["x1"] - b["x2"]) ** 2 + ((o["y1"] + o["y2"]) / 2 - cy) ** 2) ** 0.5
        if d < best_d:
            best, best_d = v, d
    return best

def value_near(boxes, labels, parse):
    for b, e, _ in label_boxes(boxes, labels):
        v = value_from(boxes, b, e, parse)
        if v:
            return v
    return None

MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}

def parse_date_any(t):
    """Printed dates: 'March 19, 1975', 'Mar 19 1975', '03/19/1975' -> MM/DD/YYYY"""
    m = re.search(r"([A-Za-z]{3,9})\.?\s*(\d{1,2}),?\s*(\d{4})", t)
    if m and m.group(1)[:3].lower() in MONTHS:
        mo, d, y = MONTHS[m.group(1)[:3].lower()], int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{1,2})\s*[/-]\s*(\d{1,2})\s*[/-]\s*(\d{4})", t.translate(DIGITISH))
        if not m:
            return None
        mo, d, y = map(int, m.groups())
    if not (1900 <= y <= datetime.now().year + 1):   # "7211" is not a year
        return None
    try:
        return datetime(y, mo, d).strftime("%m/%d/%Y")
    except ValueError:
        return None

def parse_money(t):
    t = t.translate(str.maketrans("Oo", "00"))
    m = re.search(r"\$?\s*(\d{1,4})[.,](\d{2})\b", t)
    if m:
        return f"{int(m.group(1))}.{m.group(2)}"
    m = re.search(r"[$S]\s*(\d{1,4})\b(?![.,]\d)", t)          # "$0", "$10"
    return f"{int(m.group(1))}.00" if m else None

def guess_copay(boxes):
    hits = sorted(label_boxes(boxes, COPAY_LABELS), key=lambda x: x[0]["y1"])   # topmost = Exam section
    for b, e, _ in hits:
        v = value_from(boxes, b, e, parse_money)
        if v:
            return {"v": v, "flag": "ok"}
    # fallback: the copay is the only dollar amount on the form ($ may be read as S)
    amounts = []
    for b in sorted(boxes, key=lambda b: b["y1"]):  # topmost first (this form's Exam box)
        for m in re.finditer(r"[$S]\s*(\d{1,4})(?:\s*[.,]\s*(\d{2}))?\b", b["t"]):
            amounts.append(f"{int(m.group(1))}.{m.group(2) or '00'}")
    if amounts:
        return {"v": amounts[0], "flag": "ok" if len(set(amounts)) == 1 else "warn"}
    return {"v": "", "flag": "missing"}

HW_DATE = re.compile(r"(1[0-2]|0?[1-9])\s*[/\\|lI\-.]\s*(3[01]|[12]\d|0?[1-9])\s*[/\\|lI\-.]\s*(\d{4}|\d{2})")

MONTH_YEAR = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(?:(\d{1,2}),\s*)?((?:19|20)\d{2})", re.I)

def printed_month_year(t):
    """VSP prints DOBs as 'March 1972'. -> (month, year, rest_of_text_after_it) or (None, None, '')"""
    m = MONTH_YEAR.search(t)
    if m:
        return MONTHS[m.group(1)[:3].lower()], int(m.group(3)), t[m.end():]
    return None, None, ""

def glued_dob(rest, pm, py):
    """Staff's handwriting glued onto the printed month/year with the slashes lost:
    'March1972' + '3177' (= 3/17/72). Month and year come from the print, the day from the digits."""
    d = re.sub(r"\D", "", rest.translate(str.maketrans("OoDlI|", "000111")))
    for mp in (f"{pm:02d}", str(pm)):
        if d.startswith(mp):
            d = d[len(mp):]
            break
    else:
        return None
    for n in (2, 1):                                # "177" -> day 17 (the rest is the year)
        if len(d) >= n and 1 <= int(d[:n]) <= 31:
            try:
                return datetime(py, pm, int(d[:n])).strftime("%m/%d/%Y")
            except ValueError:
                pass
    return None

def guess_insured(boxes):
    """Member Name row present = dependent. Insured DOB = staff's handwritten MM/DD/YY on that row,
    checked against VSP's printed month/year (which also settles the century)."""
    cy = lambda b: (b["y1"] + b["y2"]) / 2
    mem = label_boxes(boxes, MEMBER_LABELS)
    n_dob = len({id(b): b for b, _, _ in label_boxes(boxes, DOB_LABELS)})
    # Two signals: a "Member Name" row, and two Birth Date lines (patient's + holder's). A ✓ needs both to agree.
    # (One dependent was billed as Self with a ✓ because the Member Name label didn't read.)
    choices = {"top": ["Dependent", "Self"]}
    if not mem:
        if n_dob < 2:
            return {"v": False, "flag": "ok"}, {"v": "", "flag": "ok"}
        mb = sorted({id(b): b for b, _, _ in label_boxes(boxes, DOB_LABELS)}.values(), key=cy)[-1]   # the holder's line
        dep_flag = "warn"
    else:
        mb = mem[0][0]
        dep_flag = "ok" if n_dob >= 2 else "warn"
    lh = max(mb["y2"] - mb["y1"], 1)
    row_y = (mb["y1"] + mb["y2"]) / 2
    # The member's Birth Date: VSP prints the patient's Birth Date first and the member's below it.
    # (Long names wrap, so "closest to the Member Name label" can land on the patient's line.)
    all_dobs = sorted({id(b): b for b, _, _ in label_boxes(boxes, DOB_LABELS)}.values(), key=cy)
    dobs = [b for b in all_dobs if abs(cy(b) - row_y) < 3 * lh]
    if len(dobs) >= 2:
        dob_lab = dobs[-1]                            # the lower one = the member's
    elif dobs:
        dob_lab = dobs[0]
        pat = label_boxes(boxes, NAME_LABELS)         # only one found: make sure it isn't the patient's
        if pat and cy(dob_lab) < (cy(pat[0][0]) + row_y) / 2 and len(all_dobs) == 1:
            dob_lab = None
    else:
        dob_lab = None
    anchor_y = cy(dob_lab) if dob_lab else row_y
    anchor_x = dob_lab["x1"] if dob_lab else mb["x2"]
    near = sorted((b for b in boxes if -1.8 * lh < cy(b) - anchor_y < 2.2 * lh and b["x2"] > anchor_x - 20),
                  key=lambda b: abs(cy(b) - anchor_y))

    pm = py = None                                   # printed month/year: on the member's Birth Date line
    rest = ""
    for b in near:
        if dob_lab is not None and abs(cy(b) - anchor_y) > 0.7 * lh:
            continue
        pm, py, rest = printed_month_year(b["t"])
        if pm:
            break
    hand, best = None, 1e9                           # handwritten MM/DD/YY closest to the member row
    this_year = datetime.now().year
    for b in near:
        t = b["t"].translate(str.maketrans("OoD", "000"))
        for m in HW_DATE.finditer(t):
            if re.search(r"[A-Za-z]{3,}", t[:m.start()]):
                continue
            hm, hd, ys = int(m.group(1)), int(m.group(2)), m.group(3)
            if len(ys) == 4 and not (1900 <= int(ys) <= this_year):
                ys = ys[:2]                          # "7211" = "72" + stray marks
            score = abs(cy(b) - anchor_y) + (0 if pm is None or hm == pm else 1000)   # month must match VSP's
            if abs(cy(b) - anchor_y) > 0.8 * lh and (pm is None or hm != pm):
                continue                             # off the member's line and doesn't match: patient's DOB
            if score < best:
                hand, best = (hm, hd, int(ys)), score

    dep = {"v": True, "flag": dep_flag, **({"ctc": choices} if dep_flag != "ok" else {})}
    hint = f"{pm:02d}/??/{py}" if pm else ""
    line = [b for b in near if -0.6 * lh <= cy(b) - anchor_y <= 1.7 * lh
            and (MONTH_YEAR.search(b["t"]) or sum(ch.isdigit() for ch in b["t"]) >= 3)]
    area = union(line + ([dob_lab] if dob_lab else []))
    hw_lines = []                                     # [x1, y1, x2, y2, start fraction] of the handwriting itself
    for b in line:
        m2 = MONTH_YEAR.search(b["t"])
        if m2:
            tail = b["t"][m2.end():]
            if sum(ch.isdigit() for ch in tail) >= 3:    # handwriting glued onto "April1983"
                hw_lines.append([b["x1"], b["y1"], b["x2"], b["y2"], m2.end() / max(1, len(b["t"]))])
        elif not re.search(r"[A-Za-z]{3,}", b["t"]):
            hw_lines.append([b["x1"], b["y1"], b["x2"], b["y2"], 0.0])

    def out(v, flag="warn"):
        # The staff's handwriting is the weakest read on the form, and the reader can be
        # confidently wrong (4/26 read as 4/30 six times), so a handwritten DOB is "check it"
        # unless the constrained reading later says it's clear.
        return {"v": v, "flag": flag, "printed": hint, "box": area, "hw": hw_lines, "pm": pm, "py": py}

    def year_for(hm, hy):
        if py and (pm is None or hm == pm or abs(hy % 100 - py % 100) <= 1):
            return py                                # printed year is reliable
        if 1900 <= hy <= this_year:
            return hy
        return 1900 + hy if hy > this_year % 100 else 2000 + hy

    # 1) clean handwritten date on its own, month matches VSP's -> take it
    if hand and pm and hand[0] == pm:
        try:
            return dep, out(datetime(year_for(*hand[::2]), hand[0], hand[1]).strftime("%m/%d/%Y"))
        except ValueError:
            pass
    # 2) glued onto "March1972" but the slashes survived ("3/27/72", "03l27l72")
    if pm and rest:
        m = HW_DATE.search(rest.translate(str.maketrans("OoD", "000")))
        if m and int(m.group(1)) == pm:
            try:
                return dep, out(datetime(py, pm, int(m.group(2))).strftime("%m/%d/%Y"))
            except ValueError:
                pass
    # 3) messy: best day in VSP's month/year against every reading of the member's line only
    if pm:
        readings = []
        for b in near:
            dy = cy(b) - anchor_y
            if dy < -0.6 * lh or dy > 1.7 * lh:       # member's line or just under it (patient's line is above)
                continue
            for t in [b["t"]] + b.get("alts", []):
                m2 = MONTH_YEAR.search(t)
                tail = t[m2.end():] if m2 else (t if not re.search(r"[A-Za-z]{3,}", t) else "")
                if sum(ch.isdigit() for ch in tail) >= 3:
                    readings.append(tail)
        if readings:
            cands = []
            for day in range(1, 32):
                try:
                    cands.append((datetime(py, pm, day), date_forms(datetime(py, pm, day)), 0))
                except ValueError:
                    pass
            best, sure = best_fit(cands, readings)
            if best:
                return dep, out(best.strftime("%m/%d/%Y"))
    # 4) handwritten date whose month disagrees with VSP's -> show it, flagged
    if hand:
        try:
            return dep, out(datetime(year_for(*hand[::2]), hand[0], hand[1]).strftime("%m/%d/%Y"))
        except ValueError:
            pass
    for b in near:                                   # some forms print the whole date (member's line only)
        if abs(cy(b) - anchor_y) > 0.8 * lh:
            continue
        v = parse_date_any(b["t"])
        if v:
            return dep, out(v)
    return dep, out("", "missing")

def parse_auth(t):
    m = re.search(config.AUTH_VALUE, re.sub(r"\s+", "", t.translate(DIGITISH)))
    return m.group(0) if m else None

def guess_auth(boxes, cut):
    v = value_near(boxes, AUTH_LABELS, parse_auth)
    if v:
        b = next((b for b in boxes if parse_auth(b["t"]) == v), None)
        return {"v": v, "flag": "ok", **({"box": [b["x1"], b["y1"], b["x2"], b["y2"]]} if b else {})}
    cands = [x for b in boxes if b["y1"] < cut for x in [parse_auth(b["t"])] if x]
    return {"v": max(cands, key=len), "flag": "warn"} if cands else {"v": "", "flag": "missing"}

def parse_name(t):
    t = t.strip(" :#.-+=;")
    words = [w for w in re.findall(r"[A-Za-z]{2,}", t) if fuzz.ratio(w.lower(), "name") < 75]
    return t if words else None

def _sex(p, conf_ok):
    conf = max(p, 1 - p)
    return "F" if p >= 0.5 else "M", ("ok" if conf >= config.GENDER_SURE and conf_ok else "warn"), round(conf, 2)

def guess_gender(boxes, labels=None):
    """VSP prints 'Patient Name: FIRST LAST' / 'Member Name: FIRST LAST': the first word is the first name."""
    if not NAMES:
        return {"v": "", "flag": "missing", "name": "", "note": "name list not loaded: put names.zip in the folder and restart"}
    raw = value_near(boxes, labels or NAME_LABELS, parse_name)
    if not raw:
        return {"v": "", "flag": "missing", "name": ""}
    words = [w for w in re.findall(r"[A-Za-z]{2,}", raw) if fuzz.ratio(w.lower(), "name") < 75]
    if not words:
        return {"v": "", "flag": "missing", "name": raw.strip()}
    first = words[0].lower()
    if first in NAMES:                               # normal case
        sex, flag, conf = _sex(NAMES[first][0], True)
        return {"v": sex, "flag": flag, "name": first.title(), "conf": conf}
    for k in range(len(first) - 1, 2, -1):           # glued "EMMASAMPLE" -> "emma" (VSP prints names without spaces)
        if first[:k] in NAMES:
            p, tot = NAMES[first[:k]]
            # sure if it's a common first name (not a rare one that happens to fit) and clearly one sex
            sex, flag, conf = _sex(p, k >= 4 and tot >= 5000 and len(first) - k >= 2)
            return {"v": sex, "flag": flag, "name": first[:k].title(), "conf": conf}
    if NAME_KEYS:                                    # OCR typo in the first name ("Jame") -> closest real name
        from rapidfuzz import process
        hit = process.extractOne(first, NAME_KEYS, scorer=fuzz.ratio, score_cutoff=75 if len(first) <= 5 else 80)
        if hit:
            sex, _, conf = _sex(NAMES[hit[0]][0], False)
            return {"v": sex, "flag": "warn", "name": f"{hit[0].title()}? (read '{words[0]}')", "conf": conf}
    return {"v": "", "flag": "missing", "name": words[0]}   # don't guess from the last name

def guess_hcodes(boxes, cut):
    codes = {c: norm(c) for c in config.H_CODES}
    def line_codes(t):
        """Codes found in one reading of a line: {code: (score, ambiguous)}"""
        found = {}
        toks = re.split(r"[\s;]+", t) + re.findall(
            r"[A-Za-z#]?\s*[0-9SsOoIlZ]{2}\s*[.,]\s*[0-9SsOoIlZz]{1,3}", t)   # "H52.13myopia" -> "H52.13"
        for tok in toks:
            nt = norm(tok)
            if len(nt) < 3:
                continue
            scores = sorted(((fuzz.ratio(nt, n), c) for c, n in codes.items()), reverse=True)
            sc, code = scores[0]
            if sc < config.H_MATCH_MIN:
                continue
            close = len(scores) > 1 and sc - scores[1][0] < 10
            if code not in found or sc > found[code][0]:
                found[code] = (sc, close)
        return found

    def scan(cands):
        """One handwritten line = one code (majority vote across its readings), unless the line itself
        clearly has two codes written on it. Re-reads are votes for the same line, not extra codes."""
        best = {}                                    # code -> (score, ambiguous)
        for b in cands:
            first = line_codes(b["t"])
            reads = [first] + [line_codes(t) for t in b.get("alts", [])]
            from itertools import combinations
            sets = [r for r in reads if r]
            pairs = {}
            for r in sets:                            # two codes on one line: the same pair in most readings
                for pr in combinations(sorted(r), 2):
                    pairs[pr] = pairs.get(pr, 0) + 1
            pair = max(pairs, key=pairs.get) if pairs else None
            if pair and pairs[pair] >= max(1, 0.5 * len(sets)) and (len(sets) > 1 or all(
                    first[c][0] >= config.H_MATCH_SURE for c in pair)):
                picks = {c: max((r[c] for r in sets if c in r), key=lambda v: v[0]) for c in pair}
            else:
                votes, top = {}, {}
                for r in reads:
                    if not r:
                        continue
                    code = max(r, key=lambda c: r[c][0])
                    votes[code] = votes.get(code, 0) + 1
                    if code not in top or r[code][0] > top[code][0]:
                        top[code] = r[code]
                if not votes:
                    continue
                win = max(votes, key=lambda c: (votes[c], top[c][0]))
                split = len(votes) > 1 and votes[win] < 0.75 * sum(votes.values())   # readings disagree
                picks = {win: (top[win][0], top[win][1] or split)}
            for code, v in picks.items():
                if code not in best or v[0] > best[code][0]:
                    best[code] = v
                    dx_boxes[code] = b
        return best
    dx_boxes = {}
    best = scan([b for b in boxes if b["y1"] >= cut]) or scan(boxes)   # handwritten area first, then anywhere
    if not best:                                     # code unreadable -> go by the word ("myopia")
        words = getattr(config, "H_CODE_WORDS", {})
        hits = {}
        for bx in boxes:
            if bx["y1"] < cut or is_any_label(bx["t"]):
                continue
            letters = re.sub(r"[^a-z]", "", bx["t"].lower())     # "52.13wnyopla" -> "wnyopla"
            if len(letters) < 5:
                continue
            for word, code in words.items():
                sc = max(fuzz.ratio(letters, word), fuzz.partial_ratio(word, letters) if len(letters) >= len(word) else 0)
                if sc >= 65 and sc > hits.get(code, 0):
                    hits[code] = sc
                    dx_boxes[code] = bx
        if hits:                                     # best word wins; similar words (myopia/hyperopia) -> one pick
            code = max(hits, key=hits.get)
            return {"v": [code], "flag": "warn", "via": "word", "box": union([dx_boxes.get(code)])}
        return {"v": [], "flag": "missing"}
    picks = sorted(best, key=lambda c: -best[c][0])
    sure = all(best[c][0] >= config.H_MATCH_SURE and not best[c][1] for c in picks)
    return {"v": picks, "flag": "ok" if sure else "warn", "box": union([dx_boxes.get(c) for c in picks])}

# Handwritten slashes often come back as 1, l, |, u, z...
_SEP = r"[/\\|1lIiuUzZ.\-]"
DOS_RE = re.compile(r"(1[0-2]|0?[1-9])(" + _SEP + r")(3[01]|[12]\d|0?[1-9])(?:(" + _SEP + r")(\d{4}|\d{2}))?")

CONFUSE = {"1": "794", "7": "12", "9": "14", "4": "9", "2": "7", "3": "8", "8": "36", "5": "6", "6": "50", "0": "6"}

def fit_printed(mo, d, year, printed):
    """Handwritten date vs printed date: the visit is on or after the printed date, within ~2 months.
    If the reading lands outside that, try common digit mix-ups (9 read as 1, etc.)."""
    lo = datetime.strptime(printed, "%m/%d/%Y")
    ok = lambda dt: dt and lo <= dt <= lo + timedelta(days=60)
    def mk(m, dd):
        for y in ([year] if year else []) + [lo.year, lo.year + 1]:
            try:
                return datetime(y, m, dd)
            except ValueError:
                pass
    dt = mk(mo, d)
    if ok(dt):
        return dt, False
    best = None
    for which in ("d", "m"):
        sv = f"{d if which == 'd' else mo}"
        for i, ch in enumerate(sv):
            for alt in CONFUSE.get(ch, ""):
                nv = int(sv[:i] + alt + sv[i + 1:])
                cand = mk(mo, nv) if which == "d" else mk(nv, d)
                if ok(cand) and (best is None or cand < best):
                    best = cand
    return best, True

def dos_by_fit(boxes, cut, printed):
    """Score every date from the printed one to ~6 weeks later against all readings of the handwritten lines."""
    readings, used = [], []
    for b in boxes:
        if b["y1"] < cut or is_any_label(b["t"]):
            continue
        t = b["t"].strip()
        digitish = sum(ch.isdigit() or ch in "/lI|()" for ch in t)
        if digitish < 4 or re.match(r"\s*[hH#A]\s*[5S]", t):            # not a date / an H code
            continue
        if re.search(r"[5S]\s*2\s*[.,]", t) or re.search(r"[A-Za-z]{3,}", t):   # "152.13 myopia" = the Dx line
            continue
        if looks_cpt(t):                                                   # the doctor's "92004"
            continue
        readings += [t] + b.get("alts", [])
        used.append(b)
    if not readings:
        return None
    lo = datetime.strptime(printed, "%m/%d/%Y")
    cands = [(lo + timedelta(days=k), date_forms(lo + timedelta(days=k)), k * 0.02) for k in range(46)]
    best, sure = best_fit(cands, readings)
    return {"v": best.strftime("%m/%d/%Y"), "flag": "ok" if sure else "warn", "box": union(used),
            "hw": [[b["x1"], b["y1"], b["x2"], b["y2"], 0.0] for b in used]} if best else None

def guess_dos(boxes, cut, printed=None):
    if printed:
        r = dos_by_fit(boxes, cut, printed)
        if r:
            return r
        return {"v": printed, "flag": "warn", "note": "no handwritten date found, used printed"}
    last, today = last_dos(), datetime.now()
    for b in sorted((b for b in boxes if b["y1"] >= cut), key=lambda b: b["y1"]):
        t = b["t"]
        if re.match(r"\s*[hH#]", t) or re.search(r"dob|birth", t, re.I):
            continue
        sq = re.sub(r"\s+", "", t).translate(str.maketrans("OoD", "000"))
        for m in DOS_RE.finditer(sq):
            mo, d, yr = int(m.group(1)), int(m.group(3)), m.group(5)
            clean = m.group(2) in "/-" and (m.group(4) or "/") in "/-"
            dt = None
            if yr:
                y = int(yr) + (2000 if len(yr) == 2 else 0)
                try:
                    dt = datetime(y, mo, d)
                except ValueError:
                    dt = None
                if dt and (dt > today or (today - dt).days > 365):
                    dt = None
            if dt is None:                           # year unreadable -> most recent past occurrence
                clean = False
                try:
                    dt = datetime(today.year, mo, d)
                    if dt > today:
                        dt = datetime(today.year - 1, mo, d)
                except ValueError:
                    continue
            if printed:                              # sanity-check against the printed date of service
                fixed, changed = fit_printed(dt.month, dt.day, dt.year if yr else None, printed)
                if fixed is None:
                    return {"v": printed, "flag": "warn", "note": "handwritten date didn't fit, used printed"}
                return {"v": fixed.strftime("%m/%d/%Y"), "flag": "ok" if (clean and not changed) else "warn"}
            v = dt.strftime("%m/%d/%Y")
            near_last = not last or abs((dt - datetime.strptime(last, "%m/%d/%Y")).days) <= 14
            return {"v": v, "flag": "ok" if (clean and near_last) else "warn"}
    return {"v": last, "flag": "warn"} if last else {"v": "", "flag": "missing"}

def union(bs):
    """One box around several (for showing that bit of the photo in the panel)."""
    bs = [b for b in bs if b]
    if not bs:
        return None
    return [min(b["x1"] for b in bs), min(b["y1"] for b in bs), max(b["x2"] for b in bs), max(b["y2"] for b in bs)]

def looks_cpt(t):
    """The doctor's handwritten '92004' ('9700y', '9200Y'): a 4-6 digit run starting with 9, no slashes."""
    d = re.sub(r"\D", "", t.translate(DIGITISH).replace("y", "4").replace("Y", "4"))
    return "/" not in t and (bool(re.fullmatch(r"9\d{3,5}", d)) or
                             any(fuzz.ratio(d, l["code"]) >= 60 for l in config.CPT_LINES))

def handwriting_cut(boxes, h):
    if boxes:
        top, bot = min(b["y1"] for b in boxes), max(b["y2"] for b in boxes)
    else:
        top, bot = 0, h
    cut = top + (bot - top) * config.HANDWRITTEN_TOP
    # handwriting goes under this form's Exam box; "Coverage" is its last row (topmost one = this form)
    cov = sorted(b["y2"] for b, _, _ in label_boxes(boxes, ["coverage"]))
    cop = sorted(b["y2"] for b, _, _ in label_boxes(boxes, ["copayment"]))
    if cov:
        cut = max(cut, cov[0] + 5)
    elif cop:
        cut = max(cut, cop[0] + 40)
    return cut

def zone_end(boxes, cut):
    """Where this form's handwriting area ends: the next printed label below it (e.g. another sheet)."""
    below = [b["y1"] for b in boxes if b["y1"] > cut + 40 and is_any_label(b["t"])]
    return min(below) if below else 1e9

def add_rereads(path, boxes, h):
    """Re-read the lines that are (or contain) handwriting: everything under the Exam box,
    and the member's birth-date line (printed month/year + staff's MM/DD/YY)."""
    import cv2
    img = cv2.imread(path)
    if img is None:
        return
    cut = handwriting_cut(boxes, h)
    end = zone_end(boxes, cut)
    cpts = [l["code"] for l in config.CPT_LINES]
    for b in boxes:
        t = b["t"]
        if b["y1"] >= end:
            continue
        if MONTH_YEAR.search(t) and not re.search(r"prepar|report|service", t, re.I):   # birth-date lines
            b["alts"] = reread(img, b)
        elif not re.search(r"[A-Za-z]{3,}", t) and sum(ch.isdigit() for ch in t) >= 4 and b["y1"] < cut:
            b["alts"] = reread(img, b, digits=True)  # staff's handwritten DOBs sit on their own line too
        elif b["y1"] >= cut and not is_any_label(t):  # under the Exam box: only the handwritten DATE needs help
            digitish = sum(ch.isdigit() or ch in "/lI|()" for ch in t)
            looks_dx = re.match(r"\s*[hH#A]\s*[5S]", t)
            if digitish >= 4 and not looks_dx and not looks_cpt(t):
                b["alts"] = reread(img, b, digits=True)
            elif looks_dx or re.search(r"\d{2}\s*[.,]\s*\d", t):   # the Dx line ("H52.13 myopia")
                b["alts"] = reread(img, b)

def parse(boxes, h):
    # Measure the handwritten cutoff on the page itself (where text is), not the whole photo
    cut = handwriting_cut(boxes, h)
    end = zone_end(boxes, cut)
    hw = [b for b in boxes if b["y1"] < end]          # ignore anything past this form's handwriting area
    printed = value_near(boxes, DOS_LABELS, parse_date_any)
    dos = guess_dos(hw, cut, printed)
    if dos["flag"] == "missing" and printed:          # handwriting unreadable -> printed DOS, flagged
        dos = {"v": printed, "flag": "warn"}
    if printed:
        dos["printed"] = printed
    dependent, insured_dob = guess_insured(boxes)
    insured_gender = guess_gender(boxes, MEMBER_LABELS) if dependent["v"] else {"v": "", "flag": "ok"}
    return {"auth": guess_auth(boxes, cut), "gender": guess_gender(boxes),
            "hcodes": guess_hcodes(hw, cut), "dos": dos, "copay": guess_copay(boxes),
            "dependent": dependent, "insured_dob": insured_dob, "insured_gender": insured_gender,
            "zone": {"cut": cut, "end": end},
            "raw": [b["t"] + (f"   [re-read: {' | '.join(b['alts'][:6])}]" if b.get("alts") else "") for b in boxes]}

# ---------------- constrained reading ----------------
# Instead of letting the reader spell out the handwriting one character at a time (it writes "10124126"
# for 10/29/26), give it the list of answers that are possible and ask how well each one fits the ink.
# CTC scoring = the exact probability the reader assigns to a given string, over every way of lining it up.

def line_probs(img, box):
    """Per-time-step character probabilities for one handwritten line, a few ways (cleanups x sizes)."""
    import cv2, numpy as np
    rec = recognizer()
    if rec is None:
        return []
    H, W = img.shape[:2]
    bx1, by1, bx2, by2, frac = box
    bx1 = bx1 + (bx2 - bx1) * frac - (8 if frac else 0)   # glued onto printed text: start where the handwriting starts
    lh = max(8, int(by2 - by1))
    crops = []
    for py_, px_ in ((0.06, 0.5), (0.3, 0.7)):        # tight, and roomy (so the bottom of a 2 or loop of a 6 isn't cut)
        x1, y1 = max(0, int(bx1 - lh * px_)), max(0, int(by1 - max(2, lh * py_)))
        x2, y2 = min(W, int(bx2 + lh * px_)), min(H, int(by2 + max(2, lh * py_)))
        c = img[y1:y2, x1:x2]
        if c.size and c.shape[0] >= 8 and c.shape[1] >= 8:
            crops.append(c)
    variants = []
    for crop in crops:
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        variants += [crop, cv2.cvtColor(cv2.divide(g, cv2.GaussianBlur(g, (0, 0), 25), scale=255), cv2.COLOR_GRAY2BGR)]
    out = []
    for v in variants:
        for hh in (48,):                              # (the reader scales to 48px itself; 64 was a near-repeat)
          for sx in (1.0, 2.0):                         # also stretched 2x wide: more reading steps per digit
            sc = hh / v.shape[0]
            im = cv2.resize(v, (max(8, int(v.shape[1] * sc * sx)), hh), interpolation=cv2.INTER_CUBIC)
            try:
                x = rec.resize_norm_img(im, im.shape[1] / im.shape[0])[np.newaxis].astype(np.float32)
                P = rec.session(x)[0][0]
                out.append(np.log(np.clip(P, 1e-12, 1.0)))
            except Exception:
                pass
    return out

def ctc_logp(lp, text, idx):
    """log P(text | ink) under the reader (CTC forward algorithm). lp: (time, chars) log-probs; 0 = blank."""
    import numpy as np
    lab = [idx.get(c) for c in text]
    if not lab or any(l is None for l in lab):
        return -np.inf
    ext = [0]
    for l in lab:
        ext += [l, 0]
    ext = np.array(ext)
    S, T = len(ext), lp.shape[0]
    if T < len(lab):
        return -np.inf
    skip = np.zeros(S, bool)
    skip[2:] = (ext[2:] != 0) & (ext[2:] != ext[:-2])
    a = np.full(S, -np.inf)
    a[0], a[1] = lp[0, ext[0]], lp[0, ext[1]]
    neg = np.array([-np.inf])
    for t in range(1, T):
        s1 = np.concatenate([neg, a[:-1]])
        s2 = np.concatenate([neg, neg, a[:-2]])
        s2[~skip] = -np.inf
        a = np.logaddexp(np.logaddexp(a, s1), s2) + lp[t, ext]
    return float(np.logaddexp(a[-1], a[-2]))

def pick(img, lines, cands):
    """cands: [(value, [ways it could be written])]. Scores every candidate on every handwritten line
    (summed over the cleanup/size variants) and returns (best value, margin per variant, runner-up) or None."""
    import numpy as np
    rec = recognizer()
    try:
        chars = rec.postprocess_op.character
    except Exception:
        return None
    idx = {c: i for i, c in enumerate(chars)}
    best_line = None
    for box in lines:
        Ps = line_probs(img, box)
        if not Ps:
            continue
        scored = []
        for val, forms in cands:
            s = sum(max(ctc_logp(P, f, idx) for f in forms) for P in Ps)
            scored.append((s, val))
        scored.sort(key=lambda x: -x[0])
        if not np.isfinite(scored[0][0]):
            continue
        frames = sum(P.shape[0] for P in Ps)
        fit = scored[0][0] / frames                   # how well the best answer explains this line's ink
        second = scored[1][0] if len(scored) > 1 else scored[0][0] - 50
        margin = (scored[0][0] - second) / len(Ps)
        top = [v for sc, v in scored[:4] if np.isfinite(sc)]
        if best_line is None or fit > best_line[0]:
            best_line = (fit, scored[0][1], margin, top)
    return best_line[1:] if best_line else None

CTC_SURE = {"dos": 2.0, "insured_dob": 3.0, "hcodes": 9.0}   # margin per variant needed for a ✓ (log units)
DX_SURE_WITH_AI = 9.0       # Dx: even when the AI agrees, the built-in reader must be this sure for a ✓
                            # (two "both agreed" Dx were wrong at 4.5 and 7.0; right ones on clean writing were 12+)

def refine(path, data):
    """Re-decide DOS, holder DOB and Dx by scoring the possible answers against the ink."""
    import cv2
    if recognizer() is None:
        return
    img = cv2.imread(path)
    if img is None:
        return
    # Date of service: every day from the printed date to ~6 weeks after
    dos = data.get("dos") or {}
    if dos.get("hw"):
        lo = datetime.strptime(dos["printed"], "%m/%d/%Y") if dos.get("printed") else datetime.now() - timedelta(days=60)
        cands = [((lo + timedelta(days=k)).strftime("%m/%d/%Y"), date_forms(lo + timedelta(days=k))) for k in range(46)]
        r = pick(img, dos["hw"], cands)
        if r:
            v, margin, top = r
            dos.update(v=v, flag="ok" if margin >= CTC_SURE["dos"] else "warn", via="ctc",
                       ctc={"margin": round(margin, 2), "top": top})
    # Holder DOB: every day in VSP's printed month and year
    dob = data.get("insured_dob") or {}
    if dob.get("hw") and dob.get("pm") and dob.get("py"):
        pm, py = dob["pm"], dob["py"]
        cands = []
        for day in range(1, 32):
            try:
                dt = datetime(py, pm, day)
            except ValueError:
                continue
            cands.append((dt.strftime("%m/%d/%Y"), date_forms(dt) | {f"{dt:%m}/{dt:%d}/{dt.year}"}))
        r = pick(img, dob["hw"], cands)
        if r:
            v, margin, top = r
            dob.update(v=v, flag="ok" if margin >= CTC_SURE["insured_dob"] else "warn", via="ctc",
                       ctc={"margin": round(margin, 2), "top": top})
    # Dx: the 4 codes, with or without the word the doctor writes after them
    hx = data.get("hcodes") or {}
    if hx.get("box") and len(hx.get("v") or []) <= 1:
        words = {c: w for w, c in getattr(config, "H_CODE_WORDS", {}).items()}
        cands = []
        for c in config.H_CODES:
            forms = {c, c.replace("H", "#"), c.replace(".", "")}
            if c in words:
                forms |= {f"{c} {words[c]}", f"{c}{words[c]}"}
            cands.append((c, forms))
        r = pick(img, [list(hx["box"]) + [0.0]], cands)
        if r:
            v, margin, top = r
            hx.update(v=[v], flag="ok" if margin >= CTC_SURE["hcodes"] else "warn", via="ctc",
                      ctc={"margin": round(margin, 2), "top": top})

# ---------------- local AI (LM Studio) as a second reader ----------------
# Qwen3-VL on the gaming PC reads each handwritten field from a small crop, told what it's looking at.
# Its answer must pass the same rules (date window, VSP's month/year, one of the 4 codes); then it votes
# with the built-in reader: agree -> ✓, disagree -> AI's answer but ⚠ so you glance.
_ai = {"model": None, "down_until": 0.0, "status": "off"}
MONTH_NAMES = "January February March April May June July August September October November December".split()

def ai_url():
    return (getattr(config, "AI_URL", "") or "").rstrip("/")

def ai_call(path, body=None, timeout=20):
    import urllib.request
    req = urllib.request.Request(ai_url() + path, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def ai_ready():
    """Is the AI reachable? Checks at most once a minute when it's down, so a sleeping PC never slows scans."""
    if not ai_url():
        _ai["status"] = "off (no AI_URL in config.py)"
        return False
    if _ai["model"]:
        return True
    if time.time() < _ai["down_until"]:
        return False
    try:
        models = [m["id"] for m in ai_call("/v1/models", timeout=3)["data"]]
        vis = [m for m in models if "vl" in m.lower() or "vision" in m.lower()]
        _ai["model"] = (vis or models or [None])[0]
        _ai["status"] = f"{_ai['model']} at {ai_url()}" if _ai["model"] else "no model loaded in LM Studio"
        return bool(_ai["model"])
    except Exception as e:
        _ai["down_until"] = time.time() + 60
        _ai["status"] = f"not reachable at {ai_url()} ({type(e).__name__}); using the built-in reader"
        return False

_last_ai = {"text": ""}

AI_CACHE = {}                                         # (prompt, image) -> answer, for the claim being read

def ai_read(im, prompt):
    import base64, hashlib
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=92)
    key = (prompt, hashlib.sha1(buf.getvalue()).hexdigest())
    if key in AI_CACHE:                               # already asked (while the laptop was busy)
        _last_ai["text"], ans = AI_CACHE[key]
        return ans
    ans = _ai_ask(buf, prompt)
    AI_CACHE[key] = (_last_ai["text"], ans)
    return ans

def _ai_ask(buf, prompt):
    import base64
    b64 = base64.b64encode(buf.getvalue()).decode()
    try:
        out = ai_call("/v1/chat/completions", {
            "model": _ai["model"], "temperature": 0, "max_tokens": 24,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]},
            timeout=20 if _ai.get("warm") else 90)   # 1st call: LM Studio may still be loading the model (JIT)
        _ai["warm"] = True
        _last_ai["text"] = (out["choices"][0]["message"]["content"] or "").strip()
        return _last_ai["text"]
    except Exception as e:
        _ai["model"], _ai["warm"] = None, False       # re-check next time
        _ai["down_until"] = time.time() + 60
        _last_ai["text"] = f"(no answer: {type(e).__name__})"
        return ""

def ai_crop(im, box, tall=False, roomy=False):
    x1, y1, x2, y2 = box[:4]
    h = max(10, y2 - y1)
    if roomy:                                         # Dx: a second code is often on the line below, or off to the side
        return im.crop((max(0, x1 - h * 3), max(0, y1 - h * 1.2), min(im.width, x2 + h * 6), min(im.height, y2 + h * 2.6)))
    below = 1.3 if tall else .5                       # corrections often get written under a crossed-out number
    return im.crop((max(0, x1 - h * .6), max(0, y1 - h * .5), min(im.width, x2 + h * .8), min(im.height, y2 + h * below)))

def ai_dates(text):
    """Every date in the AI's answer."""
    out = []
    for m in re.finditer(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2,4})", text or ""):
        dt = ai_date(m.group(0))
        if dt:
            out.append(dt)
    return out

def ai_date(text):
    m = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2,4})", text or "")
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y = y + (2000 if y < 50 else 1900) if y < 100 else y
    try:
        return datetime(y, mo, d)
    except ValueError:
        return None

def vote(field, f, ai_v):
    """Combine the AI's (already rule-checked) answer with the built-in reader's.
    For Dx, ai_v is a list of codes and the comparison is as a set ("H52.13 + H52.4")."""
    if not ai_v:
        return
    top = list(((f.get("ctc") or {}).get("top")) or [])
    if field == "hcodes":
        cur, ai_set = sorted(f.get("v") or []), sorted(ai_v)
        f["ai"] = " + ".join(ai_v)
        margin = (f.get("ctc") or {}).get("margin") or 0
        if cur == ai_set:                             # both agree, AND the built-in read was clear
            ok = margin >= DX_SURE_WITH_AI            # (2 agreed-but-wrong Dx had margins 4.5 and 7)
            f["flag"] = "ok" if ok else "warn"
            f["via"] = "ai+ocr"
        else:
            f["v"], f["flag"], f["via"] = list(ai_v), "warn", "ai"
        combo = " + ".join(ai_v)
        top = ([combo] if len(ai_v) > 1 else []) + [t for t in top if t != combo]
        if len(ai_v) == 1:
            top = [ai_v[0]] + [t for t in top if t != ai_v[0]]
        f.setdefault("ctc", {})["top"] = top[:4]
        return
    cur = f.get("v")
    f["ai"] = ai_v
    if ai_v == cur:
        f["flag"] = "ok"                              # both readers agree
        f["via"] = "ai+ocr"
    else:
        f["v"] = ai_v
        f["flag"] = "warn"                            # they disagree: take the AI's, but have a look
        f["via"] = "ai"
        top = [ai_v] + [t for t in top if t != ai_v]
    f.setdefault("ctc", {})["top"] = top[:4]

def ai_check(path, data):
    if not ai_ready():
        for k in ("dos", "insured_dob", "hcodes"):
            if data.get(k):
                data[k]["ai_note"] = f"AI not used: {_ai['status']}"
        return
    im = Image.open(path)
    au = data.get("auth") or {}
    if au.get("v") and au.get("box"):                 # printed, but one in 34 was misread: have the AI read it too
        ans = re.sub(r"\D", "", ai_read(ai_crop(im, au["box"]),
            "This is a printed authorization number on a vision insurance form. Reply with only its digits.") or "")
        if not ans:
            au["ai_status"] = "no answer" if _last_ai["text"].startswith("(no answer") else "rejected"
        elif au["v"] in ans:                          # (extra digits from a neighbouring box still count)
            au["ai_status"], au["via"] = "agree", "ai+ocr"
        else:
            au["ai_status"], au["flag"], au["ai"] = "disagree", "warn", ans
            au["ctc"] = {"top": [au["v"], ans]}
    dos = data.get("dos") or {}
    z = data.get("zone") or {}
    if not dos.get("box") and dos.get("printed") and z.get("end"):
        # no handwritten date line found: let the AI look at the whole handwriting area
        y1, y2 = z["cut"], min(im.height, z["end"])
        reg = im.crop((0, max(0, int(y1) - 20), im.width, min(im.height, int(y2) + 40)))
        if reg.width > 1400:
            reg = reg.resize((1400, round(reg.height * 1400 / reg.width)))
        ans = ai_read(reg, "This is the handwritten part of a vision insurance form. Find every handwritten date "
            "(month/day/2-digit year). If a number is crossed out, use the rewritten one. "
            "Reply with only the dates as MM/DD/YYYY, separated by commas.")
        lo = datetime.strptime(dos["printed"], "%m/%d/%Y")
        hits = sorted({d for d in ai_dates(ans) if lo - timedelta(days=45) <= d <= lo + timedelta(days=60)})
        if len(hits) == 1:
            dos.update(v=hits[0].strftime("%m/%d/%Y"), flag="warn", via="ai", ai=hits[0].strftime("%m/%d/%Y"),
                       ai_status="region", box=[0, y1, im.width, y2])
            dos.pop("note", None)
            dos.setdefault("ctc", {})["top"] = [dos["v"], dos["printed"]] if dos["v"] != dos["printed"] else [dos["v"]]
        else:
            dos["ai_status"] = "no answer" if _last_ai["text"].startswith("(no answer") else "region: none fit"
            dos["ai_note"] = f"AI looked at the whole handwriting area and said {_last_ai['text']!r}"
        data["dos"] = dos
    if dos.get("box") and dos.get("ai_status") != "region":
        dt = ai_date(ai_read(ai_crop(im, dos["box"], tall=True),
            "This is a handwritten date of service on a vision insurance form, written as month/day/2-digit year "
            "(for example 10/26/26). If any number is crossed out or scribbled over, ignore it and use the number "
            "written next to it or underneath it instead. Reply with only the date as MM/DD/YYYY."))
        lo = datetime.strptime(dos["printed"], "%m/%d/%Y") if dos.get("printed") else None
        if dt and (lo is None or lo - timedelta(days=45) <= dt <= lo + timedelta(days=60)):
            vote("dos", dos, dt.strftime("%m/%d/%Y"))
            if lo and dt < lo:                        # before the printed date: unusual, so always have a look
                dos["flag"] = "warn"
            dos["ai_status"] = "agree" if dos.get("via") == "ai+ocr" else "disagree"
        else:
            dos["ai_note"] = f"AI said {_last_ai['text']!r}: " + ("not a date" if not dt else "outside the date window, ignored")
            dos["ai_status"] = "no answer" if _last_ai["text"].startswith("(no answer") else ("not a date" if not dt else "out of window")
    dob = data.get("insured_dob") or {}
    if dob.get("box") and dob.get("pm") and dob.get("py"):
        dt = ai_date(ai_read(ai_crop(im, dob["box"], tall=True),
            "This shows a birth date on a vision insurance form. Someone handwrote the full date as month/day/2-digit "
            f"year. The form also prints the month and year: {MONTH_NAMES[dob['pm'] - 1]} {dob['py']}. "
            "If any number is crossed out, ignore it and use the rewritten one. "
            "Read the handwritten date exactly as written. Reply with only the date as MM/DD/YYYY."))
        if dt:
            dt = dt.replace(year=dob["py"]) if dt.year % 100 == dob["py"] % 100 else dt
        if dt and dt.month == dob["pm"] and dt.year == dob["py"]:
            vote("insured_dob", dob, dt.strftime("%m/%d/%Y"))
            dob["ai_status"] = "agree" if dob.get("via") == "ai+ocr" else "disagree"
        else:
            dob["ai_status"] = "no answer" if _last_ai["text"].startswith("(no answer") else "rejected"
            dob["ai_note"] = f"AI said {_last_ai['text']!r}: " + ("not a date" if not dt else "not in VSP's month/year, ignored")
    hx = data.get("hcodes") or {}
    if hx.get("box"):
        ans = ai_read(ai_crop(im, hx["box"], roomy=True),
            "This shows the handwritten diagnosis code(s) on a vision insurance form. The doctor writes one or two "
            f"codes, each one of: {', '.join(config.H_CODES)}. Ignore anything crossed out. "
            "Reply with only the code or codes, separated by commas.")
        up = re.sub(r"\s", "", (ans or "").upper())
        found, rest = [], up
        for c in sorted(config.H_CODES, key=len, reverse=True):   # longest first so H52.223 isn't read as H52.2
            if c in rest:
                found.append(c)
                rest = rest.replace(c, "")
        found = [c for c in config.H_CODES if c in found]          # keep the config order (H52.13 first)
        if found:
            vote("hcodes", hx, found)
            hx["ai_status"] = "agree" if hx.get("via") == "ai+ocr" else "disagree"
        else:
            hx["ai_note"] = f"AI said {ans!r}: not one of the codes, ignored"
            hx["ai_status"] = "no answer" if _last_ai["text"].startswith("(no answer") else "rejected"

# ---------------- background OCR worker ----------------
STATE = {"ocr": "loading"}

import logging
class _QuietPolling(logging.Filter):                  # keep the log readable: skip the every-second status checks
    def filter(self, r):
        m = r.getMessage()
        return not any(p in m for p in ("/api/home", "/status/", "/api/next", "/api/crop/", "/favicon"))
logging.getLogger("werkzeug").addFilter(_QuietPolling())
jobs = queue.Queue()

def worker():
    try:
        get_ocr()                                     # load models up front so problems show right away
        print(f"  OCR ready. Reader: {READER}")
        STATE["ocr"] = "ready"
        ai_ready()
        print(f"  AI reader: {_ai['status']}")
    except Exception as e:
        print(f"  OCR FAILED TO LOAD: {_ocr_err}")
        STATE["ocr"] = f"failed: {_ocr_err}"
    while True:
        cid = jobs.get()
        cl = get_claim(cid)
        if not cl:
            continue
        if not cl["img"] or not os.path.exists(cl["img"]):
            delete_claim(cid)
            continue
        try:
            T, t = {}, time.time()
            def lap(k):
                nonlocal t
                T[k] = round(time.time() - t, 1); t = time.time()
            h = Image.open(cl["img"]).height
            boxes = ocr_image(cl["img"]); lap("read page")
            add_rereads(cl["img"], boxes, h); lap("re-read handwriting")
            data = parse(boxes, h); lap("parse")
            AI_CACHE.clear()                          # ask the AI now, in parallel with the best-fit step below;
            pre = threading.Thread(target=_ai_prefetch, args=(cl["img"], copy.deepcopy(data)), daemon=True)
            pre.start()                               # (same crops + questions, so ai_check later finds the answers ready)
            try:
                refine(cl["img"], data)
            except Exception as e:
                print(f"  (constrained reading skipped for scan {cid}: {type(e).__name__}: {e})")
            lap("best fits")
            pre.join(timeout=120)
            try:
                ai_check(cl["img"], data)
            except Exception as e:
                print(f"  (AI check skipped for scan {cid}: {type(e).__name__}: {e})")
            lap("AI")
            STATE["last"] = T
            print(f"  scan {cid}: {sum(T.values()):.1f}s (" + ", ".join(f"{k} {v}s" for k, v in T.items()) + ")")
            labels_seen = sum(1 for lab in ALL_LABELS if any(label_hit(b["t"], [lab]) for b in boxes))
            if not auth_of(data) and labels_seen < 3:     # no auth # and hardly any VSP labels: not a form
                if os.path.exists(cl["img"]):
                    os.remove(cl["img"])
                update(cid, status="notform", img="", data={})
                continue
            dup = duplicate_of(cid, auth_of(data))
            if dup:                                   # same auth # already queued or billed -> don't queue it
                if os.path.exists(cl["img"]):
                    os.remove(cl["img"])
                update(cid, status="duplicate", img="", data={"dup": dup})
            else:
                update(cid, status="ready", data=data)   # straight to the eClaim queue
        except Exception as e:
            update(cid, status="error", data={"error": f"{type(e).__name__}: {e}"})

def _ai_prefetch(path, data):
    try:
        ai_check(path, data)
    except Exception:
        pass

threading.Thread(target=worker, daemon=True).start()
with db() as c:   # finish scans that were mid-read when the app closed; claims already read (and any phone fixes) stay as they are
    for r in c.execute("SELECT id FROM claims WHERE status IN ('processing','error') ORDER BY id").fetchall():
        c.execute("UPDATE claims SET status='processing', data='{}' WHERE id=?", (r["id"],))
        jobs.put(r["id"])


# ---------------- self-update (Windows exe) ----------------
REPO = "thecoltonk/claim-scanner"
UPD = {"state": "", "latest": "", "url": "", "msg": ""}   # state: "" | available | current | downloading | installing | error

def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))

def check_update():
    """Ask GitHub for the newest release (just a version number; nothing about you or your claims is sent)."""
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                     headers={"User-Agent": "ClaimScanner", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            rel = json.loads(r.read())
        asset = next((a for a in rel.get("assets", []) if a["name"].endswith(".zip")), None)
        UPD["latest"] = rel.get("tag_name", "").lstrip("v")
        UPD["url"] = asset["browser_download_url"] if asset else ""
        if UPD["state"] in ("", "current", "available", "error"):
            UPD["state"] = "available" if asset and _vtuple(UPD["latest"]) > _vtuple(VERSION) else "current"
        if UPD["state"] == "available":
            print(f"  Update available: {UPD['latest']}")
    except Exception as e:
        print(f"  (couldn't check for updates: {e})")

def _updater_loop():
    time.sleep(5)
    while True:
        check_update()
        time.sleep(6 * 3600)

def install_update(zip_path=None):
    """Download the new zip, unpack it beside the app, and hand off to a tiny script that waits for this app
    to close, copies the new program files over the old ones (your config, queue, certs stay), and restarts it."""
    if not FROZEN or sys.platform != "win32":
        UPD.update(state="error", msg="Self-update only works in the Windows app. From source: download the new files.")
        return
    try:
        stage = os.path.join(BASE, "_update")
        shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage)
        if not zip_path:
            UPD.update(state="downloading", msg="Downloading… 0%")
            zip_path = os.path.join(stage, "new.zip")
            req = urllib.request.Request(UPD["url"], headers={"User-Agent": "ClaimScanner"})
            with urllib.request.urlopen(req, timeout=60) as r, open(zip_path, "wb") as out:
                total, got = int(r.headers.get("Content-Length") or 0), 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk); got += len(chunk)
                    if total:
                        UPD["msg"] = f"Downloading… {got * 100 // total}%"
        UPD.update(state="installing", msg="Unpacking…")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(os.path.join(stage, "new"))
        src = os.path.join(stage, "new", "ClaimScanner")
        if not os.path.exists(os.path.join(src, "ClaimScanner.exe")):
            raise RuntimeError("the download doesn't look like Claim Scanner")
        exe = os.path.join(BASE, "ClaimScanner.exe")
        script = os.path.join(stage, "update.cmd")
        with open(script, "w", encoding="utf-8") as f:
            f.write(f"""@echo off
:wait
tasklist /FI "PID eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul && (timeout /t 1 /nobreak >nul & goto wait)
robocopy "{src}\\_internal" "{BASE}\\_internal" /MIR /R:5 /W:1 /NFL /NDL /NJH /NJS /NP >nul
robocopy "{src}" "{BASE}" ClaimScanner.exe eclaim-prefill.user.js README.md LICENSE /R:5 /W:1 /NFL /NDL /NJH /NJS /NP >nul
start "" "{exe}" --no-browser --updated
""")
        subprocess.Popen(["cmd.exe", "/c", script], creationflags=0x08000000 | 0x00000200,   # no window, own group
                         close_fds=True, cwd=stage)
        UPD.update(msg="Restarting…")
        print("  Installing update and restarting")
        threading.Timer(0.5, lambda: os._exit(0)).start()
    except Exception as e:
        UPD.update(state="error", msg=f"Update failed: {e}")
        print(f"  Update failed: {e}")

if FROZEN and "--no-update-check" not in sys.argv:
    threading.Thread(target=_updater_loop, daemon=True).start()

# ---------------- routes ----------------
def local_only(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        if request.remote_addr not in ("127.0.0.1", "::1"):
            abort(403)
        return fn(*a, **k)
    return w

HTTPS_PORT = getattr(config, "HTTPS_PORT", 5443)
CERTS = os.path.join(BASE, "certs")

SETUP_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Scanner Setup</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px;line-height:1.5}
h1{font-size:22px}.s{background:#1e293b;border-radius:14px;padding:16px;margin:14px 0}
a.b{display:block;text-align:center;background:#2563eb;color:#fff;text-decoration:none;padding:14px;border-radius:12px;font-weight:600;margin-top:10px}
ol{padding-left:20px;margin:8px 0}.m{color:#94a3b8;font-size:14px}</style></head><body>
<h1>Scanner setup (one time)</h1>
<div class="s"><b>1. Install the certificate</b>
<a class="b" href="/ca.crt">Download certificate</a>
<ol><li>Tap <b>Allow</b>, then <b>Close</b></li>
<li>Settings app &rarr; <b>Profile Downloaded</b> (near the top) &rarr; <b>Install</b></li>
<li>Settings &rarr; General &rarr; About &rarr; <b>Certificate Trust Settings</b> &rarr; turn on <b>Claim Scanner Local CA</b></li></ol></div>
<div class="s"><b>2. Open the scanner</b>
<a class="b" href="https://{host}:{port}/?k={k}">Open scanner</a>
<p class="m">Allow the camera, then Share &rarr; <b>Add to Home Screen</b>. Use that icon from now on.</p></div>
</body></html>"""

@app.get("/setup")
def setup():
    if request.args.get("k") != TOKEN:
        abort(403)
    return SETUP_HTML.replace("{host}", request.host.split(":")[0]).replace("{port}", str(HTTPS_PORT)).replace("{k}", TOKEN)

@app.get("/ca.crt")
def ca_crt():
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    ca = x509.load_pem_x509_certificate(open(os.path.join(CERTS, "ca.crt"), "rb").read())
    return Response(ca.public_bytes(serialization.Encoding.DER), mimetype="application/x-x509-ca-cert",
                    headers={"Content-Disposition": "attachment; filename=ClaimScannerCA.crt"})

@app.get("/")
def phone():
    k = request.args.get("k") or request.cookies.get("k")
    if k != TOKEN:
        abort(403)
    if not request.is_secure:                        # camera needs HTTPS -> send them to setup
        return redirect(f"/setup?k={k}")
    resp = make_response(open(os.path.join(RES, "phone.html"), encoding="utf-8").read())
    resp.set_cookie("k", k, max_age=3600 * 24 * 365, samesite="Strict")
    return resp

def images_from(data):
    """Photo or multi-page PDF (iPhone Scan Document) -> one image per page."""
    if data[:4] == b"%PDF":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
        for i in range(len(pdf)):
            yield pdf[i].render(scale=200 / 72).to_pil()
    else:
        yield ImageOps.exif_transpose(Image.open(io.BytesIO(data)))

@app.post("/upload")
def upload():
    if (request.args.get("k") or request.cookies.get("k")) != TOKEN:
        abort(403)
    files = [f for key in request.files for f in request.files.getlist(key)]   # any field name
    if not files:
        abort(400)
    rep = request.args.get("replaces", "")
    if rep.isdigit():
        delete_claim(int(rep))                       # retake: throw away the bad scan first
    ids = []
    for f in files:
        for img in images_from(f.read()):
            img = img.convert("RGB")
            img.thumbnail((4000, 4000))                 # keep detail for the handwriting
            with db() as c:
                cid = c.execute("INSERT INTO claims (created, status, img, data) VALUES (?,?,?,?)",
                                (time.time(), "processing", "", "{}")).lastrowid
            path = os.path.join(SCANS, f"{cid}.jpg")
            img.save(path, quality=88)
            update(cid, img=path)
            jobs.put(cid)
            ids.append(cid)
    return jsonify(ok=True, ids=ids)

CHECK_FIELDS = [("auth", "Auth"), ("gender", "Name"), ("hcodes", "Dx"), ("dos", "Date"), ("copay", "Copay")]

def phone_ok():
    return (request.args.get("k") or request.cookies.get("k")) == TOKEN

@app.get("/status/<int:cid>")
def status(cid):
    """For the phone: did each field read OK? Flags only, never the values."""
    if not phone_ok():
        abort(403)
    cl = get_claim(cid)
    if not cl:
        return jsonify(status="gone")
    if cl["status"] == "processing":
        return jsonify(status="processing")
    if cl["status"] == "duplicate":
        where = cl["data"].get("dup", "queue")
        delete_claim(cid)
        return jsonify(status="duplicate", where=where)
    if cl["status"] == "notform":
        delete_claim(cid)
        return jsonify(status="notform")
    d = cl["data"]
    if d.get("error"):
        return jsonify(status="error")
    need_paper = {"auth", "hcodes", "dos"}            # everything else is a quick fix on the review page
    fields = []
    for k, lbl in CHECK_FIELDS + ([("insured_dob", "Insured DOB")] if (d.get("dependent") or {}).get("v") else []):
        f = (d.get(k) or {}).get("flag", "missing")
        fields.append({"label": lbl, "flag": "warn" if (f == "missing" and k not in need_paper) else f})
    return jsonify(status="done", dependent=bool((d.get("dependent") or {}).get("v")), fields=fields)

CHECKABLE = [("auth", "Auth #"), ("dependent", "Type"), ("dos", "Date of service"), ("insured_dob", "Holder DOB"),
             ("hcodes", "Dx"), ("copay", "Copay"), ("gender", "Patient sex"), ("insured_gender", "Holder sex")]

def _field_view(d, k):
    """What the phone shows / lets you edit for one field."""
    f = d.get(k) or {}
    dep = bool((d.get("dependent") or {}).get("v"))
    v = f.get("v")
    view = {"field": k, "flag": f.get("flag", "missing"), "raw": v}
    if k == "auth":
        view.update(kind="digits", value=v or "", choices=[t for t in ((f.get("ctc") or {}).get("top") or []) if t])
    elif k == "dependent":
        view.update(kind="pick", value="Dependent" if dep else "Self", raw="Dependent" if dep else "Self",
                    choices=["Self", "Dependent"], flag="ok")
    elif k in ("dos", "insured_dob"):
        top = [t for t in ((f.get("ctc") or {}).get("top") or []) if t]
        if v and v not in top:
            top = [v] + top
        hint = (f"printed {f['printed']}" if k == "dos" and f.get("printed") else
                f"VSP prints {f['printed']}" if k == "insured_dob" and f.get("printed") else "")
        view.update(kind="date", value=v or "", choices=top[:4], hint=hint)
    elif k == "hcodes":
        view.update(kind="codes", value=", ".join(v or []), raw=list(v or []), choices=list(config.H_CODES))
    elif k == "copay":
        view.update(kind="money", value=f"${v}" if v not in (None, "") else "")
    elif k in ("gender", "insured_gender"):
        view.update(kind="pick", value=v or "", choices=["M", "F"])
    return view

@app.get("/details/<int:cid>")
def details(cid):
    """Confirm mode: the values that were read, so you can compare with the paper (and fix them) on the phone."""
    if not phone_ok():
        abort(403)
    cl = get_claim(cid)
    if not cl or cl["status"] != "ready":
        return jsonify(fields=[])
    d = cl["data"]
    dep = bool((d.get("dependent") or {}).get("v"))
    out = []
    for k, lbl in CHECKABLE:
        if k in ("insured_dob", "insured_gender") and not dep:
            continue
        out.append(dict(_field_view(d, k), label=lbl))
    return jsonify(fields=out)

def _clean_fix(k, v):
    """Validate a value typed/picked on the phone. Returns the stored form, or None if it doesn't make sense."""
    v = (v if isinstance(v, list) else str(v or "")).copy() if isinstance(v, list) else str(v or "").strip()
    if k == "auth":
        d = re.sub(r"\D", "", v)
        return d if 6 <= len(d) <= 14 else None
    if k in ("dos", "insured_dob"):
        d = re.sub(r"\D", "", v)
        m = re.match(r"^(\d{1,2})\D+(\d{1,2})\D+(\d{2}|\d{4})$", v)
        if m:
            mo, dd, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        elif len(d) in (6, 8):                        # 042683 / 04261983
            mo, dd, y = int(d[:2]), int(d[2:4]), int(d[4:])
        else:
            return None
        if y < 100:
            y += 2000 if (k == "dos" or y <= datetime.now().year % 100) else 1900
        try:
            dt = datetime(y, mo, dd)
        except ValueError:
            return None
        if dt > datetime.now() + timedelta(days=1) or dt.year < 1900:
            return None
        return dt.strftime("%m/%d/%Y")
    if k == "hcodes":
        codes = [c for c in config.H_CODES if c in (v if isinstance(v, list) else re.split(r"[,\s+]+", v))]
        return codes or None
    if k == "copay":
        m = re.match(r"^\$?\s*(\d{1,4})(?:\.(\d{1,2}))?$", v)
        return f"{int(m.group(1))}.{(m.group(2) or '0').ljust(2, '0')}" if m else None
    if k in ("gender", "insured_gender"):
        return v.upper()[:1] if v.upper()[:1] in ("M", "F") else None
    if k == "dependent":
        return v == "Dependent"
    return None

@app.post("/check/<int:cid>")
def check(cid):
    """Confirm mode: you compared with the paper. Saves your fixes, logs right/wrong per field (no values),
    and marks the claim confirmed, so eClaim gets the corrected values with nothing left to check."""
    if not phone_ok():
        abort(403)
    cl = get_claim(cid)
    if not cl:
        return jsonify(ok=False)
    body = request.get_json(silent=True) or {}
    wrong, fixes = set(body.get("wrong", [])), body.get("fix") or {}
    d = cl["data"]
    bad = [k for k in fixes if _clean_fix(k, fixes[k]) is None]
    if bad:                                           # nothing saved until every fix makes sense
        return jsonify(ok=False, bad=bad, confirmed=False)
    first = not d.get("checked")                      # log right/wrong once per claim
    dep0 = bool((d.get("dependent") or {}).get("v"))
    rows = []
    for k, _ in CHECKABLE:
        if k in ("insured_dob", "insured_gender") and not dep0:
            continue
        f = d.get(k) or {}
        flag0, via0 = f.get("flag"), f.get("via")       # what the app said before you touched it
        changed = False
        if k in fixes:
            val = _clean_fix(k, fixes[k])
            old = f.get("v")
            changed = (val != old) if k != "hcodes" else (sorted(val) != sorted(old or []))
            f["v"], f["flag"], f["via"] = val, "ok", "you"
            f.pop("note", None)
            d[k] = f
        is_wrong = (k in wrong and k not in fixes) or changed
        rows.append({"t": round(time.time()), "field": k, "flag": flag0 if k != "dependent" else "ok",
                     "via": via0 or "fuzzy", "margin": (f.get("ctc") or {}).get("margin"),
                     "ai": f.get("ai_status", "none"), "right": not is_wrong, "src": "phone"})
        if k in wrong and k not in fixes and k != "dependent":
            f["flag"] = "missing"
            f["note"] = "marked wrong on the phone, check the paper"
            d[k] = f
    if bool((d.get("dependent") or {}).get("v")) and not dep0:       # switched to Dependent: holder info needed
        for k in ("insured_dob", "insured_gender"):
                d[k] = {"v": "", "flag": "missing", "note": "switched to Dependent on the phone: type it in eClaim"}
    if first:
        with open(ACC_LOG, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    d["checked"] = True                               # don't count it again at Billed
    d["confirmed"] = not [k for k in wrong if k not in fixes]
    update(cid, data=d)
    return jsonify(ok=True, bad=[], confirmed=d["confirmed"])

@app.post("/retake/<int:cid>")
def retake(cid):
    if not phone_ok():
        abort(403)
    delete_claim(cid)
    return jsonify(ok=True)

@app.get("/review")
@local_only
def review():
    return send_file(os.path.join(RES, "review.html"))

@app.get("/scan/<int:cid>")
@local_only
def scan_page(cid):
    cl = get_claim(cid)
    if not cl:
        abort(404)
    import html as _h
    lines = "".join(f"<li>{_h.escape(t)}</li>" for t in cl["data"].get("raw", []))
    fits = "".join(f"<li>{n}: {_h.escape(' / '.join(map(str, ((cl['data'].get(k) or {}).get('ctc') or {}).get('top', []))) or '-')}"
                   f" (confidence {((cl['data'].get(k) or {}).get('ctc') or {}).get('margin', '-')})"
                   f" | AI read: {_h.escape(str((cl['data'].get(k) or {}).get('ai') or (cl['data'].get(k) or {}).get('ai_note') or '-'))}"
                   f" | went with: {_h.escape(str((cl['data'].get(k) or {}).get('v') or '-'))} {'✓' if (cl['data'].get(k) or {}).get('flag') == 'ok' else '⚠'}</li>"
                   for k, n in (("dos", "Date of service"), ("insured_dob", "Holder DOB"), ("hcodes", "Dx")))
    return f"""<!doctype html><meta charset="utf-8"><title>Scan {cid}</title>
<style>body{{margin:0;display:flex;gap:16px;font:14px system-ui;background:#0f172a;color:#e2e8f0}}
img{{height:100vh;max-width:60vw;object-fit:contain;background:#000}} div{{padding:16px;overflow:auto;height:100vh;box-sizing:border-box}}
li{{margin:3px 0;font-family:ui-monospace,monospace}}</style>
<img src="/img/{cid}"><div><b>Best fits for the handwriting</b><ul>{fits}</ul><b>What the OCR read, top to bottom</b><ol>{lines}</ol></div>"""

@app.get("/img/<int:cid>")
@local_only
def img(cid):
    cl = get_claim(cid)
    if not cl or not cl["img"]:
        abort(404)
    return send_file(cl["img"])

@app.get("/api/list")
@local_only
def api_list():
    with db() as c:
        rows = [row_dict(r) for r in c.execute("SELECT * FROM claims ORDER BY id")]
    for r in rows:
        r.pop("img")
    return jsonify(codes=config.H_CODES, claims=rows)

@app.post("/api/claim/<int:cid>")
@local_only
def api_claim(cid):
    body = request.get_json(force=True)
    action = body.get("action")
    if action == "delete":
        delete_claim(cid)
        return jsonify(ok=True)
    cl = get_claim(cid)
    if not cl:
        abort(404)
    data = cl["data"]
    for k, v in (body.get("data") or {}).items():    # your edits = confirmed
        data.setdefault(k, {})["v"] = v
        data[k]["flag"] = "ok"
    status = {"approve": "ready", "unapprove": "review"}.get(action, cl["status"])
    update(cid, data=data, status=status)
    return jsonify(ok=True)

@app.get("/api/next")
@local_only
def api_next():
    with db() as c:
        r = c.execute("SELECT * FROM claims WHERE status='ready' ORDER BY created, id LIMIT 1").fetchone()
        left = c.execute("SELECT COUNT(*) FROM claims WHERE status IN ('ready','processing')").fetchone()[0]
    if not r:
        return jsonify(claim=None, left=left, app_version=VERSION, script_version=SCRIPT_VERSION)
    d = row_dict(r)["data"]
    g = lambda k, dflt="": (d.get(k) or {}).get("v", dflt)
    labels = {"auth": "Auth #", "dependent": "Self or Dependent", "dos": "Date of service", "gender": "Sex", "hcodes": "Dx", "copay": "Copay"}
    if g("dependent", False):
        labels["insured_dob"] = "Insured DOB"
        labels["insured_gender"] = "Insured sex"
    check = []                                        # what to eyeball / type yourself in eClaim
    for k, lbl in labels.items():
        f = (d.get(k) or {}).get("flag", "missing")
        crop = bool((d.get(k) or {}).get("box"))      # panel shows that bit of the photo
        if f == "missing":
            note = (d.get(k) or {}).get("note")
            check.append({"level": "missing", "field": k, "crop": crop, "text": f"{lbl}: " + (note or "couldn't read, type it in"),
                          "choices": ((d.get(k) or {}).get("ctc") or {}).get("top", [])})
        elif f == "warn":
            extra = {"dos": f" (printed {(d.get('dos') or {}).get('printed', '?')})",
                     "insured_dob": f" (VSP prints {(d.get('insured_dob') or {}).get('printed', '?')})",
                     "gender": f" (guessed from \"{(d.get('gender') or {}).get('name', '')}\")",
                     "insured_gender": f" (guessed from \"{(d.get('insured_gender') or {}).get('name', '')}\")",
                     }.get(k, "")
            check.append({"level": "warn", "field": k, "crop": crop, "text": f"{lbl}: double-check{extra}",
                          "choices": ((d.get(k) or {}).get("ctc") or {}).get("top", [])})
    if d.get("confirmed"):                            # you checked everything against the paper on the phone
        check = [x for x in check if x["level"] == "missing"]
    return jsonify(left=left, app_version=VERSION, script_version=SCRIPT_VERSION, claim={"id": r["id"], "confirmed": bool(d.get("confirmed")), "check": check, "auth": g("auth"), "dos": g("dos"), "gender": g("gender"),
                          "hcodes": g("hcodes", []), "copay": g("copay"), "dependent": bool(g("dependent", False)),
                          "insured_dob": g("insured_dob"), "insured_gender": g("insured_gender"), "cpt": config.CPT_LINES,
                          "npi": getattr(config, "RENDERING_NPI", ""), "tax_id_type": getattr(config, "TAX_ID_TYPE", "EIN")})

@app.get("/api/crop/<int:cid>/<field>")
@local_only
def api_crop(cid, field):
    """Just the handwriting for one field (e.g. the DOS line), so the panel can show it next to the guess."""
    cl = get_claim(cid)
    box = ((cl or {}).get("data", {}).get(field) or {}).get("box") if cl else None
    if not box or not cl["img"] or not os.path.exists(cl["img"]):
        abort(404)
    im = Image.open(cl["img"])
    x1, y1, x2, y2 = box[:4]
    h = max(10, y2 - y1)
    if field == "hcodes":                             # the same roomy view the AI gets (2nd codes live below)
        crop = ai_crop(im, box, roomy=True)
    else:
        crop = im.crop((max(0, x1 - h * .4), max(0, y1 - h * .35), min(im.width, x2 + h * .4), min(im.height, y2 + h * .35)))
    crop.thumbnail((640, 110 if crop.height < 2.5 * h and field != "hcodes" else 260))
    buf = io.BytesIO()
    crop.convert("RGB").save(buf, "JPEG", quality=85)
    return Response(buf.getvalue(), mimetype="image/jpeg")

@app.post("/api/skip/<int:cid>")
@local_only
def api_skip(cid):
    update(cid, created=time.time())                 # back of the line
    return jsonify(ok=True)

@app.post("/api/delete/<int:cid>")
@local_only
def api_delete(cid):
    delete_claim(cid)                                # junk scan: gone, not recorded as billed
    return jsonify(ok=True)

@app.post("/api/done/<int:cid>")
@local_only
def api_done(cid):
    cl = get_claim(cid)
    a = auth_of(cl["data"]) if cl else ""
    try:
        log_accuracy(cl, request.get_json(silent=True) or {})
    except Exception:
        pass
    if a:
        with db() as c:
            c.execute("INSERT OR REPLACE INTO billed (auth, at) VALUES (?, ?)", (a, time.time()))
    delete_claim(cid)                                # billed -> scan and data deleted
    return jsonify(ok=True)

ACC_LOG = os.path.join(BASE, "accuracy.jsonl")

def _same(field, guess, actual):
    if field == "hcodes":
        g = (guess or [""])[0] if isinstance(guess, list) else (guess or "")
        return re.sub(r"[^0-9A-Z]", "", g.upper()) == re.sub(r"[^0-9A-Z]", "", (actual or "").upper())
    try:
        f = lambda x: datetime.strptime(re.sub(r"\s", "", x), "%m/%d/%Y")
        return f(guess) == f(actual)
    except Exception:
        return (guess or "").strip() == (actual or "").strip()

def log_accuracy(cl, actual):
    """On Billed: was each handwritten guess right? Stores right/wrong + confidence only (no dates, no names)."""
    if not cl or cl["data"].get("checked"):
        return                                        # already checked on the phone
    d = cl["data"]
    rows = []
    for field in ("dos", "insured_dob", "hcodes"):
        if not actual.get(field):
            continue
        f = d.get(field) or {}
        rows.append({"t": round(time.time()), "field": field, "flag": f.get("flag"), "via": f.get("via", "fuzzy"),
                     "ai": f.get("ai_status", "none"),
                     "margin": (f.get("ctc") or {}).get("margin"), "right": _same(field, f.get("v"), actual[field])})
    if rows:
        with open(ACC_LOG, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

@app.get("/stats")
@local_only
def stats():
    rows = []
    if os.path.exists(ACC_LOG):
        rows = [json.loads(l) for l in open(ACC_LOG) if l.strip()]
    names = {"dos": "Date of service", "insured_dob": "Holder DOB", "hcodes": "Dx", "auth": "Auth #", "copay": "Copay",
             "gender": "Patient sex", "insured_gender": "Holder sex", "dependent": "Self/Dependent"}
    out = ["<!doctype html><meta charset=utf-8><title>Accuracy</title><style>body{font:15px system-ui;margin:24px}"
           "td,th{padding:6px 12px;text-align:left}</style><h2>How the reads did (from phone checks and Billed ✓)</h2><table>"
           "<tr><th>Field</th><th>Claims</th><th>Right</th><th>Marked ✓</th><th>✓ that were right</th><th>⚠ that were right</th></tr>"]
    pct = lambda xs: f"{100 * sum(r['right'] for r in xs) / len(xs):.0f}% ({sum(r['right'] for r in xs)}/{len(xs)})" if xs else "–"
    for k, name in names.items():
        xs = [r for r in rows if r["field"] == k]
        if not xs and k not in ("dos", "insured_dob", "hcodes"):
            continue
        ok = [r for r in xs if r["flag"] == "ok"]
        warn = [r for r in xs if r["flag"] != "ok"]
        out.append(f"<tr><td>{name}</td><td>{len(xs)}</td><td>{pct(xs)}</td><td>{len(ok)}</td><td>{pct(ok)}</td><td>{pct(warn)}</td></tr>")
    out.append("</table><h3>By who answered</h3><table><tr><th>Field</th><th>Both readers agreed</th><th>AI only (disagreed)</th><th>Built-in only</th></tr>")
    for k, name in list(names.items())[:3]:
        xs = [r for r in rows if r["field"] == k]
        out.append(f"<tr><td>{name}</td><td>{pct([r for r in xs if r.get('via') == 'ai+ocr'])}</td>"
                   f"<td>{pct([r for r in xs if r.get('via') == 'ai'])}</td>"
                   f"<td>{pct([r for r in xs if r.get('via') not in ('ai', 'ai+ocr')])}</td></tr>")
    out.append("</table><h3>What the AI did</h3><table><tr><th>Field</th><th>Agreed</th><th>Disagreed</th><th>Answer thrown out</th><th>No answer / not used</th><th>Found it in the whole area</th></tr>")
    for k, name in [(k, names[k]) for k in ("dos", "insured_dob", "hcodes", "auth")]:
        xs = [r for r in rows if r["field"] == k and r.get("ai")]
        cnt = lambda *st: sum(1 for r in xs if r.get("ai") in st)
        out.append(f"<tr><td>{name}</td><td>{cnt('agree')}</td><td>{cnt('disagree')}</td>"
                   f"<td>{cnt('out of window', 'not a date', 'rejected', 'region: none fit')}</td><td>{cnt('no answer', 'none')}</td>"
                   f"<td>{cnt('region')}</td></tr>")
    out.append("</table><p>Goal: \"✓ that were right\" stays at 100%. Paste these tables to Claude to tune it.</p>")
    wrong_ok = [r for r in rows if r["flag"] == "ok" and not r["right"]]
    if wrong_ok:
        out.append("<p><b>✓ but wrong:</b> " + ", ".join(f"{names[r['field']]} (confidence {r['margin']})" for r in wrong_ok) + "</p>")
    return "".join(out)

HOME_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claim Scanner</title><link rel="icon" href="/favicon.ico"><style>
:root{--blue:#006ab6;--ink:#1f2933;--mute:#5f6b7a;--line:#d5dce4;--bg:#f3f5f8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 "Segoe UI",Roboto,Arial,sans-serif}
header{background:var(--blue);color:#fff;padding:14px 24px;display:flex;align-items:center;gap:12px}
header h1{font-size:18px;margin:0}header .v{opacity:.8;font-size:13px}
header button{margin-left:auto;background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.5);border-radius:4px;padding:6px 14px;font:inherit;cursor:pointer}
main{max-width:860px;margin:20px auto;padding:0 16px;display:grid;grid-template-columns:1fr 300px;gap:16px}
@media(max-width:720px){main{grid-template-columns:1fr}}
.card{background:#fff;border:1px solid var(--line);border-radius:6px;padding:16px 18px}
.card h2{font-size:15px;margin:0 0 10px}
.st{display:grid;grid-template-columns:120px 1fr;gap:6px 10px}.st dt{color:var(--mute)}.st dd{margin:0;font-weight:600}
.ok{color:#1e7b34}.warn{color:#9a5b00}.bad{color:#b3261e}
.qr{display:flex;justify-content:center;margin:6px 0 10px}.qr svg{width:220px;height:220px}
.m{color:var(--mute);font-size:13px;word-break:break-all}
a{color:var(--blue)}ul{margin:0;padding-left:18px}li{margin:4px 0}
.big{font-size:28px;font-weight:700}
#upd{display:none;background:#e8f1fa;border:1px solid #9cc3e6;border-radius:6px;padding:12px 16px;margin-bottom:16px;align-items:center;gap:12px}
#upd.on{display:flex}#upd b{flex:1}
.btn{background:var(--blue);color:#fff;border:0;border-radius:4px;padding:8px 16px;font:inherit;font-weight:600;cursor:pointer}
.btn:disabled{background:#9fb3c8;cursor:default}
</style></head><body>
<header><img src="/favicon.ico" width="28" height="28" alt=""><h1>Claim Scanner</h1><span class="v">{version}</span><button id="quit">Quit</button></header>
<main>
 <div>
  <div id="upd"><b id="updt"></b><button class="btn" id="updb">Update now</button></div>
  <div class="card"><h2>Status</h2><dl class="st" id="st"><dt>Loading…</dt><dd></dd></dl></div>
  <div class="card" style="margin-top:16px"><h2>Queue</h2><div class="big" id="q">–</div><div class="m">claims waiting for eClaim</div></div>
  <div class="card" style="margin-top:16px"><h2>Links</h2><ul>
   <li><a href="/stats" target="_blank">Accuracy stats</a></li>
   <li><a href="/review" target="_blank">Review queue</a></li>
   <li><a href="/eclaim-prefill.user.js">Install / update the eClaim panel</a> <span class="m">(open in Firefox with Tampermonkey)</span></li>
   <li>Settings: <span class="m">{folder}config.py</span> (edit, then Quit and reopen)</li>
   <li>Log: <span class="m">{folder}claim-scanner.log</span></li></ul></div>
 </div>
 <div class="card"><h2>Phone</h2>
  <div class="qr">{qr}</div>
  <div class="m" style="text-align:center">Scan with the iPhone camera the first time (setup).<br>After that use the home-screen icon.</div>
  <p class="m">Setup: {setup}<br>Scanner: {scanner}</p></div>
</main>
<script>
const $ = id => document.getElementById(id), esc = t => String(t).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function tick() {
  try {
    const j = await (await fetch('/api/home')).json();
    $('st').innerHTML = j.rows.map(([k, v, c]) => `<dt>${esc(k)}</dt><dd class="${c}">${esc(v)}</dd>`).join('');
    $('q').textContent = j.queue;
    const u = j.update || {};
    if (window.ver && window.ver !== j.version) location.reload();      // came back after an update
    window.ver = j.version;
    $('upd').className = ['available', 'downloading', 'installing', 'error'].includes(u.state) ? 'on' : '';
    $('updt').textContent = u.state === 'available' ? `Update available: ${u.latest}` : u.msg || '';
    $('updb').style.display = u.state === 'available' || u.state === 'error' ? '' : 'none';
    $('updb').textContent = u.state === 'error' ? 'Try again' : 'Update now';
  } catch (e) {
    $('st').innerHTML = window.updating ? '<dt>App</dt><dd class="warn">restarting with the update…</dd>' : '<dt>App</dt><dd class="bad">not running</dd>';
  }
}
$('updb').onclick = async () => {
  $('updb').disabled = true; window.updating = true;
  await fetch('/api/update', {method: 'POST'}).catch(() => {});
  setTimeout(() => $('updb').disabled = false, 3000); tick();
};
$('quit').onclick = async () => { await fetch('/api/quit', {method: 'POST'}).catch(() => {}); document.body.innerHTML = '<p style="padding:24px">Claim Scanner is closed. You can close this tab.</p>'; };
tick(); setInterval(tick, 2000);
</script></body></html>"""

def _qr_svg(url):
    try:
        import segno
        return segno.make(url, error="m").svg_inline(scale=6, border=2)
    except Exception:
        return ""

@app.get("/home")
@local_only
def home():
    ip = lan_ip()
    setup_url = f"http://{ip}:{config.PORT}/setup?k={TOKEN}"
    scan_url = f"https://{ip}:{HTTPS_PORT}/?k={TOKEN}"
    e = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;")
    return (HOME_HTML.replace("{version}", VERSION).replace("{qr}", _qr_svg(setup_url))
            .replace("{setup}", e(setup_url)).replace("{scanner}", e(scan_url)).replace("{folder}", e(BASE + os.sep)))

@app.get("/api/home")
@local_only
def api_home():
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM claims WHERE status IN ('ready','processing')").fetchone()[0]
    ocr = STATE["ocr"]
    ai = _ai.get("status") or "checking…"
    rows = [["Reader", f"ready ({READER})" if ocr == "ready" else ocr, "ok" if ocr == "ready" else "bad" if ocr.startswith("failed") else "warn"],
            ["AI reader", ai, "ok" if _ai.get("model") else "warn"],
            ["Name list", f"{len(NAMES):,} names" if NAMES else "not loaded (sex can't be guessed)", "ok" if NAMES else "warn"],
            ["This PC", lan_ip(), ""]]
    if STATE.get("last"):
        T = STATE["last"]
        rows.append(["Last scan", f"{sum(T.values()):.0f}s: " + ", ".join(f"{k} {v:g}s" for k, v in T.items()), ""])
    return jsonify(rows=rows, queue=n, version=VERSION, update=UPD)

@app.post("/api/update")
@local_only
def api_update():
    if UPD["state"] in ("downloading", "installing"):
        return jsonify(ok=True)
    test_zip = os.environ.get("CLAIMSCANNER_UPDATE_ZIP")      # (build check: install a local zip)
    if not test_zip and UPD["state"] != "available":
        check_update()
        if UPD["state"] != "available":
            return jsonify(ok=False, msg="Already up to date")
    threading.Thread(target=install_update, args=(test_zip,), daemon=True).start()
    return jsonify(ok=True)

def _script_version():
    try:
        m = re.search(r"@version\s+(\S+)", open(os.path.join(RES, "eclaim-prefill.user.js"), encoding="utf-8").read(4000))
        return m.group(1) if m else ""
    except OSError:
        return ""
SCRIPT_VERSION = _script_version()

@app.get("/eclaim-prefill.user.js")
def userscript():
    """The eClaim panel that matches this version of the app. Tampermonkey checks here for updates."""
    return send_file(os.path.join(RES, "eclaim-prefill.user.js"), mimetype="text/javascript", max_age=0)

@app.get("/favicon.ico")
def favicon():
    return send_file(os.path.join(RES, "icon.png"), mimetype="image/png")

@app.get("/api/ping")
def api_ping():
    return "claim-scanner"

@app.post("/api/quit")
@local_only
def api_quit():
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return jsonify(ok=True)

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))             # no packet is actually sent
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def ensure_certs(ip):
    """Local CA (installed once on the phone) + a server cert for this laptop's IP. Rebuilt if the IP changes."""
    import datetime as dt, ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    os.makedirs(CERTS, exist_ok=True)
    p = lambda n: os.path.join(CERTS, n)
    now = dt.datetime.now(dt.timezone.utc)
    pem_key = lambda k: k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
    if not (os.path.exists(p("ca.key")) and os.path.exists(p("ca.crt"))):
        ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Claim Scanner Local CA")])
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
              .serial_number(x509.random_serial_number())
              .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                           content_commitment=False, key_encipherment=False, data_encipherment=False,
                                           key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
              .sign(ca_key, hashes.SHA256()))
        open(p("ca.key"), "wb").write(pem_key(ca_key))
        open(p("ca.crt"), "wb").write(ca.public_bytes(serialization.Encoding.PEM))
        for n in ("server.crt", "server.key"):
            if os.path.exists(p(n)):
                os.remove(p(n))
    ca_key = serialization.load_pem_private_key(open(p("ca.key"), "rb").read(), None)
    ca = x509.load_pem_x509_certificate(open(p("ca.crt"), "rb").read())
    fresh = False
    if os.path.exists(p("server.crt")):
        cur = x509.load_pem_x509_certificate(open(p("server.crt"), "rb").read())
        sans = cur.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        fresh = (ipaddress.ip_address(ip) in sans.get_values_for_type(x509.IPAddress)
                 and cur.not_valid_after_utc > now + dt.timedelta(days=30))
    if not fresh:
        key = ec.generate_private_key(ec.SECP256R1())
        crt = (x509.CertificateBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ip)]))
               .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=397))
               .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip)),
                              x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]), critical=False)
               .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
               .sign(ca_key, hashes.SHA256()))
        open(p("server.key"), "wb").write(pem_key(key))
        open(p("server.crt"), "wb").write(crt.public_bytes(serialization.Encoding.PEM))
    return p("server.crt"), p("server.key")

def start_tray(home_url):
    """Icon by the clock (Windows): Open / Stats / Folder / Quit. It's the app's only "window"."""
    if sys.platform != "win32" or "--no-tray" in sys.argv:
        return
    def run():
        try:
            import webbrowser
            import pystray
            img = Image.open(os.path.join(RES, "icon.png"))
            def queued():
                with db() as c:
                    return c.execute("SELECT COUNT(*) FROM claims WHERE status IN ('ready','processing')").fetchone()[0]
            def quit_app(icon, item):
                icon.stop()
                os._exit(0)
            icon = pystray.Icon("ClaimScanner", img, "Claim Scanner", menu=pystray.Menu(
                pystray.MenuItem("Open Claim Scanner", lambda i, it: webbrowser.open(home_url), default=True),
                pystray.MenuItem("Accuracy stats", lambda i, it: webbrowser.open(f"http://127.0.0.1:{config.PORT}/stats")),
                pystray.MenuItem("Open folder (settings, log)", lambda i, it: os.startfile(BASE)),
                pystray.MenuItem(lambda it: f"Install update ({UPD['latest']})",
                                 lambda i, it: threading.Thread(target=install_update, daemon=True).start(),
                                 visible=lambda it: UPD["state"] == "available"),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", quit_app)))
            def tip():                                # hover text: how many claims are waiting
                while True:
                    try:
                        n = queued()
                        icon.title = f"Claim Scanner: {n} claim{'s' if n != 1 else ''} waiting" if n else "Claim Scanner: queue empty"
                        if UPD["state"] == "available" and not tip.told:
                            tip.told = True
                            icon.update_menu()
                            icon.notify(f"Update {UPD['latest']} is ready. Open Claim Scanner and click Update now.", "Claim Scanner")
                    except Exception:
                        pass
                    time.sleep(5)
            tip.told = False
            threading.Thread(target=tip, daemon=True).start()
            def setup(icon):
                icon.visible = True
                print("  Tray icon: on (by the clock; right-click it to Quit)")
                try:
                    icon.notify("Running in the background. Right-click this icon by the clock to quit.", "Claim Scanner")
                except Exception:
                    pass
            icon.run(setup=setup)
        except Exception as e:
            print(f"  Tray icon: off ({e})")
    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    import webbrowser
    from werkzeug.serving import make_server
    home_url = f"http://127.0.0.1:{config.PORT}/home"
    try:                                              # already running? just show it
        if urllib.request.urlopen(f"http://127.0.0.1:{config.PORT}/api/ping", timeout=1).read() == b"claim-scanner":
            webbrowser.open(home_url)
            sys.exit(0)
    except Exception:
        pass
    ip = lan_ip()
    crt, key = ensure_certs(ip)
    https = make_server("0.0.0.0", HTTPS_PORT, app, threaded=True, ssl_context=(crt, key))
    threading.Thread(target=https.serve_forever, daemon=True).start()
    print(f"\n  Claim scanner {VERSION}")
    print(f"  Name list: {len(NAMES):,} first names loaded" if NAMES else
          "  Name list: NOT LOADED -> sex can't be guessed. Put names.zip (ssa.gov/oact/babynames/names.zip) in this folder and restart.")
    print(f"  Phone setup (open in Safari, one time):  http://{ip}:{config.PORT}/setup?k={TOKEN}")
    print(f"  Phone scanner:                           https://{ip}:{HTTPS_PORT}/?k={TOKEN}")
    print(f"  Accuracy so far:                         http://localhost:{config.PORT}/stats")
    print(f"  Home page:                               {home_url}\n")
    if "--updated" in sys.argv:
        print(f"  Updated to {VERSION}")
    start_tray(home_url)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(home_url)).start()
    app.run(host="0.0.0.0", port=config.PORT, threaded=True)
