"""ClinicalTrials.gov (API v2) — the pipeline and catalyst calendar for
pre-revenue names, where DCF and multiples do not apply.

Public API, no key, no auth: https://clinicaltrials.gov/api/v2/studies

What this is for: a clinical-stage company IS its trial pipeline. The questions
worth answering are what is in the clinic, at what phase, in what indication,
and WHEN the next readout lands. Everything here serves that last one.

Caveat that matters: sponsors self-report, and primary completion dates are
mostly ESTIMATED. A date here is what the company last told the registry, not a
promise. Slippage is the norm, and the registry is often updated late. Treat a
readout date as a planning aid, never as a tradeable certainty.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date, datetime

API = "https://clinicaltrials.gov/api/v2/studies"
TIMEOUT = 30

ACTIVE_STATUSES = ["RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION",
                   "NOT_YET_RECRUITING"]
DONE_STATUSES = ["COMPLETED", "TERMINATED", "SUSPENDED", "WITHDRAWN"]

_FIELDS = "|".join([
    "protocolSection.identificationModule.nctId",
    "protocolSection.identificationModule.briefTitle",
    "protocolSection.statusModule.overallStatus",
    "protocolSection.statusModule.startDateStruct",
    "protocolSection.statusModule.primaryCompletionDateStruct",
    "protocolSection.statusModule.completionDateStruct",
    "protocolSection.statusModule.lastUpdatePostDateStruct",
    "protocolSection.statusModule.whyStopped",
    "protocolSection.sponsorCollaboratorsModule.leadSponsor",
    "protocolSection.conditionsModule.conditions",
    "protocolSection.designModule.phases",
    "protocolSection.designModule.studyType",
    "protocolSection.designModule.enrollmentInfo",
    "protocolSection.armsInterventionsModule.interventions",
])

_PHASE_ORDER = {"EARLY_PHASE1": 0, "PHASE1": 1, "PHASE2": 2, "PHASE3": 3,
                "PHASE4": 4, "NA": 5}


def _get(params: dict) -> dict:
    url = API + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def _flatten(study: dict) -> dict:
    ps = study.get("protocolSection", {})
    ident = ps.get("identificationModule", {})
    status = ps.get("statusModule", {})
    design = ps.get("designModule", {})
    interventions = [
        {"type": i.get("type"), "name": i.get("name")}
        for i in ps.get("armsInterventionsModule", {}).get("interventions", [])
        if i.get("type") in (None, "DRUG", "BIOLOGICAL", "GENETIC", "COMBINATION_PRODUCT")
    ]
    phases = design.get("phases") or ["NA"]
    nct = ident.get("nctId")
    return {
        "nct_id": nct,
        "title": ident.get("briefTitle"),
        "sponsor": ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name"),
        "status": status.get("overallStatus"),
        "why_stopped": status.get("whyStopped"),
        "phase": "/".join(p.replace("PHASE", "Ph") for p in phases),
        "phase_rank": min(_PHASE_ORDER.get(p, 5) for p in phases),
        "study_type": design.get("studyType"),
        "conditions": ps.get("conditionsModule", {}).get("conditions", []),
        "interventions": interventions,
        "enrollment": (design.get("enrollmentInfo") or {}).get("count"),
        "enrollment_type": (design.get("enrollmentInfo") or {}).get("type"),
        "start": (status.get("startDateStruct") or {}).get("date"),
        "primary_completion": (status.get("primaryCompletionDateStruct") or {}).get("date"),
        "primary_completion_type": (status.get("primaryCompletionDateStruct") or {}).get("type"),
        "completion": (status.get("completionDateStruct") or {}).get("date"),
        "last_update": (status.get("lastUpdatePostDateStruct") or {}).get("date"),
        "url": f"https://clinicaltrials.gov/study/{nct}" if nct else None,
    }


def search(sponsor: str = "", term: str = "", condition: str = "",
           intervention: str = "", status: list[str] | None = None,
           interventional_only: bool = True, limit: int = 25) -> list[dict]:
    """Raw search. At least one of sponsor/term/condition/intervention."""
    params = {"pageSize": min(int(limit), 100), "fields": _FIELDS,
              "sort": "LastUpdatePostDate:desc"}
    if sponsor:
        params["query.spons"] = sponsor
    if term:
        params["query.term"] = term
    if condition:
        params["query.cond"] = condition
    if intervention:
        params["query.intr"] = intervention
    if status:
        params["filter.overallStatus"] = ",".join(status)
    if interventional_only:
        params["filter.advanced"] = "AREA[StudyType]INTERVENTIONAL"
    if not any(k.startswith("query.") for k in params):
        raise ValueError("give a sponsor, term, condition or intervention")
    return [_flatten(s) for s in _get(params).get("studies", [])]


def _months_until(d: str | None) -> float | None:
    if not d:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            when = datetime.strptime(d, fmt).date()
            break
        except ValueError:
            continue
    else:
        return None
    return round((when - date.today()).days / 30.44, 1)


def pipeline(sponsor: str, limit: int = 60) -> dict:
    """Everything a sponsor has in the clinic, deepest phase first.

    Deepest phase first because that is where the value is: a Phase 3 readout
    moves a clinical-stage company far more than anything earlier.
    """
    studies = search(sponsor=sponsor, status=ACTIVE_STATUSES, limit=limit)
    for s in studies:
        s["months_to_primary"] = _months_until(s["primary_completion"])
    studies.sort(key=lambda s: (-s["phase_rank"],
                                s["primary_completion"] or "9999"))
    by_phase: dict[str, int] = {}
    for s in studies:
        by_phase[s["phase"]] = by_phase.get(s["phase"], 0) + 1
    return {
        "sponsor_query": sponsor,
        "active_trials": len(studies),
        "by_phase": by_phase,
        "deepest_phase": studies[0]["phase"] if studies else None,
        "trials": studies,
        "caveat": "Dates are sponsor-reported and mostly ESTIMATED. Slippage is normal.",
    }


def readouts(sponsor: str, months: int = 18, limit: int = 60) -> dict:
    """The catalyst calendar: primary completions inside the window.

    This is the substitute for a valuation section on a pre-revenue name. Each
    row is a date the stock can gap on.
    """
    pipe = pipeline(sponsor, limit=limit)
    rows = [s for s in pipe["trials"]
            if s["months_to_primary"] is not None
            and -1 <= s["months_to_primary"] <= months]
    rows.sort(key=lambda s: s["primary_completion"] or "9999")
    return {
        "sponsor_query": sponsor,
        "window_months": months,
        "n_catalysts": len(rows),
        "next": rows[0] if rows else None,
        "catalysts": rows,
        "caveat": pipe["caveat"],
    }


def history(sponsor: str, limit: int = 60) -> dict:
    """Finished and failed trials — the sponsor's actual track record.

    `why_stopped` on a TERMINATED trial is one of the highest-value fields on
    the whole registry and nobody reads it.
    """
    studies = search(sponsor=sponsor, status=DONE_STATUSES, limit=limit)
    studies.sort(key=lambda s: s["primary_completion"] or "", reverse=True)
    bad = [s for s in studies if s["status"] in ("TERMINATED", "SUSPENDED", "WITHDRAWN")]
    return {
        "sponsor_query": sponsor,
        "n_finished": len(studies),
        "n_stopped_early": len(bad),
        "stopped_early": [{"nct_id": s["nct_id"], "phase": s["phase"],
                           "status": s["status"], "why_stopped": s["why_stopped"],
                           "title": s["title"]} for s in bad],
        "trials": studies,
    }


def study(nct_id: str) -> dict:
    res = _get({"query.id": nct_id, "pageSize": 1, "fields": _FIELDS})
    studies = res.get("studies", [])
    if not studies:
        return {"error": f"no study {nct_id}"}
    out = _flatten(studies[0])
    out["months_to_primary"] = _months_until(out["primary_completion"])
    return out
