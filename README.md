# Claim Scanner

Bill VSP vision claims in Eyefinity eClaim (2020source) without typing them in.

1. **Phone:** point an iPhone at each VSP Patient Eligibility Report. It snaps the photo on its own.
2. **PC:** Claim Scanner reads the auth #, date of service, holder DOB, Dx codes and copay. It reads the printed text and the doctor's handwriting, all on your own PC.
3. **Firefox:** a Tampermonkey panel in eClaim fills in the claim form. You look it over and click Submit yourself. It never submits for you.

Fields it's sure about get a ✓. Anything it isn't sure about gets ⚠, with a crop of the handwriting and buttons for the likely answers. In Confirm mode you can check and fix every claim on the phone right after you scan it.

> **Patient data stays on your network.** Photos, the queue and the accuracy log stay on the PC running Claim Scanner. The optional AI reader runs in LM Studio on a PC you own. Nothing is sent to any cloud service. Scans are deleted once a claim is marked billed. You are responsible for HIPAA compliance where you use it (disk encryption, who can use the PC, and so on).
>
> Not affiliated with VSP or Eyefinity.

## Install (Windows)

1. Download `ClaimScanner-windows.zip` from [Releases](../../releases) and unzip it somewhere permanent, like `Documents\ClaimScanner`.
2. Double-click **ClaimScanner.exe**. Windows may say "Windows protected your PC" because the app isn't code-signed. Click **More info → Run anyway**. When the firewall asks, allow it on **Private** networks.
3. A home page opens in your browser. Edit the `config.py` file shown there with your fees, NPI and H codes, then **Quit** and reopen.
4. **Phone (one time):** scan the QR code on the home page with the iPhone camera. Then:
   - install the certificate, and turn on trust for it under Settings → General → About → Certificate Trust Settings;
   - open the scanner and Share → **Add to Home Screen**.
5. **Firefox (one time):** install [Tampermonkey](https://www.tampermonkey.net/), then on the home page click **Install / update the eClaim panel** and confirm.

Your data (`config.py`, `claims.db`, `certs/`, `token.txt`, the log) lives next to the exe.

**Updates:** when there's a new version, the home page shows **Update now** (and the tray icon offers it). It downloads, swaps the program files, and restarts; your data stays. The eClaim panel updates itself from the app (Tampermonkey), or click the link in the panel when it says an update is ready.

## Billing a stack

1. Open Claim Scanner (the home page shows when it's ready).
2. **Phone:** hold each form inside the frame.
   - ✓ means done; tap Scan next.
   - ✗ means retake, or "Use it anyway".
   - ✗ "already scanned" means that auth # is already queued or billed, so it wasn't added again.
   - Turn **Confirm: on** to check each claim against the paper and fix anything on the spot.
3. **eClaim:** press the blue button (or **F2**) to Search → the claim form fills itself → you Submit → Billed ✓, and the next claim loads.
   - ⚠ boxes show the handwriting with buttons for the likely answers.
   - Fill again, Skip, Delete and View scan are underneath.

The **Accuracy stats** page shows how often each field was right, split by ✓ and ⚠. The log behind it (`accuracy.jsonl`) only records right/wrong and confidence scores, never values.

## Optional: AI reader (LM Studio)

A local vision model reads the handwritten fields a second time, and the two readers vote. It works on a modest GPU (it was tuned on an Intel Arc B580 with the Vulkan runtime).

1. Install [LM Studio](https://lmstudio.ai) and download **Qwen3-VL-8B-Instruct** (Q4_K_M).
2. **Developer** tab → start the server. In its settings, turn on **Serve on Local Network** and **Just-in-Time model loading**.
3. **App settings** (Ctrl+,) → turn on **Run the LLM server on login**. After that the PC only needs to be on and signed in.
4. In `config.py`, set `AI_URL = "http://<that PC's IP>:1234"`, or `http://127.0.0.1:1234` if it's the same PC. Give that PC a fixed IP in your router.

If the AI PC is off, Claim Scanner falls back to the built-in reader and marks more fields ⚠.

## Run from source / build

```
py -m pip install -r requirements.txt
py app.py                     # --no-browser to skip opening the home page
py -m pip install pyinstaller
pyinstaller ClaimScanner.spec # -> dist/ClaimScanner/ClaimScanner.exe
```

Every push to `main` that builds and passes the smoke test is published as a release named after `VERSION` in `app.py` (bump it to make a new one).

Sex guesses use the public SSA baby-name list (downloaded on first run). If that download fails, put [names.zip](https://www.ssa.gov/oact/babynames/names.zip) next to the app.

## License

MIT
