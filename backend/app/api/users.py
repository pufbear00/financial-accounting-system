from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User, UserCategory
from app.auth import get_current_user, hash_password
from app.schemas.auth import UserInfo, UserCreate, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


def _require_admin(user: User):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")


@router.get("", response_model=list[UserInfo])
async def list_users(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_admin(user)
    users = (await db.execute(select(User).order_by(User.id))).scalars().all()
    result = []
    for u in users:
        cat_ids = (await db.execute(
            select(UserCategory.category_id).where(UserCategory.user_id == u.id)
        )).scalars().all()
        result.append(UserInfo(
            id=u.id, username=u.username, display_name=u.display_name,
            role=u.role, category_ids=list(cat_ids), feishu_open_id=u.feishu_open_id,
        ))
    return result


@router.post("", response_model=UserInfo)
async def create_user(data: UserCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_admin(user)
    existing = await db.scalar(select(User).where(User.username == data.username))
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")
    new_user = User(
        username=data.username,
        password_hash=hash_password(data.password),
        display_name=data.display_name,
        role=data.role,
        feishu_open_id=data.feishu_open_id,
    )
    db.add(new_user)
    await db.flush()
    for cat_id in data.category_ids:
        db.add(UserCategory(user_id=new_user.id, category_id=cat_id))
    await db.commit()
    return UserInfo(
        id=new_user.id, username=new_user.username, display_name=new_user.display_name,
        role=new_user.role, category_ids=data.category_ids, feishu_open_id=new_user.feishu_open_id,
    )


@router.put("/{user_id}", response_model=UserInfo)
async def update_user(user_id: int, data: UserUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_admin(user)
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if data.display_name is not None:
        target.display_name = data.display_name
    if data.password is not None:
        target.password_hash = hash_password(data.password)
    if data.role is not None:
        target.role = data.role
    if data.feishu_open_id is not None:
        target.feishu_open_id = data.feishu_open_id
    if data.category_ids is not None:
        await db.execute(delete(UserCategory).where(UserCategory.user_id == user_id))
        for cat_id in data.category_ids:
            db.add(UserCategory(user_id=user_id, category_id=cat_id))
    await db.commit()
    cat_ids = (await db.execute(
        select(UserCategory.category_id).where(UserCategory.user_id == user_id)
    )).scalars().all()
    return UserInfo(
        id=target.id, username=target.username, display_name=target.display_name,
        role=target.role, category_ids=list(cat_ids), feishu_open_id=target.feishu_open_id,
    )


@router.delete("/{user_id}")
async def delete_user(user_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    _require_admin(user)
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    await db.execute(delete(UserCategory).where(UserCategory.user_id == user_id))
    await db.delete(target)
    await db.commit()
    return {"ok": True}
