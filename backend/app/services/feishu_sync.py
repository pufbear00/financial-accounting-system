"""Live approval integration intentionally unavailable in the local copy."""
from app.services.voucher_feishu_import import FeishuImportError


async def run_sync(*args, **kwargs):
    raise FeishuImportError("本地空数据版已停用外部审批同步")


async def _resolve_user_name(*args, **kwargs):
    raise FeishuImportError("本地空数据版已停用外部通讯录查询")
