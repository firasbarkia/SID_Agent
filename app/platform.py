"""Read-only mapping of the supplied UML to the main platform's MongoDB records."""

from app.config import Settings
from app.domain import DomainError
from app.identity import Principal


class PlatformReader:
    def __init__(self, db, settings: Settings):
        self.users = db[settings.platform_users_collection]
        self.candidates = db[settings.platform_candidates_collection]
        self.companies = db[settings.platform_companies_collection]
        self.offers = db[settings.platform_offers_collection]

    async def principal(self, verified_user_id: str) -> Principal:
        user = await self.users.find_one({"id": verified_user_id, "isActive": True})
        if user is None or user.get("role") not in {"CANDIDATE", "RECRUITER", "ADMIN"}:
            raise DomainError(401, "inactive_or_unknown_user")
        company_id = None
        if user["role"] == "RECRUITER":
            company = await self.companies.find_one({"userId": verified_user_id})
            if company is None:
                raise DomainError(403, "company_profile_required")
            company_id = company["id"]
        return Principal(user_id=verified_user_id, role=user["role"], company_id=company_id)

    async def owned_candidate(self, principal: Principal, profile_id: str):
        profile = await self.candidates.find_one({"id": profile_id, "userId": principal.user_id})
        if profile is None:
            raise DomainError(404, "candidate_profile_not_found")
        return profile

    async def company_offer(self, principal: Principal, offer_id: str):
        if principal.role != "RECRUITER" or not principal.company_id:
            raise DomainError(403, "recruiter_access_required")
        offer = await self.offers.find_one({"id": offer_id, "companyId": principal.company_id})
        if offer is None:
            raise DomainError(404, "job_offer_not_found")
        return offer
