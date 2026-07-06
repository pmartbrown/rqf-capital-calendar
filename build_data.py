#!/usr/bin/env python3
"""RQF Capital Planning - rebuild data.json from HubSpot.
Runs in GitHub Actions on a schedule. Needs env HUBSPOT_TOKEN (Private App token,
scope crm.objects.deals.read). Implements the rqf-capital-planning skill logic."""
import os, json, sys, datetime, urllib.request

TOKEN = os.environ.get("HUBSPOT_TOKEN")
if not TOKEN:
    sys.exit("HUBSPOT_TOKEN not set")
POOL, FLOOR = 1500000, 50000
TODAY = datetime.date.today()
FUND_END = "2027-12-31"
GAP_CUTOFF = datetime.date(2026, 3, 1)

PIPES = {"667469957": "EMD", "1835271887": "POF", "669996899": "Stack",
         "669884544": "DC", "670073508": "Echo", "default": "GAP", "656512639": "GAP"}
TRANSACTIONAL = ["667469957", "1835271887", "669996899", "669884544", "670073508"]
CONF = {"979383438", "979383439", "982752174", "982752175", "982635877", "982635878",
        "contractsent", "962625959", "964682409", "964682411", "964682412", "964682413", "964682414"}
PROB = {"presentationscheduled", "979383437", "3303857904", "982752173", "982635876"}
DEAD = {"979383441", "1014419716", "982752176", "982635879", "964682410", "964682415",
        "979383440", "979383442", "983990951", "closedlost", "982752177", "982752178",
        "982635880", "982635881"}
PROPS = ["dealname", "pipeline", "dealstage", "dealtype", "wired_amount", "requested_amount",
         "signed___funded_date", "return_wire_date", "return_wire_amount",
         "due_diligence_period_expiry_date", "close_of_escrow_date", "closedate"]


def search(filter_groups):
    url = "https://api.hubapi.com/crm/v3/objects/deals/search"
    out, after = [], None
    while True:
        body = {"filterGroups": filter_groups, "properties": PROPS, "limit": 100}
        if after:
            body["after"] = after
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + TOKEN,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            j = json.loads(r.read())
        out += [d["properties"] for d in j.get("results", [])]
        after = j.get("paging", {}).get("next", {}).get("after")
        if not after:
            return out


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def pd(s):
    if not s:
        return None
    return datetime.date.fromisoformat(s[:10])


def d1(dt, n):
    return (dt + datetime.timedelta(days=n)).isoformat()


deals, seen = [], set()

# ---- STEP 1: DEPLOYED (wired>0, no return amount; GAP only if Signed&Funded after 2026-03-01) ----
funded = search([{"filters": [{"propertyName": "wired_amount", "operator": "GT", "value": "0"}]}])
for p in funded:
    pipe = p.get("pipeline")
    if pipe not in PIPES:
        continue
    if num(p.get("return_wire_amount")):        # money came back -> not out
        continue
    signed = pd(p.get("signed___funded_date"))
    if PIPES[pipe] == "GAP" and (not signed or signed <= GAP_CUTOFF):
        continue                                 # old GAP = different pool
    wired = num(p.get("wired_amount"))
    amt = wired if wired > 0 else num(p.get("requested_amount"))
    if amt <= 0:
        continue
    stage = p.get("dealstage")
    err, note = False, ""
    if stage in DEAD:
        err, note = True, "HubSpot: stage says returned/fell-through, no return amount"
    elif stage in PROB:
        err, note = True, "HubSpot: wired but stage still 02 Approved"
    if not num(p.get("wired_amount")):
        err, note = True, "HubSpot: funded but Wired Amount blank"
    start = signed or TODAY
    end = pd(p.get("return_wire_date")) or datetime.date.fromisoformat(FUND_END)
    if start > end:
        start = end
    name = (p.get("dealname") or "Deal").strip()
    seen.add(name)
    deals.append({"n": name, "t": PIPES[pipe], "l": "confirmed", "f": True, "a": round(amt, 2),
                  "s": start.isoformat(), "e": end.isoformat(), "c": start.isoformat(),
                  "err": err, "note": note})

# ---- STEP 2: FORWARD pipeline (open, transactional, not already deployed) ----
fwd = search([{"filters": [{"propertyName": "pipeline", "operator": "IN", "values": TRANSACTIONAL},
                           {"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"}]}])
for p in fwd:
    name = (p.get("dealname") or "Deal").strip()
    if name in seen:
        continue
    stage = p.get("dealstage")
    if stage in DEAD:
        continue
    amt = num(p.get("wired_amount")) or num(p.get("requested_amount"))
    if amt <= 0:
        continue
    pipe = p.get("pipeline")
    t = PIPES.get(pipe, "EMD")
    coe = pd(p.get("close_of_escrow_date")) or pd(p.get("closedate"))
    if stage in PROB or (stage in CONF and not num(p.get("wired_amount"))):
        layer = "probable"
    else:
        layer = "onhold"
    if t == "EMD":
        start = pd(p.get("signed___funded_date")) or (TODAY + datetime.timedelta(days=1))
        end = pd(p.get("due_diligence_period_expiry_date")) or coe
    elif t == "POF":
        start = end = pd(p.get("closedate"))
    else:
        start = end = coe
    if not start or not end:
        continue
    if start > end:
        start = end
    land = coe or end
    deals.append({"n": name, "t": t, "l": layer, "f": False, "a": round(amt, 2),
                  "s": d1(start, -1), "e": d1(end, 1), "c": land.isoformat(),
                  "err": False, "note": ""})

out = {"pool": POOL, "floor": FLOOR,
       "generated": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-6))).isoformat(timespec="seconds"),
       "source": "HubSpot RQF pipelines: EMD, POF, Stack, Double Close, Echo, GAP",
       "notes": "Deployed(confirmed,f=true)=Wired Amount out, no Return Wire Amount logged (GAP only if Signed&Funded after 2026-03-01); out from Signed&Funded until returned. err=true: HubSpot record inconsistent but still counted until corrected. probable=02-approved + committed-not-yet-wired. onhold=00/01 pipeline watch (not in math).",
       "deals": deals}
with open("data.json", "w") as f:
    json.dump(out, f, indent=2)
dep = sum(d["a"] for d in deals if d["l"] == "confirmed")
print("deals={} deployed=${:,.0f} available=${:,.0f}".format(len(deals), dep, POOL - dep))
