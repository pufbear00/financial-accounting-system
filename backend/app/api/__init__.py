from fastapi import APIRouter

from app.api.auth import router as auth_router
from app.api.users import router as users_router
from app.api.approvals import router as approvals_router
from app.api.budgets import router as budgets_router
from app.api.categories import router as categories_router
from app.api.sync import router as sync_router
from app.api.dashboard import router as dashboard_router
from app.api.vouchers import router as vouchers_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(approvals_router)
api_router.include_router(budgets_router)
api_router.include_router(categories_router)
api_router.include_router(sync_router)
api_router.include_router(dashboard_router)
api_router.include_router(vouchers_router)
