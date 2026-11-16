"""官方预签名分片上传；子任务在返回或取消前全部回收。"""

from __future__ import annotations

import math
import asyncio
import hashlib

from .codec import api_path, opaque_id, identifier
from ...core.concurrency import run_concurrently


async def upload(actions, route, target_id, file_type, file_name, content, srv_send_msg):
    from .api import QQAPIError

    if not isinstance(content, bytes) or not content or len(content) > 200 * 1024 * 1024:
        raise ValueError("QQ 上传需要非空字节，最大 200 MiB")
    if type(file_type) is not int or file_type not in {1, 2, 3, 4}:
        raise ValueError("QQ 上传类型必须为 1、2、3 或 4")
    if not isinstance(file_name, str) or not file_name or any(char in file_name for char in "\\/\x00"):
        raise ValueError("QQ 上传文件名无效")
    path = api_path("v2", route, identifier(target_id))
    prepared = await actions._request(
        "POST",
        path + "/upload_prepare",
        json={
            "file_type": file_type,
            "file_name": file_name,
            "file_size": len(content),
            "md5": hashlib.md5(content).hexdigest(),
            "sha1": hashlib.sha1(content).hexdigest(),
            "md5_10m": hashlib.md5(content[: 10 * 1024 * 1024]).hexdigest(),
        },
    )
    upload_id = opaque_id(prepared["upload_id"])
    block_size = prepared["block_size"]
    if isinstance(block_size, str) and block_size.isascii() and block_size.isdecimal():
        block_size = int(block_size)
    if type(block_size) is not int or block_size <= 0:
        raise ValueError("QQ 上传分块大小无效")
    parts = prepared["parts"]
    if not isinstance(parts, list) or len(parts) != math.ceil(len(content) / block_size):
        raise ValueError("QQ 上传分片数量与文件大小不一致")
    config = prepared["upload_config"]
    concurrency = config["concurrency"]
    if type(concurrency) is not int or not 1 <= concurrency <= 32:
        raise ValueError("QQ 上传并发数必须为 1 到 32")
    for name in ("retry_timeout", "retry_delay"):
        if type(config[name]) is not int or not 0 <= config[name] <= 3600:
            raise ValueError("QQ 上传重试设置无效")
    for index, part in enumerate(parts):
        if type(part["index"]) is not int or part["index"] != index:
            raise ValueError("QQ 上传分片索引必须从零连续递增")
    semaphore = asyncio.Semaphore(concurrency)

    async def send_part(index, part):
        async with semaphore:
            block = content[index * block_size : (index + 1) * block_size]
            await actions.put_upload_part(presigned_url=part["presigned_url"], data=block)
            deadline = asyncio.get_running_loop().time() + config["retry_timeout"]
            for attempt in range(3):
                try:
                    await actions._request(
                        "POST",
                        path + "/upload_part_finish",
                        json={
                            "upload_id": upload_id,
                            "part_index": index,
                            "block_size": len(block),
                            "md5": hashlib.md5(block).hexdigest(),
                        },
                    )
                except QQAPIError as error:
                    # 平台明确返回分片转存失败时，重报同一分片完成；不重发业务消息。
                    if (
                        error.code != 40093001
                        or attempt == 2
                        or asyncio.get_running_loop().time() + config["retry_delay"] >= deadline
                    ):
                        raise
                    await asyncio.sleep(config["retry_delay"])
                else:
                    break

    await run_concurrently(send_part(index, part) for index, part in enumerate(parts))
    return await actions._request("POST", path + "/files", json={"upload_id": upload_id, "srv_send_msg": srv_send_msg})
