# BlenderBridge

A single-file, zero-dependency bridge that lets other machines on your LAN
render with [Blender](https://www.blender.org/) on your Mac — using its GPU
(Metal) — including remote template management, with a token as the single
trust anchor.

Typical setup: an AI assistant (or any HTTP client) on your network sends a
**render job made of pure parameters**, your Mac renders it headless with
Blender + Metal, and the resulting MP4/PNG is downloaded over HTTP. Templates
(scene recipes) can also be pushed, updated and versioned remotely.

```
+----------------+     POST /render {template, params}     +----------------+
|  Any client    |  ------------------------------------->  |   Your Mac     |
|  (AI, script)  |  <----- 202 {job_id} ------------------  |                |
|                |     GET  /jobs/{id}        (poll)        |  BlenderBridge |
|                |  <----- state: queued->running->done --  |  (this server) |
|                |     GET  /jobs/{id}/file   (download)    |                |
+----------------+  <----- backrooms.mp4 -----------------  |  Blender+Metal |
                                                      +----------------+
```

Tested with Blender 4.3 (Linux/CPU) and Blender 5.x (macOS/Metal); Python 3.9+
with **standard library only** — no pip installs.

---

## Features

- **One file, zero dependencies** — `bridge.py` uses only the Python standard library
- **Pure-parameter jobs** — render requests carry only values (numbers, booleans, closed enumerations), clamped server-side by the template
- **Remote template management** — push, update and remove templates over HTTP: syntax check, atomic write, automatic backups, SHA-256 fingerprints
- **Template integrity checks** — `/health` publishes a SHA-256 fingerprint per template so clients can verify what the Mac is running
- **Job lifecycle** — `queued → running → done|error`, Blender runs as a subprocess (a crash never takes the bridge down)
- **Remote cancel** — `/cancel` SIGTERMs (then SIGKILLs) the running render
- **Single-render gate** — concurrent renders are refused with `409` instead of fighting over the GPU
- **macOS niceties** — auto-detects `/Applications/Blender*.app`, wraps renders in `caffeinate` to prevent sleep
- **Everything on disk** — every job keeps `params.json`, `blender.log` and its output in `out/<job_id>/`

---

## Quick start

### 1. Install Blender

```bash
brew install --cask blender
```

(Any recent Blender works — the bridge auto-detects `/Applications/Blender*.app`
or whatever `blender` is on your `PATH`.)

### 2. Get this repository

```bash
git clone https://github.com/Tattooed-Geek/blenderbridge.git
cd blenderbridge
```

### 3. Start the bridge

```bash
bash start_bridge.sh
```

The console prints a **token** — keep it, your HTTP client needs it.
To stop: `Ctrl-C`.

### 4. Send a job from any machine on the LAN

```bash
TOKEN=xxxxxxxxxxxxxxxx   # the token printed by the bridge

# Submit a render (async)
JOB=$(curl -s -X POST http://<mac-ip>:8777/render \
  -H "X-Bridge-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"template":"backrooms","params":{"seconds":3,"fps":24,"width":854,"quality":"draft"}}')
JOBID=$(echo "$JOB" | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")

# Poll until done
curl -s http://<mac-ip>:8777/jobs/$JOBID

# Download the MP4
curl -s -o backrooms.mp4 http://<mac-ip>:8777/jobs/$JOBID/file
```

Or with Python:

```python
import json, time, urllib.request

BRIDGE = "http://192.168.1.50:8777"   # your Mac's LAN IP
TOKEN  = "xxxxxxxxxxxxxxxx"

def post(path, payload):
    req = urllib.request.Request(
        BRIDGE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Bridge-Token": TOKEN})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

print(post("/render", {"template": "backrooms",
                       "params": {"seconds": 10, "width": 1920, "quality": "good"}}))
# then poll GET /jobs/{id} until state == "done", then GET /jobs/{id}/file
```

---

## HTTP API

| Method | Route | Auth | Description |
|---|---|---|---|
| GET | `/health` | — | Version, Blender path, template list, SHA-256 per template, busy flag |
| POST | `/render` | token | `{template, params}` → `202 {job_id}` · `409` if a render is already running |
| GET | `/jobs/{id}` | — | State (`queued`/`running`/`done`/`error`), error message, last 25 log lines |
| GET | `/jobs/{id}/file` | — | The rendered artifact (`video/mp4` or `image/png`) |
| POST | `/cancel` | token | Stops the running render (SIGTERM → SIGKILL) · `404` if idle |
| POST | `/template` | token | `{name, code}` — validates, backs up the old version and activates the template immediately |

Auth = `X-Bridge-Token` header, compared in constant time.

### Example: `/health`

```json
{
  "bridge": "blender-bridge/1.5",
  "blender": "/opt/homebrew/bin/blender",
  "templates": ["backrooms"],
  "template_hashes": {"backrooms": "06b3e2c0cf90fc00"},
  "busy": false
}
```

---

## Security model

The design goal: **give a remote assistant a single, audited capability —
driving Blender — instead of a shell.** Trust is anchored on one secret: the
token.

**What the token holder can do**

- Submit render jobs whose `params` are plain values (numbers, booleans, strings
  from closed enumerations). The template clamps them to safe ranges
  (e.g. `seconds` 1–30, `width` 320–3840) regardless of what was sent.
- Push new templates or update existing ones. Each push is:
  - **name-validated** (`a-z_` only, no traversal) and **size-capped**
  - **syntax-checked** with Python's own `ast.parse` (rejected with the failing line number)
  - **written atomically** (`.tmp` + rename, so a half-written template can never run)
  - **backed up automatically** (`templates/_backups/`, 5 most recent kept per template)
  - **logged visibly** in the bridge console
  - **activated immediately** — this is trust-the-token by design
- Cancel the running render.
- Verify what's deployed: `/health` publishes a fingerprint of every template.

**What nobody without the token can do**

- Anything. Every mutating route returns `403` without a valid token
  (compared in constant time, immune to timing attacks).

**Honest caveat** — a template is Python running inside Blender (an unsandboxed
CPython). That is by design here: whoever holds the token controls the
templates, so **treat the token like a shell on the machine**. Keep it private,
rotate it freely (delete `.token` and restart), and don't run the bridge on an
untrusted network segment.

**Other properties**

- One render at a time; `/cancel` terminates the Blender subprocess cleanly
- Body size capped at 64 KB; all output stays under `out/`
- LAN-only by design — don't port-forward it

---

## Templates

A template is a small Python script that Blender runs headless:

```bash
blender -b --factory-startup -P templates/backrooms.py -- params.json out_dir
```

It reads a `params.json`, builds a scene through Blender's `bpy` API, sets up
the render and writes its artifact(s) into the output directory. Templates
should clamp every parameter they read — see
[`templates/backrooms.py`](templates/backrooms.py) for the reference
implementation (procedural liminal-space maze, GPU auto-detection, Blender
4.x/5.x compatibility).

**Shipped template: `backrooms`** — a procedural "Backrooms" liminal-space
maze: mustard wallpaper, damp carpet, ceiling tiles, fluorescent panels
(15 % dead, 15 % dim), slow handheld camera drift. All geometry and materials
are generated procedurally, no texture files needed.

| Parameter | Range | Default | Notes |
|---|---|---|---|
| `seconds` | 1–30 | `10` | Animation duration |
| `fps` | 12–30 | `24` | |
| `width` | 320–3840 | `1280` | Height derived 16:9, rounded even for H.264 |
| `quality` | `draft`/`good`/`final` | `good` | 24 / 96 / 256 Cycles samples |
| `seed` | any int | `42` | Same seed → same maze |
| `grid` | 6–14 | `10` | Maze size (N×N cells) |
| `engine` | `cycles`/`eevee` | `cycles` | Eevee requires a display/GPU context |
| `denoise` | bool | `true` | OpenImageDenoise |

### Pushing a template

```bash
python3 - << 'EOF'
import json, urllib.request

code = open("mytemplate.py").read()
req = urllib.request.Request(
    "http://<mac-ip>:8777/template",
    data=json.dumps({"name": "mytemplate", "code": code}).encode(),
    headers={"Content-Type": "application/json", "X-Bridge-Token": "TOKEN"})
print(urllib.request.urlopen(req).read())
EOF
```

Invalid Python is rejected up front with the failing line number; valid
templates are backed up and live instantly.

### Writing your own

1. Copy the `backrooms.py` structure: read `argv` after `--`, clamp inputs,
   build a scene, write into the output directory.
2. Test locally: `blender -b --factory-startup -P mytemplate.py -- params.json out`
3. Push it (above) or drop it in `templates/`.

---

## Project layout

```
blenderbridge/
├── bridge.py            # the whole server (single file, stdlib only)
├── start_bridge.sh      # launcher: bash start_bridge.sh
├── templates/
│   └── backrooms.py     # reference scene template
│   └── _backups/        # auto-rotated backups of replaced templates
├── out/<job_id>/        # params.json + blender.log + render artifacts
└── .token               # auto-generated token (chmod 600 recommended)
```

---

## Performance notes

Measured with the shipped `backrooms` template:

| Machine | Engine | Resolution | Quality | Time / frame |
|---|---|---|---|---|
| Linux VM, 4 vCPU (no GPU) | Cycles CPU | 640×480 | 64 samples | ~1 s |
| Mac (Apple Silicon, Metal) | Cycles GPU | 854×480 draft | 24 samples | ~0.6 s (first render includes Metal shader compilation) |
| Mac (Apple Silicon, Metal) | Cycles GPU | 1920×1080, 96 samples, denoised | good | ~25 s |

Rules of thumb:

- First render after a Blender/engine change includes shader compilation — much slower than subsequent ones
- Prefer `draft` for framing tests, `good` for finals, `final` for hero shots
- The MP4 is finalized only when the last frame is written: killing a render means no usable video (frames are lost — this is an H.264 muxer limitation, not a bridge bug)

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Blender not found` | `brew install --cask blender`, then restart the bridge |
| `403 invalid token` | Token mismatch — copy it from the bridge console |
| `409 a render is already running` | Wait, or `POST /cancel` |
| Renders are dark / lights missing | Check `out/<job>/blender.log`; verify GPU detection line (`GPU: METAL` etc.) |
| `Failed to denoise` in the log | Your Blender build lacks OpenImageDenoise — send `"denoise": false` |
| MP4 won't open after a killed render | Expected — H.264 index is written at the end; re-render |
| `height not divisible by 2` error | Use even dimensions (the shipped template rounds for you) |
| Port already in use | Change `PORT` at the top of `bridge.py` |

---

## FAQ

**Why not just SSH and run Blender?**
You can — but then the remote side has a shell on your Mac. This bridge
exposes exactly one capability (parameterized renders + template management)
with every action logged, which is a much smaller and more auditable surface.

**Why does the client pick templates by name instead of sending a script?**
Render jobs stay data-only that way. Code changes go through the dedicated
`/template` route where they're validated, backed up and fingerprinted.

**Can I run multiple renders in parallel?**
Not built-in — the bridge refuses with `409` while busy. Run a second bridge
instance on another port if you really want concurrent renders.

**Does it work on Linux/Windows?**
The bridge is pure Python and the Blender detection is macOS-focused
(`/Applications/*.app`), but `find_blender()` checks `PATH` first — so it
works anywhere Blender is on `PATH`. `caffeinate` is macOS-only and skipped
elsewhere.

---

## License

MIT — see [LICENSE](LICENSE).
