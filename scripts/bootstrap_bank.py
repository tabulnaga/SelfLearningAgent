"""Create/update the Hindsight bank deliberately: missions, directives, knowledge pages. Idempotent.

    python -m scripts.bootstrap_bank
"""

from __future__ import annotations

import asyncio
import json

from hindsight_client import Hindsight

from app.config import get_settings
from app.memory.bank_setup import bootstrap_bank


async def main() -> None:
    s = get_settings()
    key = s.hindsight_api_key.get_secret_value() if s.hindsight_api_key else None
    client = Hindsight(base_url=s.hindsight_url, api_key=key, timeout=120)
    try:
        print(json.dumps(await bootstrap_bank(client, s.hindsight_bank_id), indent=2))
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
