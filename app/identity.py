from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


class Principal(BaseModel):
    """Identity returned by the platform's server-side authentication adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    user_id: str = Field(min_length=1, max_length=128)
    company_id: str | None = Field(default=None, min_length=1, max_length=128)
    role: Literal["CANDIDATE", "RECRUITER", "ADMIN"]

    @property
    def scope_id(self) -> str:
        # Candidates do not belong to companies in the supplied schema.
        return (
            f"candidate:{self.user_id}"
            if self.role == "CANDIDATE"
            else f"company:{self.company_id}"
        )


UserIdVerifier = Callable[[Request], Awaitable[str]]


async def get_principal(request: Request) -> Principal:
    resolver = request.app.state.identity_resolver
    if resolver is None:
        raise HTTPException(503, detail={"code": "platform_identity_not_integrated"})
    principal = await resolver(request)
    if not isinstance(principal, Principal):
        raise HTTPException(401, detail={"code": "invalid_platform_identity"})
    return principal


async def candidate(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    if principal.role != "CANDIDATE":
        raise HTTPException(403, detail={"code": "candidate_access_required"})
    return principal


Candidate = Annotated[Principal, Depends(candidate)]
