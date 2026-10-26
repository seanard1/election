#!/usr/bin/env python3
"""
Pulls the City of Waterloo's arearesults.json over SFTP every 30 seconds and
pushes it to the waterloo-relay Cloudflare Worker whenever it changes.

Needs curl with SFTP support (check: curl -V | grep -o sftp).
Settings come from the .env beside this script:
  WATERLOO_SFTP_HOST=sftp.waterloo.ca
  WATERLOO_SFTP_USER=therecord
  WATERLOO_SFTP_PASS=...
  WATERLOO_SFTP_PATH=arearesults.json      # path after login; adjust if the file is in a folder
  RELAY_URL=https://waterloo-relay.<you>.workers.dev/
  RELAY_KEY=...                            # same as the Worker's RELAY_KEY secret

Usage:
  python3 waterloo_relay.py --once     # fetch once, push, exit (test)
  python3 waterloo_relay.py            # run every 30 s
"""
import hashlib, json, os, signal, subprocess, sys, time, urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
for line in open(os.path.join(HERE, ".env"), encoding="utf-8") if os.path.exists(os.path.join(HERE, ".env")) else []:
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

HOST = os.environ.get("WATERLOO_SFTP_HOST", "sftp.waterloo.ca")
USER = os.environ.get("WATERLOO_SFTP_USER", "")
PASS = os.environ.get("WATERLOO_SFTP_PASS", "")
PATH = os.environ.get("WATERLOO_SFTP_PATH", "arearesults.json").lstrip("/")
RELAY_URL = os.environ.get("RELAY_URL", "")
RELAY_KEY = os.environ.get("RELAY_KEY", "")
INTERVAL = int(os.environ.get("WATERLOO_POLL_SECONDS", "30"))
# Server identity is checked against ~/.ssh/known_hosts, filled in when you first accepted the key with sftp.
# Optional extra pin (works only with libssh2 builds of curl): WATERLOO_HOSTKEY_SHA256=oAGeoT0MO/...
HOSTKEY = os.environ.get("WATERLOO_HOSTKEY_SHA256", "")

def log(*a): print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)

def fetch():
    url = f"sftp://{HOST}/~/{PATH}"
    # credentials go in on stdin, never on the command line
    cfg = f'user = "{USER}:{PASS}"\n'
    args = ["curl", "-sS", "--fail", "--max-time", "25", "-K", "-", url]
    if HOSTKEY:
        args[1:1] = ["--hostpubsha256", HOSTKEY]
    r = subprocess.run(args, input=cfg.encode(), capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip() or f"curl exit {r.returncode}")
    body = r.stdout
    json.loads(body.decode("utf-8-sig"))   # refuse half-written files
    return body

def push(body):
    req = urllib.request.Request(RELAY_URL, data=body, method="PUT",
                                 headers={"X-Relay-Key": RELAY_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()

RUN = True
def stop(*_):
    global RUN; RUN = False
signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)

def main():
    once = "--once" in sys.argv
    if not (USER and PASS and RELAY_URL and RELAY_KEY):
        log("missing settings in .env (WATERLOO_SFTP_USER/PASS, RELAY_URL, RELAY_KEY)"); sys.exit(1)
    last = None
    log(f"relaying sftp://{HOST}/~/{PATH} -> {RELAY_URL} every {INTERVAL} s")
    while RUN:
        t0 = time.time()
        try:
            body = fetch()
            h = hashlib.sha256(body).hexdigest()
            if h != last:
                push(body)
                last = h
                log(f"pushed new version ({len(body):,} bytes)")
        except Exception as e:
            log(f"error: {e}")
        if once:
            break
        while RUN and time.time() - t0 < INTERVAL:
            time.sleep(0.5)
    log("stopped")

if __name__ == "__main__":
    main()
