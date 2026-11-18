"""官方预签名分片上传；子任务在返回或取消前全部回收。"""

from __future__ import annotations

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
    if not isinstance(parts, list) or not parts:
        raise ValueError("QQ 上传分片列表无效")
    normalized: list[tuple[int, str]] = []
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("QQ 上传分片记录无效")
        index = part.get("index")
        if isinstance(index, str) and index.isascii() and index.isdecimal():
            index = int(index)
        if type(index) is not int:
            raise ValueError("QQ 上传分片索引无效")
        url = part.get("presigned_url")
        if not isinstance(url, str) or not url:
            raise ValueError("QQ 上传分片地址无效")
        normalized.append((index, url))

    config = prepared["upload_config"]
    if not isinstance(config, dict):
        raise ValueError("QQ 上传配置无效")
    concurrency = config["concurrency"]
    if isinstance(concurrency, str) and concurrency.isascii() and concurrency.isdecimal():
        concurrency = int(concurrency)
    if type(concurrency) is not int or not 1 <= concurrency <= 32:
        raise ValueError("QQ 上传并发数必须为 1 到 32")
    semaphore = asyncio.Semaphore(concurrency)

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
                    # 与原版一致：仅分片转存错误最多重报 3 次，固定间隔 5 秒。
                    if error.code != 40093001 or attempt == 2:
                        raise
                    await asyncio.sleep(5)
                else:
                    break

    await run_concurrently(
        send_part(position, part_index, url) for position, (part_index, url) in enumerate(normalized)
    )
    return await actions._request("POST", path + "/files", json={"upload_id": upload_id, "srv_send_msg": srv_send_msg})
