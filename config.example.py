# ===== Edit these =====
# Settings for Claim Scanner. Next to ClaimScanner.exe (or app.py) as config.py; restart after editing.

PORT = 5000

# The diagnosis codes the practice uses
H_CODES = ["H52.13", "H52.03", "H52.4", "H52.223"]

# Backup: the word the doctor writes next to the code -> code (used only if the code itself is unreadable)
H_CODE_WORDS = {
    "myopia": "H52.13",
    "hyperopia": "H52.03",
    "presbyopia": "H52.4",
    "astigmatism": "H52.223",
}

# Procedure lines filled on EVERY claim (same for everyone)
# Service lines (box 24): CPT, charge, units, diagnosis pointer
CPT_LINES = [
    {"code": "92004", "fee": "0.00", "units": "1", "pointer": "A"},   # <- your fees
    {"code": "92015", "fee": "0.00", "units": "1", "pointer": "A"},
]

RENDERING_NPI = ""             # box 24j: the doctor's NPI
TAX_ID_TYPE = "EIN"            # box 25

# How sure the H-code match has to be (0-100)
H_MATCH_MIN = 70    # below this, ignored
H_MATCH_SURE = 90   # at/above this, no yellow flag

# Where the handwritten section starts, as a fraction of page height
# (0 = top, 1 = bottom). Only text below this line is checked for H codes and the date.
HANDWRITTEN_TOP = 0.5

# Printed label text next to each value on the auth form (fuzzy-matched, so OCR typos are OK)
AUTH_LABELS = ["authorization", "auth #"]
NAME_LABELS = ["patient name"]
AUTH_VALUE = r"\d{6,14}"   # what an auth number looks like

# Name -> gender guess: below this confidence it gets a yellow flag
GENDER_SURE = 0.95

# Local AI (LM Studio on the gaming PC) for the handwritten fields. "" = off.
# Give the gaming PC a fixed IP (DHCP reservation) so this doesn't change.
AI_URL = ""                    # e.g. "http://192.168.0.50:1234" (LM Studio) or "http://127.0.0.1:1234"
