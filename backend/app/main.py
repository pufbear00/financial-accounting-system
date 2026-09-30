"""Local empty-data application; no background synchronization is started."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.database import engine, Base, ensure_schema_compatibility
from app import models  # noqa: F401 — register local tables
from app.api import api_router
from app.services.seed import seed_categories


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_schema_compatibility(conn)
    await seed_categories()
    yield
    await engine.dispose()


app = FastAPI(title="费用审批看板 · 本地空数据版", version="1.0.0-demo", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5282", "http://localhost:5282"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)


@app.middleware("http")
async def reject_external_actions(request, call_next):
    if request.url.path.endswith(("/import-feishu-attachments", "/sync-feishu-comments")):
        return JSONResponse(status_code=403, content={"detail": "本地空数据版已停用飞书附件与评论同步"})
    return await call_next(request)


@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "mode": "offline-empty-demo", "external_sync": False}
