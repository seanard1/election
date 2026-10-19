#!/usr/bin/env python3
"""
Election-night Slack watcher for the City of Hamilton results feed.

Polls the feed every 30 seconds and posts ONE batched Slack message per cycle
when something happens:
  - first results in a race
  - lead change (must hold for 2 consecutive polls, ties never alert)
  - city flags a winner
  - a race has all polls reporting
  - city-wide poll milestones (25/50/75/100%) with a mayor + council summary
  - feed outage (after ~2 minutes of failures) and recovery

Python 3 standard library only. Config comes from a .env file beside this script
or from environment variables (see .env.example).

Usage:
  python3 election_watch.py                 # run for real
  python3 election_watch.py --dry-run       # print alerts instead of posting
  python3 election_watch.py --once          # one poll, then exit
  python3 election_watch.py --hello         # post a test message to Slack and exit
  python3 election_watch.py --simulate arearesults.json --interval 2 --dry-run
                                            # fake an entire election night from a snapshot
  python3 election_watch.py --reset         # forget what has already been alerted
"""
import argparse, copy, json, os, random, signal, sys, time
import urllib.error, urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- config
def load_env(path):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env(os.path.join(HERE, ".env"))

FEED_URL        = os.environ.get("FEED_URL", "https://elections2026.hamilton.ca/data/arearesults.json")
SLACK_TOKEN     = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL   = os.environ.get("SLACK_CHANNEL", "")
SLACK_WEBHOOK   = os.environ.get("SLACK_WEBHOOK_URL", "")
POLL_SECONDS    = int(os.environ.get("POLL_SECONDS", "30"))
STATE_FILE      = os.environ.get("STATE_FILE", os.path.join(HERE, "election_state.json"))
LEAD_CONFIRM    = int(os.environ.get("LEAD_CONFIRM_POLLS", "2"))
OUTAGE_AFTER    = int(os.environ.get("OUTAGE_AFTER_FAILURES", "4"))
LABEL           = os.environ.get("ALERT_LABEL", "")          # e.g. "TEST" on Monday
# trustee races: "winners" = only winners/all-polls-in (default), "all" = same as council, "none"
TRUSTEES        = os.environ.get("ALERT_TRUSTEES", "winners").lower()
ARCHIVE_DIR     = os.environ.get("ARCHIVE_DIR", "")          # save every new feed version here
MILESTONES      = [25, 50, 75, 100]

# Surnames the auto-caser gets wrong. Keep in sync with results.html.
NAME_FIX = {
    "DIFALCO": "DiFalco",
    "DEMELO": "DeMelo",
    "DI NICOLA": "Di Nicola",
    "TENBRINKE": "Tenbrinke",
}

# ---------------------------------------------------------------- formatting (CP style)
def case_word(w):
    if w in NAME_FIX:
        return NAME_FIX[w]
    out, up = [], True
    for ch in w.lower():
        out.append(ch.upper() if up else ch)
        up = ch in " -'"
    s = "".join(out)
    if s.startswith("Mc") and len(s) > 2:
        s = "Mc" + s[2].upper() + s[3:]
    return s

def display_name(raw):
    if "," not in raw:
        return raw
    last, first = raw.split(",", 1)
    last = last.strip()
    return f"{first.strip()} {NAME_FIX.get(last) or case_word(last)}"

def race_title(n):
    n = n.replace("Councillor - ", "")
    for board in ("Hamilton-Wentworth Catholic District School Board", "Hamilton-Wentworth District School Board"):
        if board in n:
            short = "Catholic board" if "Catholic" in board else "Public board"
            n = n.replace(f"Trustee, {board} - ", f"{short} trustee, ")
    return n

def cp_time(d=None):
    d = d or datetime.now()
    h, m = d.hour, d.minute
    ap = "a.m." if h < 12 else "p.m."
    h = h % 12 or 12
    return f"{h} {ap}" if m == 0 else f"{h}:{m:02d} {ap}"

def fmt(n):
    return f"{n:,}"

def pct(v, total):
    return f"{v / total * 100:.1f}%" if total else "—"

def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)

# ---------------------------------------------------------------- feed
def fetch_feed(url):
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{sep}_={int(time.time())}",
                                 headers={"User-Agent": "HamiltonSpectator-ElectionWatch",
                                          "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=15) as r:
        stale = r.headers.get("X-Feed-State") == "stale"
        data = json.loads(r.read().decode("utf-8-sig"))
    if "areaResults" not in data:
        raise ValueError("unexpected feed shape")
    return data, stale

def flatten(data):
    races = []
    for block in data.get("areaResults") or []:
        for area_id, area in block.items():
            st = area.get("statistics") or {}
            for c in area.get("contestResults") or []:
                cands = c.get("choiceResults") or []
                total = sum(int(x.get("votes") or 0) for x in cands)
                ranked = sorted(cands, key=lambda x: -int(x.get("votes") or 0))
                top = int(ranked[0].get("votes") or 0) if ranked else 0
                n_top = sum(1 for x in ranked if int(x.get("votes") or 0) == top)
                leader = ranked[0] if total and n_top == 1 else None
                races.append({
                    "id": str(c.get("id")),
                    "name": c.get("contestName", ""),
                    "title": race_title(c.get("contestName", "")),
                    "sort": c.get("sort", 0),
                    "acclaimed": bool(c.get("isAcclaimed")),
                    "polls": int(st.get("polls") or 0),
                    "closed": int(st.get("closedPolls") or 0),
                    "total": total,
                    "ranked": ranked,
                    "leader": leader,
                    "tied": bool(total) and n_top > 1,
                    "winners": [x for x in cands if x.get("isWinner")],
                })
    return sorted(races, key=lambda r: r["sort"])

def standing(r, n=None):
    """'Andrea Horwath 41.2%, Rob Cooper 22.0%' for the top n."""
    rows = r["ranked"][:n] if n else r["ranked"]
    def one(x):
        v = int(x["votes"] or 0)
        share = f"{v / r['total'] * 100:.0f}%" if r["total"] else "0%"
        return f"{display_name(x['choiceName'])}, {fmt(v)} vote{'' if v == 1 else 's'} ({share})"
    return "; ".join(one(x) for x in rows)

def surname(raw):
    last = raw.split(",", 1)[0].strip()
    return NAME_FIX.get(last) or case_word(last)

def margin(r):
    """' — Wilson leads by 81 votes' (empty when tied or no votes)."""
    if not r["leader"] or len(r["ranked"]) < 2:
        return ""
    gap = int(r["ranked"][0]["votes"] or 0) - int(r["ranked"][1]["votes"] or 0)
    return f" — {surname(r['leader']['choiceName'])} leads by {fmt(gap)} vote{'' if gap == 1 else 's'}"

def polls_line(r):
    return f" ({r['closed']} of {r['polls']} polls)" if r["closed"] else ""

# ---------------------------------------------------------------- state
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"races": {}, "milestones": [], "fails": 0, "down_since": None, "down_alerted": False,
                "last_stamp": None}

def save_state(s):
    tmp = STATE_FILE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE_FILE)

# ---------------------------------------------------------------- the rules
def evaluate(data, state):
    """Return a list of Slack lines for this cycle and update state in place."""
    g = data.get("statistics") or {}
    races = flatten(data)
    firsts, leads, wins, complete = [], [], [], []

    for r in races:
        if r["acclaimed"]:
            continue
        s = state["races"].setdefault(r["id"], {"first": False, "leader": None, "pending": None,
                                                "pending_n": 0, "winners": [], "complete": False})
        if not r["total"]:
            continue
        trustee = r["name"].startswith("Trustee")
        if trustee and TRUSTEES == "none":
            continue
        quiet = trustee and TRUSTEES != "all"   # skip first-results and lead-change lines
        lead_id = r["leader"]["id"] if r["leader"] else None

        # first results
        if not s["first"]:
            s["first"] = True
            s["leader"] = lead_id
            if quiet:
                pass
            elif r["tied"]:
                firsts.append(f"*{r['title']}*: tied — {standing(r)}{polls_line(r)}")
            else:
                firsts.append(f"*{r['title']}*: {display_name(r['leader']['choiceName'])} leads — "
                              f"{standing(r)}{polls_line(r)}{margin(r)}")
        # lead changes (ties and flickers never alert)
        elif lead_id and lead_id != s["leader"]:
            if s["pending"] == lead_id:
                s["pending_n"] += 1
            else:
                s["pending"], s["pending_n"] = lead_id, 1
            if s["pending_n"] >= LEAD_CONFIRM and quiet:
                s["leader"], s["pending"], s["pending_n"] = lead_id, None, 0
            elif s["pending_n"] >= LEAD_CONFIRM:
                prev = next((x for x in r["ranked"] if x["id"] == s["leader"]), None)
                was = f", passing {display_name(prev['choiceName'])}" if prev else ""
                leads.append(f"*{r['title']}*: {display_name(r['leader']['choiceName'])} takes the lead{was} — "
                             f"{standing(r)}{polls_line(r)}{margin(r)}")
                s["leader"], s["pending"], s["pending_n"] = lead_id, None, 0
        else:
            s["pending"], s["pending_n"] = None, 0

        # winner flagged by the city
        new_w = [w for w in r["winners"] if w["id"] not in s["winners"]]
        if new_w:
            s["winners"] += [w["id"] for w in new_w]
            names = " and ".join(display_name(w["choiceName"]) for w in new_w)
            wv = int(new_w[0]["votes"] or 0)
            wins.append(f"*{r['title']}*: {names} wins — {fmt(wv)} votes, {pct(wv, r['total'])} "
                        f"{polls_line(r)}".strip())

        # all polls in
        if r["polls"] and r["closed"] >= r["polls"] and not s["complete"]:
            s["complete"] = True
            if not new_w:
                who = "Tied" if r["tied"] else f"{display_name(r['leader']['choiceName'])} leads"
                complete.append(f"*{r['title']}*: all {r['polls']} polls in. {who} — {standing(r)}{margin(r)}")

    lines = []
    if wins:     lines += [":white_check_mark: *Unofficial winners (flagged by the city)*"] + wins + [""]
    if leads:    lines += [":arrows_counterclockwise: *Lead changes*"] + leads + [""]
    if complete: lines += [":ballot_box_with_ballot: *All polls reporting*"] + complete + [""]
    if firsts:   lines += [":new: *First results*"] + firsts + [""]

    # city-wide milestones, with a summary
    gp, gc = int(g.get("globPolls") or 0), int(g.get("globClosedPolls") or 0)
    if gp:
        done = gc / gp * 100
        hit = [m for m in MILESTONES if done >= m and m not in state["milestones"]]
        if hit:
            state["milestones"] += hit
            lines += summary(races, g, f"{max(hit)}% of polls reporting ({gc} of {gp})")
    return lines

def summary(races, g, heading):
    out = [f":bar_chart: *{heading}*"]
    turnout = g.get("globTurnout")
    if turnout and turnout != "0%":
        out[0] += f" · turnout {turnout}"
    for r in races:
        if r["acclaimed"] or not r["total"]:
            continue
        if r["name"].startswith("Mayor"):
            out.append(f"*Mayor*: {standing(r, 4)}{polls_line(r)}{margin(r)}")
        elif r["name"].startswith("Councillor"):
            who = "tied" if r["tied"] else display_name(r["leader"]["choiceName"]) + " " + pct(int(r["leader"]["votes"]), r["total"])
            out.append(f"{r['title']}: {who}{polls_line(r)}")
    return out + [""]

# ---------------------------------------------------------------- slack
def post(text, dry):
    if LABEL:
        text = f"*[{LABEL}]* " + text
    if dry:
        print("\n----- SLACK -----\n" + text + "\n-----------------", flush=True)
        return True
    if SLACK_TOKEN and SLACK_CHANNEL:
        url, body = "https://slack.com/api/chat.postMessage", {
            "channel": SLACK_CHANNEL, "text": text, "unfurl_links": False, "mrkdwn": True}
        headers = {"Authorization": f"Bearer {SLACK_TOKEN}"}
    elif SLACK_WEBHOOK:
        url, body, headers = SLACK_WEBHOOK, {"text": text}, {}
    else:
        log("No Slack credentials set — use --dry-run or fill in .env")
        return False
    headers["Content-Type"] = "application/json; charset=utf-8"
    for attempt in (1, 2, 3):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = r.read().decode()
            if SLACK_TOKEN and not json.loads(resp).get("ok"):
                raise RuntimeError(resp)
            return True
        except Exception as e:
            log(f"Slack post failed (attempt {attempt}): {e}")
            time.sleep(2 * attempt)
    return False

# ---------------------------------------------------------------- simulator
class Simulator:
    """Plays a whole election night from a snapshot: polls close over ~20 ticks,
    leads swap now and then, winners flagged at the end."""
    def __init__(self, path, ticks=20, seed=7):
        self.base = json.load(open(path, encoding="utf-8-sig"))
        self.ticks, self.tick = ticks, 0
        self.rng = random.Random(seed)
        self.strength = {}

    def __call__(self, _url):
        self.tick += 1
        t = min(self.tick / self.ticks, 1.0)
        d = copy.deepcopy(self.base)
        for block in d["areaResults"]:
            for area in block.values():
                st = area["statistics"]
                st["closedPolls"] = round(st["polls"] * t)
                for c in area["contestResults"]:
                    tot = 0
                    for i, ch in enumerate(c["choiceResults"]):
                        k = c["id"] + "-" + ch["id"]
                        if k not in self.strength:
                            self.strength[k] = self.rng.uniform(200, 3000) / (i % 3 + 1)
                        # early noise so leads can flip, settles by the end
                        wobble = 1 + self.rng.uniform(-0.6, 0.6) * (1 - t)
                        ch["votes"] = int(self.strength[k] * t * wobble)
                        tot += ch["votes"]
                    if t >= 1 and tot:
                        mx = max(x["votes"] for x in c["choiceResults"])
                        for x in c["choiceResults"]:
                            x["isWinner"] = int(x["votes"] == mx)
        d["statistics"]["globClosedPolls"] = round(d["statistics"]["globPolls"] * t)
        d["statistics"]["timeStamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return d, False

# ---------------------------------------------------------------- main loop
RUN = True
def stop(*_):
    global RUN
    RUN = False
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

def cycle(getter, state, dry):
    try:
        data, stale = getter(FEED_URL)
        if stale:
            raise RuntimeError("Worker is serving a stale copy — city feed not responding")
    except Exception as e:
        state["fails"] += 1
        state["down_since"] = state["down_since"] or cp_time()
        log(f"feed error #{state['fails']}: {e}")
        if state["fails"] >= OUTAGE_AFTER and not state["down_alerted"]:
            if post(f":warning: Results feed not responding since {state['down_since']} ({e}). "
                    "Still retrying.", dry):
                state["down_alerted"] = True
        return
    if state["down_alerted"]:
        post(f":large_green_circle: Results feed is back as of {cp_time()}.", dry)
    state["fails"], state["down_since"], state["down_alerted"] = 0, None, False

    g = data.get("statistics") or {}
    stamp = str(hash(json.dumps(data.get("areaResults"), sort_keys=True)))  # stamp alone isn't reliable
    if stamp != state["last_stamp"]:
        log(f"feed changed (city stamp {g.get('timeStamp')}) · {g.get('globClosedPolls')} of {g.get('globPolls')} polls")
        if ARCHIVE_DIR:
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            name = datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"
            json.dump(data, open(os.path.join(ARCHIVE_DIR, name), "w"))
    state["last_stamp"] = stamp

    lines = evaluate(data, state)
    if lines:
        post("\n".join(lines).strip(), dry)

def main():
    global STATE_FILE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of posting")
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    ap.add_argument("--hello", action="store_true", help="post a Slack test message and exit")
    ap.add_argument("--reset", action="store_true", help="clear alert history before starting")
    ap.add_argument("--simulate", metavar="JSON", help="fake a full night from a snapshot file")
    ap.add_argument("--interval", type=float, default=None, help="seconds between polls")
    a = ap.parse_args()

    if a.hello:
        ok = post(f":wave: Election watcher connected at {cp_time()}. Watching {FEED_URL}", a.dry_run)
        sys.exit(0 if ok else 1)

    if a.reset:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        log("alert history cleared")
        sys.exit(0)

    if a.simulate:  # never mix simulated alerts into the real history
        STATE_FILE = os.path.join(HERE, "election_state.sim.json")
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

    getter = Simulator(a.simulate) if a.simulate else fetch_feed
    interval = a.interval or POLL_SECONDS
    state = load_state()
    log(f"watching {'SIMULATION of ' + a.simulate if a.simulate else FEED_URL} every {interval} s"
        + (" (dry run)" if a.dry_run else ""))

    while RUN:
        started = time.time()
        cycle(getter, state, a.dry_run)
        save_state(state)
        if a.once:
            break
        if a.simulate and getter.tick > getter.ticks + 1:
            log("simulation finished")
            break
        while RUN and time.time() - started < interval:
            time.sleep(0.5)
    log("stopped")

if __name__ == "__main__":
    main()
