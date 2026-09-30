"""Create a new local-only administrator interactively; never store plaintext."""
import asyncio
from getpass import getpass
from sqlalchemy import select
from app import models  # noqa: F401
from app.database import Base, engine, async_session
from app.models.user import User
from app.auth import hash_password
from app.services.seed import seed_categories


async def create_admin(username: str, password: str):
    if not username.strip() or len(password) < 12 or len(password.encode()) > 72:
        raise ValueError("用户名不能为空；密码至少 12 个字符，且 UTF-8 编码不超过 72 字节")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await seed_categories()
    async with async_session() as db:
        if await db.scalar(select(User.id).where(User.role == "admin")):
            raise ValueError("已有本地管理员，未覆盖任何账号")
        db.add(User(username=username.strip(), password_hash=hash_password(password),
                    display_name="本地演示管理员", role="admin"))
        await db.commit()
    await engine.dispose()


if __name__ == "__main__":
    print("仅创建本地演示账号。不要使用公司、飞书或 GitHub 密码。")
    username = input("新用户名：").strip()
    password = getpass("新演示密码（至少 12 个字符）：")
    if password != getpass("再次输入演示密码："):
        raise SystemExit("两次密码不一致，未创建账号")
    asyncio.run(create_admin(username, password))
    print("本地管理员已创建；没有写入真实业务记录。")
