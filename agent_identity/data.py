"""In-memory demo data standing in for the downstream systems (HR, CRM, Ops)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

# ---- end users (directory) and the scopes their account is entitled to --------
USERS: dict[str, dict[str, object]] = {
    "carol": {
        "name": "Carol (employee)",
        "scopes": ["courses:read", "courses:enroll", "crm:read", "ops:read"],
    },
    "dave": {
        "name": "Dave (SRE)",
        "scopes": ["courses:read", "ops:read", "ops:restart"],
    },
}

# ---- HR / learning ---------------------------------------------------------------
COURSES: dict[str, dict[str, object]] = {
    "PY101": {"title": "Python Fundamentals", "cost": 120},
    "AI201": {"title": "Building AI Agents", "cost": 350},
    "SEC301": {"title": "Zero-Trust Security Bootcamp", "cost": 1800},
}
PRIVILEGED_COST_THRESHOLD = 500  # enrolments above this need a privileged, short-lived token

# ---- CRM ---------------------------------------------------------------------------
ACCOUNTS: list[dict[str, object]] = [
    {"id": "A-1", "name": "Acme Corp", "region": "EMEA", "tier": "gold", "revenue": 1_250_000},
    {"id": "A-2", "name": "Globex", "region": "AMER", "tier": "silver", "revenue": 830_000},
    {"id": "A-3", "name": "Initech", "region": "EMEA", "tier": "bronze", "revenue": 215_000},
    {"id": "A-4", "name": "Umbrella", "region": "APAC", "tier": "gold", "revenue": 970_000},
]

# ---- Ops ---------------------------------------------------------------------------
SERVICES: dict[str, str] = {"payments": "running", "search": "running", "reports": "degraded"}


@dataclass
class Store:
    """Mutable state (enrolments, service status) with a lock for thread safety."""

    enrollments: dict[str, list[str]] = field(default_factory=dict)
    services: dict[str, str] = field(default_factory=lambda: dict(SERVICES))
    accounts: list[dict[str, object]] = field(default_factory=lambda: [dict(a) for a in ACCOUNTS])
    lock: threading.Lock = field(default_factory=threading.Lock)
