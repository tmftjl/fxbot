"""官方预签名分片上传；子任务在返回或取消前全部回收。"""

from __future__ import annotations

import asyncio
import hashlib

from .codec import api_path
from .models import PostC2CFilesPrepareReturn
from ...core.concurrency import run_concurrently


async def upload(actions, route, target_id, file_type, file_name, content, srv_send_msg):
    from .api import QQAPIError

    path = api_path("v2", route, (target_id))
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
    prepared = PostC2CFilesPrepareReturn.model_validate(prepared)
    upload_id = prepared.upload_id
    block_size = prepared.block_size
    semaphore = asyncio.Semaphore(prepared.upload_config.concurrency)

    async def send_part(position: int, part_index: int, presigned_url: str) -> None:
        async with semaphore:
            block = content[position * block_size : (position + 1) * block_size]
            await actions.put_upload_part(presigned_url=presigned_url, data=block)
            for attempt in range(3):
                try:
                    await actions._request(
                        "POST",
                        path + "/upload_part_finish",
                        json={
                            "upload_id": upload_id,
                            "part_index": part_index,
                            "block_size": len(block),
                            "md5": hashlib.md5(block).hexdigest(),
                        },
                    )
                except QQAPIError as error:
                    # 仅分片转存错误最多重报 3 次，固定间隔 5 秒。
                    if error.code != 40093001 or attempt == 2:
                        raise
                    await asyncio.sleep(5)
                else:
                    break

    await run_concurrently(
        send_part(position, part.index, part.presigned_url) for position, part in enumerate(prepared.parts)
    )
    return await actions._request("POST", path + "/files", json={"upload_id": upload_id})
