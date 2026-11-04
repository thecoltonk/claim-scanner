"""Test a local vision AI (running in LM Studio on your gaming PC) on the handwriting in your queued scans.

On the gaming PC (one time):
  1. Install LM Studio (lmstudio.ai). Search for and download:  Qwen2.5-VL-7B-Instruct  (Q4_K_M, ~5 GB)
  2. Developer tab -> load that model -> Start Server, and turn on "Serve on Local Network".
     Note the address it shows, e.g.  http://192.168.0.60:1234
     (Windows may ask to allow LM Studio through the firewall: allow on Private networks.)

On the laptop:
  py test_vlm.py 192.168.0.60          <- the gaming PC's IP

Only small crops of the handwritten lines (date of service, holder birth date, Dx) go to the gaming PC,
over your own network. Nothing leaves the house.
"""
import base64, io, json, os, sqlite3, sys, time, urllib.request
from PIL import Image

BASE = os.path.dirname(os.path.abspath(__file__))
if len(sys.argv) < 2:
    sys.exit("Usage: py test_vlm.py <gaming PC IP>   e.g.  py test_vlm.py 192.168.0.60")
host = sys.argv[1]
URL = host if host.startswith("http") else (f"http://{host}" if ":" in host else f"http://{host}:1234")
MONTHS = "January February March April May June July August September October November December".split()

sys.path.insert(0, BASE)
import config
CODES = config.H_CODES


def call(path, body=None, timeout=120):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


try:
    models = [m["id"] for m in call("/v1/models", timeout=10)["data"]]
except Exception as e:
    sys.exit(f"Can't reach LM Studio at {URL} ({e}).\nIs the server started with 'Serve on Local Network' on, "
             f"and is LM Studio allowed through the gaming PC's firewall?")
model = next((m for m in models if "vl" in m.lower() or "vision" in m.lower()), models[0] if models else None)
print(f"LM Studio at {URL}, using model: {model}\n")


def ask(img, prompt):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92)
    b64 = base64.b64encode(buf.getvalue()).decode()
    t = time.time()
    out = call("/v1/chat/completions", {
        "model": model, "temperature": 0, "max_tokens": 20,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]})
    return out["choices"][0]["message"]["content"].strip(), time.time() - t


def crop(im, box):
    x1, y1, x2, y2 = box
    h = max(10, y2 - y1)
    return im.crop((max(0, x1 - h * .6), max(0, y1 - h * .5), min(im.width, x2 + h * .6), min(im.height, y2 + h * .5)))


def prompt_for(field, f):
    if field == "dos":
        return ("This is a handwritten date of service on a vision insurance form, written as month/day/2-digit year "
                "(for example 10/26/26). Read exactly what is written. Reply with only the date as MM/DD/YYYY.")
    if field == "insured_dob":
        hint = ""
        p = f.get("printed", "")                      # like "04/??/1983"
        if p[:2].isdigit() and p[-4:].isdigit():
            hint = f" The form also prints the month and year: {MONTHS[int(p[:2]) - 1]} {p[-4:]}."
        return ("This shows a birth date on a vision insurance form. Someone handwrote the full date as "
                f"month/day/2-digit year.{hint} Read the handwritten date exactly as written. "
                "Reply with only the date as MM/DD/YYYY.")
    return (f"This is a handwritten diagnosis code on a vision insurance form. It is one of: {', '.join(CODES)}. "
            "Reply with only the code.")


db = sqlite3.connect(os.path.join(BASE, "claims.db"))
db.row_factory = sqlite3.Row
rows = db.execute("SELECT id, img, data FROM claims WHERE status='ready' ORDER BY id").fetchall()
if not rows:
    print("No scans in the queue. Scan a few forms with the phone first, then run this again.")
for r in rows:
    d = json.loads(r["data"] or "{}")
    if not r["img"] or not os.path.exists(r["img"]):
        continue
    im = Image.open(r["img"])
    print(f"Scan {r['id']}")
    for field, label in (("dos", "Date of service"), ("insured_dob", "Holder DOB"), ("hcodes", "Dx")):
        f = d.get(field) or {}
        if not f.get("box"):
            continue
        try:
            ans, secs = ask(crop(im, f["box"]), prompt_for(field, f))
        except Exception as e:
            ans, secs = f"(error: {e})", 0
        guess = ", ".join(f["v"]) if isinstance(f.get("v"), list) else (f.get("v") or "?")
        print(f"  {label:16} app guessed {guess:12} | AI read: {ans!r:16} ({secs:.1f}s)")
    print()
print("Tell Claude the real values for these, and which got closer.")
