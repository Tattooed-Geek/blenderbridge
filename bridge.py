#!/usr/bin/env python3
"""
BlenderBridge - a tiny local render server for Blender.

A single-file, zero-dependency HTTP bridge that lets a machine on your LAN
submit render jobs (pure JSON parameters) to a Mac that runs Blender
headless with its GPU. Typical setup: an AI assistant or another computer
sends a job, your Mac renders with Metal, and the resulting MP4/PNG is
downloaded over HTTP.

Security model (the short version):
  - The network never sends code. Jobs are parameter objects validated and
    clamped by the server; the only Python ever executed is the template
    files that YOU placed in templates/.
  - New/updated templates submitted over the network land as <name>.py.pending
    and are shown as a diff in this console. Nothing becomes active until a
    human types "o" (approve) or "n" (reject) in THIS terminal. There is no
    network route that can approve.
  - Token required for every mutating call (constant-time comparison).
  - One render at a time; /cancel terminates the Blender subprocess.

Requires: Python 3.9+ (stdlib only) and Blender (brew install --cask blender).
Stop: Ctrl-C. Everything lives in this folder (out/, templates/, .token).
"""
import ast
import difflib
import glob
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE, "templates")
OUT_DIR = os.path.join(BASE, "out")
TOKEN_FILE = os.path.join(BASE, ".token")
PORT = 8777
MAX_BODY = 64 * 1024

os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

JOBS = {}            # job_id -> {state, template, error, file}
JOBS_LOCK = threading.Lock()
CURRENT = None       # job_id of the running render
CURRENT_PROC = None  # subprocess.Popen of the running render
PENDING = None       # template awaiting local approval: {"name", "path"}
PENDING_LOCK = threading.Lock()


def find_blender():
    """Locate the Blender binary: PATH first, then /Applications/Blender*.app."""
    p = shutil.which("blender") or shutil.which("Blender")
    if p:
        return p
    hits = sorted(glob.glob("/Applications/Blender*.app/Contents/MacOS/Blender"))
    return hits[-1] if hits else None


def load_or_create_token():
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE).read().strip()
    tok = uuid.uuid4().hex[:16]
    open(TOKEN_FILE, "w").write(tok)
    return tok


def templates_available():
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(TEMPLATES_DIR)
        if f.endswith(".py") and not f.startswith("_")
    )


def template_hashes():
    """First 16 hex chars of SHA-256 per template (remote integrity check)."""
    out = {}
    for f in sorted(os.listdir(TEMPLATES_DIR)):
        if f.endswith(".py") and not f.startswith("_"):
            h = hashlib.sha256(open(os.path.join(TEMPLATES_DIR, f), "rb").read()).hexdigest()[:16]
            out[os.path.splitext(f)[0]] = h
    return out


def approval_loop():
    """Console reader: 'o' approves the pending template, 'n' rejects it.

    This is the ONLY way a network-submitted template becomes active - no HTTP
    route can approve on the human's behalf.
    """
    global PENDING
    while True:
        line = sys.stdin.readline()
        if not line:  # stdin closed (started without a TTY)
            print("[template] WARNING: no terminal attached - template "
                  "approval is unavailable. Restart the bridge inside a "
                  "Terminal to enable it.", flush=True)
            return
        cmd = line.strip().lower()
        if not cmd:
            continue
        with PENDING_LOCK:
            pend = PENDING
        if cmd in ("o", "oui", "y", "yes"):
            if not pend:
                print("[template] nothing to approve", flush=True)
                continue
            target = os.path.join(TEMPLATES_DIR, pend["name"] + ".py")
            backup_made = os.path.exists(target)
            if backup_made:
                bdir = os.path.join(TEMPLATES_DIR, "_backups")
                os.makedirs(bdir, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                shutil.copy2(target,
                             os.path.join(bdir, f"{pend['name']}.{stamp}.py"))
            os.replace(pend["path"], target)
            with PENDING_LOCK:
                PENDING = None
            print(f"[template] '{pend['name']}' APPROVED and now active "
                  f"(previous version backed up: {'yes' if backup_made else 'n/a'})",
                  flush=True)
        elif cmd in ("n", "non", "no"):
            with PENDING_LOCK:
                pend, PENDING = PENDING, None
            if pend:
                try:
                    os.remove(pend["path"])
                except OSError:
                    pass
                print(f"[template] '{pend['name']}' rejected, nothing changed",
                      flush=True)
            else:
                print("[template] nothing to reject", flush=True)
        elif cmd in ("h", "help", "?"):
            print("[template] commands: o = approve pending template, "
                  "n = reject", flush=True)
        # any other input is ignored


def run_job(job_id, template, params):
    global CURRENT, CURRENT_PROC
    job = JOBS[job_id]
    job_dir = os.path.join(OUT_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    params_path = os.path.join(job_dir, "params.json")
    with open(params_path, "w") as f:
        json.dump(params, f)
    template_path = os.path.abspath(os.path.join(TEMPLATES_DIR, template + ".py"))
    log_path = os.path.join(job_dir, "blender.log")
    cmd = [BLENDER, "-b", "--factory-startup", "-P", template_path, "--",
           params_path, job_dir]
    if sys.platform == "darwin":  # prevent sleep during long renders
        caff = shutil.which("caffeinate")
        if caff:
            cmd = [caff, "-i"] + cmd
    job["state"] = "running"
    with open(log_path, "w") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
        with JOBS_LOCK:
            CURRENT_PROC = proc
        rc = proc.wait()
    with JOBS_LOCK:
        CURRENT = None
        CURRENT_PROC = None
    if rc == 0:
        artifacts = [f for f in os.listdir(job_dir)
                     if f.endswith((".mp4", ".png"))]
        if artifacts:
            job["file"] = os.path.join(job_dir, artifacts[0])
            job["state"] = "done"
        else:
            job["state"] = "error"
            job["error"] = "Blender produced no file (see blender.log)"
    else:
        job["state"] = "error"
        job["error"] = f"Blender exited with code {rc}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        tok = self.headers.get("X-Bridge-Token", "")
        return hmac.compare_digest(tok, TOKEN)

    def _read_json(self):
        """Returns (data, None) or (None, (http_code, message))."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY:
                return None, (413, "body too large")
            return json.loads(self.rfile.read(length)), None
        except Exception:
            return None, (400, "invalid JSON")

    def do_GET(self):
        if self.path == "/health":
            with PENDING_LOCK:
                pend = PENDING["name"] if PENDING else None
            return self._json(200, {
                "bridge": "blender-bridge/1.4",
                "blender": BLENDER,
                "templates": templates_available(),
                "template_hashes": template_hashes(),
                "busy": CURRENT is not None,
                "pending_approval": pend,
            })
        m = re.fullmatch(r"/jobs/([a-f0-9-]{36})", self.path)
        if m:
            with JOBS_LOCK:
                job = JOBS.get(m.group(1))
                snap = dict(job) if job else None
            if not snap:
                return self._json(404, {"error": "unknown job"})
            snap.pop("file", None)
            snap["log_tail"] = tail_log(m.group(1))
            return self._json(200, snap)
        m = re.fullmatch(r"/jobs/([a-f0-9-]{36})/file", self.path)
        if m:
            with JOBS_LOCK:
                job = JOBS.get(m.group(1))
                path = job.get("file") if job else None
                state = job.get("state") if job else None
            if not path or state != "done" or not os.path.exists(path):
                return self._json(404, {"error": "file unavailable"})
            self.send_response(200)
            self.send_header("Content-Type",
                             "video/mp4" if path.endswith(".mp4") else "image/png")
            self.send_header("Content-Length", str(os.path.getsize(path)))
            self.end_headers()
            with open(path, "rb") as f:
                while chunk := f.read(256 * 1024):
                    self.wfile.write(chunk)
            return
        return self._json(404, {"error": "unknown route"})

    def do_POST(self):
        global CURRENT
        if self.path == "/cancel":
            return self._post_cancel()
        if self.path == "/template":
            return self._post_template()
        if self.path != "/render":
            return self._json(404, {"error": "unknown route"})
        if not self._authorized():
            return self._json(403, {"error": "invalid token"})
        data, err = self._read_json()
        if err:
            return self._json(err[0], {"error": err[1]})
        template = data.get("template", "")
        if template not in templates_available() or not re.fullmatch(r"[a-z_]+", template):
            return self._json(400, {"error": f"unknown template. Available: {templates_available()}"})
        if os.path.exists(os.path.join(TEMPLATES_DIR, template + ".py.pending")):
            return self._json(409, {"error": f"template '{template}' has a version "
                                           "PENDING approval - approve or reject it "
                                           "in the bridge console first"})
        params = data.get("params", {})
        if not isinstance(params, dict):
            return self._json(400, {"error": "params must be an object"})
        with JOBS_LOCK:
            if CURRENT is not None:
                return self._json(409, {"error": "a render is already running "
                                                "(POST /cancel to stop it)"})
        job_id = str(uuid.uuid4())
        with JOBS_LOCK:
            JOBS[job_id] = {"state": "queued", "template": template,
                            "error": None, "file": None}
            CURRENT = job_id
        threading.Thread(target=run_job, args=(job_id, template, params),
                         daemon=True).start()
        print(f"[render] job {job_id} template={template}", flush=True)
        return self._json(202, {"job_id": job_id})

    def _post_cancel(self):
        """Stop the running render (Blender subprocess gets SIGTERM, then SIGKILL)."""
        tok = self.headers.get("X-Bridge-Token", "")
        if not hmac.compare_digest(tok, TOKEN):
            return self._json(403, {"error": "invalid token"})
        with JOBS_LOCK:
            target = CURRENT
            proc = CURRENT_PROC
        if not target or not proc:
            return self._json(404, {"error": "no render running"})
        print(f"[cancel] stopping job {target}", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        return self._json(200, {"ok": True, "cancelled": target})

    def _post_template(self):
        """Receive a template {name, code}: DO NOT activate it.

        Writes <name>.py.pending, prints a unified diff in this console and
        waits for the local human to type 'o' (approve) or 'n' (reject).
        """
        global PENDING
        tok = self.headers.get("X-Bridge-Token", "")
        if not hmac.compare_digest(tok, TOKEN):
            return self._json(403, {"error": "invalid token"})
        data, err = self._read_json()
        if err:
            return self._json(err[0], {"error": err[1]})
        name = data.get("name", "")
        code = data.get("code", "")
        if not re.fullmatch(r"[a-z_]{1,64}", name) or "__" in name:
            return self._json(400, {"error": "invalid name (a-z and _ only)"})
        if not isinstance(code, str) or not (50 <= len(code) <= 300_000):
            return self._json(400, {"error": "code empty or too large"})
        try:
            ast.parse(code)
        except SyntaxError as e:
            return self._json(400, {"error": f"invalid Python syntax: line {e.lineno}"})
        target = os.path.abspath(os.path.join(TEMPLATES_DIR, name + ".py"))
        if not target.startswith(os.path.abspath(TEMPLATES_DIR) + os.sep):
            return self._json(400, {"error": "invalid name"})
        pending_path = target + ".pending"
        with open(pending_path, "w") as f:
            f.write(code)
        old_lines = open(target).read().splitlines() if os.path.exists(target) else []
        diff = list(difflib.unified_diff(old_lines, code.splitlines(),
                                         "before", "after", lineterm=""))
        print("\n" + "=" * 62, flush=True)
        print(f"[template] '{name}' PENDING APPROVAL "
              f"({'update' if old_lines else 'new template'}, "
              f"{len(code)} bytes)", flush=True)
        print(f"[template] diff preview ({min(len(diff), 60)} of "
              f"{len(diff)} lines) - full file: {pending_path}", flush=True)
        for line in diff[:60]:
            print("  " + line, flush=True)
        if len(diff) > 60:
            print(f"  ... (+{len(diff) - 60} more lines)", flush=True)
        print("[template] >>> type 'o' to APPROVE, 'n' to REJECT <<<", flush=True)
        print("=" * 62 + "\n", flush=True)
        with PENDING_LOCK:
            PENDING = {"name": name, "path": pending_path}
        return self._json(200, {"ok": True, "name": name, "status": "pending",
                                "message": "awaiting local approval "
                                           "('o' in the bridge console)"})


def tail_log(job_id, n=25):
    path = os.path.join(OUT_DIR, job_id, "blender.log")
    if not os.path.exists(path):
        return []
    lines = open(path, errors="replace").read().splitlines()
    return lines[-n:]


BLENDER = find_blender()
TOKEN = load_or_create_token()

if __name__ == "__main__":
    if not BLENDER:
        sys.exit("Blender not found. Install it with:  brew install --cask blender")
    threading.Thread(target=approval_loop, daemon=True).start()
    print("=" * 62)
    print(" BlenderBridge ready (v1.4 - local template approval)")
    print(f"   Blender  : {BLENDER}")
    print(f"   Templates: {', '.join(templates_available())}")
    print(f"   Token    : {TOKEN}   <- give this to your HTTP client")
    print(f"   Listening: 0.0.0.0:{PORT} (LAN only)")
    print("   Templates received over the network stay PENDING until you "
          "type 'o' here.")
    print("   Stop     : Ctrl-C")
    print("=" * 62)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
