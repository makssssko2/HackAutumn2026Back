import logging
import asyncio

from app.core.bootstrap import bootstrap
from app.integrations.bitrix24_polling import poll_bitrix24


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    (await bootstrap())
    while True:
        try:
            (await poll_bitrix24())
        except Exception as error:
            logging.error('Bitrix24 poll cycle failed (%s)', type(error).__name__)
        (await asyncio.sleep(5))


if __name__ == '__main__':
    asyncio.run(main())
