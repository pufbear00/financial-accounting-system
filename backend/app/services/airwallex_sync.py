"""No external payment-provider connector is shipped in this local copy."""


async def run_airwallex_sync(*args, **kwargs):
    raise RuntimeError("本地空数据版已停用外部支付平台同步")
