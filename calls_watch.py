#!/usr/bin/env python3
"""
Election-night projection watcher. Companion to election_watch.py (same folder, same .env).

Reads the same feed every 30 seconds and, for mayor and council races, posts to Slack when:
  - PROJECTED: half or more of the race's polls are in and the runner-up would need
    at least 70% of the estimated remaining vote to pass the leader
    (80% until the race's advance-vote tabulators have all reported)
  - CLINCHED:  the lead is bigger than all the votes estimated to be left
  - WITHDRAWN: a projected leader's position weakens (runner-up needs under 60%) or the lead changes
  - FINAL:     all of a race's polls are in (unofficial count; flags margins under 1%)

Estimated votes left = average votes per reported poll x polls not yet reported.
These are projections for the desk to confirm, not official results.

Usage:
  python3 calls_watch.py                         # run for real
  python3 calls_watch.py --dry-run               # print instead of posting
  python3 calls_watch.py --simulate arearesults.json --interval 0.2 --dry-run
  python3 calls_watch.py --reset                 # clear projection history and exit

Optional .env settings:
  CALLS_CHANNEL=C0...        post projections to a different channel (default: SLACK_CHANNEL)
  PROJECT_MIN_REPORTED=0.5   share of polls in before projecting
  PROJECT_NEED=0.70          runner-up's required share to project
  PROJECT_NEED_NO_ADVANCE=0.80
  PROJECT_WITHDRAW=0.60
"""
import argparse, json, os, sys, time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import election_watch as ew   # loads .env, gives us fetch/flatten/post/formatting

MIN_REPORTED   = float(os.environ.get("PROJECT_MIN_REPORTED", "0.5"))
NEED           = float(os.environ.get("PROJECT_NEED", "0.70"))
NEED_NO_ADV    = float(os.environ.get("PROJECT_NEED_NO_ADVANCE", "0.80"))
WITHDRAW       = float(os.environ.get("PROJECT_WITHDRAW", "0.60"))
STATE_FILE     = os.path.join(HERE, "calls_state.json")
if os.environ.get("CALLS_CHANNEL"):
    ew.SLACK_CHANNEL = os.environ["CALLS_CHANNEL"]

def advance_status(data):
    """{area contest id: True if every advance-vote tabulator in that area has reported}"""
    out = {}
    for block in data.get("areaResults") or []:
        for area in block.values():
            st = area.get("statistics") or {}
            adv = [g for g in st.get("countinggroups") or [] if "advance" in str(g.get("countingGroupName", "")).lower()]
            done = bool(adv) and all(int(g.get("tabulators") or 0) > 0 and
                                     int(g.get("closedTabulators") or 0) >= int(g.get("tabulators") or 0) for g in adv)
            for c in area.get("contestResults") or []:
                out[str(c.get("id"))] = done
    return out

def assess(r, adv_done):
    """Numbers for one race, or None if it can't be assessed yet."""
    if r["acclaimed"] or len(r["ranked"]) < 2 or not r["total"] or not r["closed"] or not r["polls"]:
        return None
    first, second = r["ranked"][0], r["ranked"][1]
    v1, v2 = int(first["votes"] or 0), int(second["votes"] or 0)
    lead = v1 - v2
    reported = r["closed"] / r["polls"]
    left = round(r["total"] / r["closed"] * (r["polls"] - r["closed"]))
    need = 1.0 if left <= 0 else (left + lead) / (2 * left)
    return {"first": first, "second": second, "lead": lead, "left": left, "need": need,
            "reported": reported, "adv": adv_done, "complete": r["closed"] >= r["polls"]}

def evaluate(data, state):
    adv = advance_status(data)
    lines = []
    for r in ew.flatten(data):
        if not (r["name"].startswith("Mayor") or r["name"].startswith("Councillor")):
            continue
        a = assess(r, adv.get(r["id"], False))
        s = state.setdefault(r["id"], {"status": None, "who": None})
        if not a or a["lead"] <= 0:
            if s["status"] == "projected" and a and a["lead"] <= 0:
                lines.append(f":warning: *{r['title']}*: projection withdrawn. The race is now tied.")
                s.update(status=None, who=None)
            continue
        lead_id, name1, name2 = a["first"]["id"], ew.display_name(a["first"]["choiceName"]), ew.surname(a["second"]["choiceName"])
        polls = f"{r['closed']} of {r['polls']} polls in"
        advtxt = "advance votes in" if a["adv"] else "advance votes not all in"

        # leader changed under an existing call
        if s["status"] in ("projected", "clinched", "final") and s["who"] != lead_id:
            lines.append(f":warning: *{r['title']}*: projection withdrawn. {name1} now leads by "
                         f"{ew.fmt(a['lead'])} votes ({polls}).")
            s.update(status=None, who=None)

        close = a["lead"] < 0.01 * r["total"]
        closenote = f" Margin is under 1% of {ew.fmt(r['total'])} votes cast." if close else ""
        if a["complete"]:
            if s["status"] != "final":
                lines.append(f":ballot_box_with_ballot: *{r['title']}*: all {r['polls']} polls in. {name1} finishes "
                             f"{ew.fmt(a['lead'])} votes ahead of {name2} on the unofficial count.{closenote}")
                s.update(status="final", who=lead_id)
            continue
        clinched = a["lead"] > a["left"]
        threshold = NEED if a["adv"] else NEED_NO_ADV
        projected = a["reported"] >= MIN_REPORTED and a["need"] >= threshold

        if clinched and s["status"] != "clinched":
            lines.append(f":lock: *{r['title']}*: {name1} has clinched. Lead of {ew.fmt(a['lead'])} votes is larger "
                         f"than the estimated {ew.fmt(a['left'])} left ({polls}).{closenote}")
            s.update(status="clinched", who=lead_id)
        elif projected and s["status"] is None:
            lines.append(f":crystal_ball: *{r['title']}*: {name1} projected to win. Leads {name2} by "
                         f"{ew.fmt(a['lead'])} votes; {name2} would need {a['need']*100:.0f}% of the estimated "
                         f"{ew.fmt(a['left'])} votes left ({polls}, {advtxt}).")
            s.update(status="projected", who=lead_id)
        elif s["status"] == "projected" and a["need"] < WITHDRAW:
            lines.append(f":warning: *{r['title']}*: projection withdrawn. {name2} now needs only "
                         f"{a['need']*100:.0f}% of the estimated {ew.fmt(a['left'])} votes left ({polls}).")
            s.update(status=None, who=None)
    if lines:
        lines.insert(0, "_Projections for the desk to confirm. Not official results._")
    return lines

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--simulate", metavar="JSON")
    ap.add_argument("--interval", type=float, default=None)
    a = ap.parse_args()
    global STATE_FILE
    if a.reset:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        ew.log("projection history cleared")
        sys.exit(0)
    if a.simulate:
        STATE_FILE = os.path.join(HERE, "calls_state.sim.json")
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    getter = ew.Simulator(a.simulate) if a.simulate else ew.fetch_feed
    try:
        state = json.load(open(STATE_FILE))
    except Exception:
        state = {}
    interval = a.interval or ew.POLL_SECONDS
    ew.log(f"projections: watching {'SIMULATION' if a.simulate else ew.FEED_URL} every {interval} s"
           + (" (dry run)" if a.dry_run else ""))
    while ew.RUN:
        started = time.time()
        try:
            data, _stale = getter(ew.FEED_URL)
            lines = evaluate(data, state)
            if lines:
                ew.post("\n".join(lines), a.dry_run)
            tmp = STATE_FILE + ".tmp"
            json.dump(state, open(tmp, "w"))
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            ew.log(f"feed error: {e}")   # election_watch.py handles outage alerts
        if a.once or (a.simulate and getter.tick > getter.ticks + 1):
            break
        while ew.RUN and time.time() - started < interval:
            time.sleep(0.5)
    ew.log("stopped")

if __name__ == "__main__":
    main()
