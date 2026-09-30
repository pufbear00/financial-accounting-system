"""No external messaging integration is shipped in this local copy."""


async def send_daily_reports(*args, **kwargs):
    raise RuntimeError("本地空数据版已停用外部消息发送")
