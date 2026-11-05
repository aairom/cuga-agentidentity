"""Identity backends.

* :class:`MockVerifyBackend` - a self-contained, in-process stand-in for the
  IBM Verify Agent Identity services (Agent Registry, DCR, token endpoint,
  RFC 8693 token exchange). It signs real RS256 JWTs, so the same validation
  code works against the mock and against a real tenant.
* :class:`HttpVerifyBackend` - thin REST client for a real IBM Verify tenant
  (SCIM ``/v2.0/Agents``, ``/oauth2/register``, token endpoint). It follows the
  public IBM documentation but has NOT been exercised against a live tenant in
  this repository - treat it as a reference implementation.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from typing import Any, Protocol

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .errors import (
    AgentNotActive,
    AgentNotFound,
    BackendError,
    DelegationError,
    InsufficientScope,
    TokenValidationError,
)
from .logging_setup import get_logger
from .models import AgentRecord, AgentStatus, ClientCredentials

log = get_logger("backend")

ALGORITHM = "RS256"
PRIVILEGED_TTL_SECONDS = 60  # privileged tokens are short-lived and not renewable


class IdentityBackend(Protocol):
    """Operations the showcase needs from an Agent Identity provider."""

    issuer: str

    def create_agent(self, name: str, description: str, owner: str, allowed_scopes: frozenset[str]) -> AgentRecord: ...
    def get_agent(self, agent_id: str) -> AgentRecord: ...
    def find_agent(self, name: str) -> AgentRecord: ...
    def list_agents(self) -> list[AgentRecord]: ...
    def set_status(self, agent_id: str, status: AgentStatus, actor: str) -> AgentRecord: ...
    def register_client(self, agent_id: str) -> ClientCredentials: ...
    def client_credentials_token(self, client_id: str, client_secret: str, scope: list[str], audience: str) -> str: ...
    def exchange_token(
        self,
        subject_token: str,
        actor_token: str,
        scope: list[str],
        audience: str,
        authorization_details: list[dict[str, Any]] | None = None,
    ) -> str: ...
    def verification_key(self) -> Any: ...


class MockVerifyBackend:
    """In-process mock of IBM Verify Agent Identity (thread-safe)."""

    def __init__(self, issuer: str = "https://mock-verify.local", token_ttl: int = 300) -> None:
        self.issuer = issuer
        self.token_ttl = token_ttl
        self._lock = threading.RLock()
        self._agents: dict[str, AgentRecord] = {}
        self._clients: dict[str, tuple[str, str]] = {}  # client_id -> (secret, agent_id)
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._private_pem = self._key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        self._public_pem = self._key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    # ---- registry -------------------------------------------------------------
    def create_agent(self, name: str, description: str, owner: str, allowed_scopes: frozenset[str]) -> AgentRecord:
        with self._lock:
            if any(a.name == name for a in self._agents.values()):
                raise BackendError(f"Agent name already exists: {name}")
            rec = AgentRecord(
                agent_id=f"agt-{uuid.uuid4().hex[:12]}",
                name=name,
                description=description,
                owner=owner,
                allowed_scopes=allowed_scopes,
            )
            self._agents[rec.agent_id] = rec
            return rec

    def get_agent(self, agent_id: str) -> AgentRecord:
        with self._lock:
            try:
                return self._agents[agent_id]
            except KeyError:
                raise AgentNotFound(agent_id) from None

    def find_agent(self, name: str) -> AgentRecord:
        with self._lock:
            for rec in self._agents.values():
                if rec.name == name:
                    return rec
        raise AgentNotFound(name)

    def list_agents(self) -> list[AgentRecord]:
        with self._lock:
            return list(self._agents.values())

    def set_status(self, agent_id: str, status: AgentStatus, actor: str) -> AgentRecord:
        with self._lock:
            rec = self.get_agent(agent_id)
            rec.status = status
            if status is AgentStatus.ACTIVE and rec.approved_by is None:
                rec.approved_by = actor
            return rec

    # ---- DCR: only ACTIVE agents may register an OAuth client -----------------
    def register_client(self, agent_id: str) -> ClientCredentials:
        with self._lock:
            rec = self.get_agent(agent_id)
            if rec.status is not AgentStatus.ACTIVE:
                # Real Verify answers HTTP 400 in this case.
                raise AgentNotActive(f"Cannot register client: agent {rec.name} is {rec.status.value}")
            client_id = f"cl-{uuid.uuid4().hex[:10]}"
            secret = secrets.token_urlsafe(24)
            self._clients[client_id] = (secret, agent_id)
            rec.client_id = client_id
            return ClientCredentials(client_id=client_id, client_secret=secret, agent_id=agent_id)

    # ---- token endpoint -------------------------------------------------------
    def _sign(self, claims: dict[str, Any]) -> str:
        return jwt.encode(claims, self._private_pem, algorithm=ALGORITHM, headers={"kid": "mock-1"})

    def _base_claims(self, sub: str, scope: list[str], audience: str, ttl: int) -> dict[str, Any]:
        now = int(time.time())
        return {
            "iss": self.issuer,
            "sub": sub,
            "aud": audience,
            "iat": now,
            "exp": now + ttl,
            "jti": uuid.uuid4().hex,
            "scope": " ".join(scope),
        }

    def client_credentials_token(self, client_id: str, client_secret: str, scope: list[str], audience: str) -> str:
        """Client-credentials grant; carries a custom ``agent_id`` claim."""
        with self._lock:
            entry = self._clients.get(client_id)
            if not entry or not secrets.compare_digest(entry[0], client_secret):
                raise BackendError("invalid_client")
            rec = self.get_agent(entry[1])
            if rec.status is not AgentStatus.ACTIVE:
                raise AgentNotActive(f"Token refused: agent {rec.name} is {rec.status.value}")
            extra = set(scope) - rec.allowed_scopes
            if extra:
                raise InsufficientScope(f"Scopes not allowed for agent {rec.name}: {sorted(extra)}")
            claims = self._base_claims(client_id, scope, audience, self.token_ttl)
            claims["agent_id"] = rec.agent_id
            return self._sign(claims)

    def issue_user_token(self, user_id: str, scope: list[str], audience: str) -> str:
        """Mock end-user login (stands in for the OIDC login of a real user)."""
        return self._sign(self._base_claims(user_id, scope, audience, self.token_ttl))

    def exchange_token(
        self,
        subject_token: str,
        actor_token: str,
        scope: list[str],
        audience: str,
        authorization_details: list[dict[str, Any]] | None = None,
    ) -> str:
        """RFC 8693 token exchange -> ``sub=user`` and ``act.sub=agent``.

        The subject token may itself be delegated (agent-to-agent); the new actor is
        then nested around the previous ``act`` claim, preserving the full chain.
        """
        with self._lock:
            subject = self._decode(subject_token, audience=None)
            actor = self._decode(actor_token, audience=None)
            agent_id = actor.get("agent_id")
            if not agent_id:
                raise DelegationError("actor_token is not an agent token (no agent_id claim)")
            rec = self.get_agent(agent_id)
            if rec.status is not AgentStatus.ACTIVE:
                raise AgentNotActive(f"Exchange refused: agent {rec.name} is {rec.status.value}")

            # Effective scopes = requested ∩ user's scopes ∩ agent's allowed scopes.
            user_scopes = set(subject.get("scope", "").split())
            denied = set(scope) - (user_scopes & rec.allowed_scopes)
            if denied:
                raise InsufficientScope(
                    f"Scopes not granted by both user and agent {rec.name}: {sorted(denied)}"
                )
            privileged = bool(authorization_details)
            ttl = PRIVILEGED_TTL_SECONDS if privileged else self.token_ttl
            claims = self._base_claims(subject["sub"], scope, audience, ttl)
            act: dict[str, Any] = {"sub": rec.agent_id}
            if "act" in subject:  # nested delegation chain (A2A / transaction context)
                act["act"] = subject["act"]
            claims["act"] = act
            if privileged:
                claims["authorization_details"] = authorization_details
                claims["privileged"] = True  # short-lived, not renewable
            return self._sign(claims)

    # ---- keys / helpers -------------------------------------------------------
    def verification_key(self) -> bytes:
        return self._public_pem

    def _decode(self, token: str, audience: str | None) -> dict[str, Any]:
        try:
            return jwt.decode(
                token,
                self._public_pem,
                algorithms=[ALGORITHM],
                audience=audience,
                issuer=self.issuer,
                options={"verify_aud": audience is not None},
            )
        except jwt.PyJWTError as exc:
            raise TokenValidationError(str(exc)) from exc


class HttpVerifyBackend:
    """REST client for a real IBM Verify tenant (reference implementation)."""

    def __init__(self, tenant_url: str, admin_client_id: str, admin_client_secret: str, timeout: float = 15.0) -> None:
        if not tenant_url:
            raise BackendError("AI_VERIFY_TENANT_URL is required for the 'verify' backend")
        self.base = tenant_url.rstrip("/")
        self.issuer = f"{self.base}/oidc/endpoint/default"
        self._admin = (admin_client_id, admin_client_secret)
        self._http = httpx.Client(timeout=timeout)
        self._admin_token: tuple[str, float] | None = None

    def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            resp = self._http.request(method, f"{self.base}{path}", **kw)
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except httpx.HTTPStatusError as exc:
            raise BackendError(f"{method} {path} -> HTTP {exc.response.status_code}: {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"{method} {path} failed: {exc}") from exc

    def _admin_bearer(self) -> dict[str, str]:
        if not self._admin_token or self._admin_token[1] < time.time() + 30:
            data = self._call(
                "POST",
                "/v1.0/endpoint/default/token",
                data={"grant_type": "client_credentials", "client_id": self._admin[0], "client_secret": self._admin[1]},
            )
            self._admin_token = (data["access_token"], time.time() + int(data.get("expires_in", 300)))
        return {"Authorization": f"Bearer {self._admin_token[0]}"}

    @staticmethod
    def _to_record(d: dict[str, Any]) -> AgentRecord:
        status = str(d.get("status", "DEFAULT")).upper().replace(" ", "_")
        return AgentRecord(
            agent_id=d["id"],
            name=d.get("name", d["id"]),
            description=d.get("description", ""),
            owner=d.get("owner", ""),
            status=AgentStatus(status) if status in AgentStatus.__members__ else AgentStatus.DEFAULT,
        )

    def create_agent(self, name: str, description: str, owner: str, allowed_scopes: frozenset[str]) -> AgentRecord:
        body = {"name": name, "description": description, "owner": owner}
        return self._to_record(self._call("POST", "/v2.0/Agents", json=body, headers=self._admin_bearer()))

    def get_agent(self, agent_id: str) -> AgentRecord:
        return self._to_record(self._call("GET", f"/v2.0/Agents/{agent_id}", headers=self._admin_bearer()))

    def find_agent(self, name: str) -> AgentRecord:
        for rec in self.list_agents():
            if rec.name == name:
                return rec
        raise AgentNotFound(name)

    def list_agents(self) -> list[AgentRecord]:
        data = self._call("GET", "/v2.0/Agents", headers=self._admin_bearer())
        return [self._to_record(r) for r in data.get("Resources", [])]

    def set_status(self, agent_id: str, status: AgentStatus, actor: str) -> AgentRecord:
        body = {"Operations": [{"op": "replace", "path": "status", "value": status.value}]}
        return self._to_record(self._call("PATCH", f"/v2.0/Agents/{agent_id}", json=body, headers=self._admin_bearer()))

    def register_client(self, agent_id: str) -> ClientCredentials:
        body = {"entity_id": agent_id, "entity_type": "agent", "grant_types": ["client_credentials"]}
        data = self._call("POST", "/oauth2/register", json=body, headers=self._admin_bearer())
        return ClientCredentials(data["client_id"], data["client_secret"], agent_id)

    def client_credentials_token(self, client_id: str, client_secret: str, scope: list[str], audience: str) -> str:
        data = self._call(
            "POST",
            "/v1.0/endpoint/default/token",
            data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
                  "scope": " ".join(scope), "audience": audience},
        )
        return data["access_token"]

    def exchange_token(self, subject_token: str, actor_token: str, scope: list[str], audience: str,
                       authorization_details: list[dict[str, Any]] | None = None) -> str:
        form: dict[str, Any] = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "actor_token": actor_token,
            "actor_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "scope": " ".join(scope),
            "audience": audience,
        }
        if authorization_details:
            import json as _json

            form["authorization_details"] = _json.dumps(authorization_details)
        return self._call("POST", "/v1.0/endpoint/default/token", data=form, headers=self._admin_bearer())["access_token"]

    def verification_key(self) -> Any:
        """Return the tenant JWKS (callers should use PyJWKClient in production)."""
        return self._call("GET", "/oidc/endpoint/default/jwks")
