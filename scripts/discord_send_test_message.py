#!/usr/bin/env python3
"""Discord Bot 冒烟：REST 单次发信（需 `.env` 中 DISCORD_BOT_TOKEN）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402 pylint: disable=C0415

from app.config import get_settings  # noqa: E402 pylint: disable=C0415


async def dispatch_message(*, token: str, channel_id: str, content: str) -> tuple[int, str]:
    endpoint = f"https://discord.com/api/v10/channels/{channel_id}/messages"

    payload = {"content": content[:2000]}
    headers = {
        "Authorization": f"Bot {token.strip()}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.post(endpoint, json=payload, headers=headers)

    return response.status_code, response.text


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OptionsAji Discord Bot outbound smoke")

    parser.add_argument("--channel-id", required=True, help="目标频道 Snowflake ID")
    parser.add_argument(
        "--message",
        default="OptionsAji · outbound smoke OK",
        help="≤2000 字符",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验 token/channel，不 POST",
    )
    parser.add_argument(
        "--print-json-meta",
        action="store_true",
        help="成功时 stdout 附带返回 JSON meta（不含令牌）",
    )

    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    os.chdir(ROOT)
    cfg = get_settings()
    tok = cfg.discord_bot_token.strip()

    if not tok:
        print("[错误] DISCORD_BOT_TOKEN 未配置。", file=sys.stderr)
        return 2

    ch_raw = args.channel_id.strip()
    if not ch_raw.isdigit():
        print("[错误] channel-id 必须是数字。", file=sys.stderr)
        return 2

    body = args.message.strip() or "(empty)"

    if args.dry_run:
        print(f"[dry-run] token=len{len(tok)} channel={ch_raw}")
        return 0

    try:
        status, raw_text = asyncio.run(
            dispatch_message(token=tok, channel_id=ch_raw, content=body),
        )
        if status >= 400:
            print(f"[失败] HTTP {status}: {raw_text[:500]}", file=sys.stderr)
            return 1

        print(f"[成功] HTTP {status} channel={ch_raw}")

        if args.print_json_meta and raw_text.strip().startswith("{"):
            snippet = json.dumps(json.loads(raw_text).get("id"), ensure_ascii=False)
            print(f"message_id snippet: {snippet}")

        return 0

    except Exception as exc:
        print(f"[异常] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
