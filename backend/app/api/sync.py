"""Fail closed for all previous live synchronization entry points."""
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/sync", tags=["offline-demo"])


@router.get("/logs")
async def list_sync_logs():
    return []


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def disabled_sync(path: str):
    raise HTTPException(status_code=403, detail="本地空数据版：外部同步、日报与导入入口已停用，不会连接原系统")
