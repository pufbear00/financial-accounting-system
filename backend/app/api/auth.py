from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User, UserCategory
from app.auth import verify_password, create_access_token, get_current_user
from app.schemas.auth import LoginRequest, TokenResponse, UserInfo

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.username == req.username))
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = create_access_token({"sub": user.id})
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserInfo)
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cat_ids = (await db.execute(
        select(UserCategory.category_id).where(UserCategory.user_id == user.id)
    )).scalars().all()
    return UserInfo(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        category_ids=list(cat_ids),
        feishu_open_id=user.feishu_open_id,
    )
