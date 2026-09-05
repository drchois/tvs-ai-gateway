from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header


@dataclass(frozen=True)
class RequestContext:
    tenant_id: str | None
    user_id: str | None
    roles: list[str]

    def resolve(self, *, tenant_id: str | None, user_id: str | None) -> "RequestContext":
        return RequestContext(
            tenant_id=(self.tenant_id or tenant_id or "default").strip(),
            user_id=(self.user_id or user_id or "anonymous").strip(),
            roles=self.roles,
        )


def request_context_headers(
    tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    user_id: str | None = Header(default=None, alias="X-User-Id"),
    roles: str = Header(default="", alias="X-Roles"),
) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id.strip() if tenant_id else None,
        user_id=user_id.strip() if user_id else None,
        roles=[value.strip().upper() for value in roles.split(",") if value.strip()],
    )


def required_request_context(
    tenant_id: str = Header(alias="X-Tenant-Id"),
    user_id: str = Header(alias="X-User-Id"),
    roles: str = Header(default="", alias="X-Roles"),
) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id.strip(),
        user_id=user_id.strip(),
        roles=[value.strip().upper() for value in roles.split(",") if value.strip()],
    )
