import asyncio
import datetime
import logging
import sys
from pathlib import Path
from typing import Dict

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from sqlalchemy import func

from forward import conf
from forward.bot import ForwardBot
from forward.model import Profile, WallPost, db
from forward.model.helpers import ThreadSwitcherWithDB, db_in_thread

config = {
    'handlers': [
        {
            'sink': Path(conf.root_dir) / conf.log_file,
            'level': 'DEBUG'
        },
        {
            'sink': sys.stdout,
            'level': 'DEBUG'
        },
    ],
}
logger.configure(**config)


class InterceptHandler(logging.Handler):
    def emit(self, record):
        logger_opt = logger.opt(exception=record.exc_info)
        logger_opt.log(record.levelname, record.getMessage())


logging.getLogger(None).setLevel(logging.DEBUG)
logging.getLogger('sqlalchemy').setLevel(logging.DEBUG)
logging.getLogger(None).addHandler(InterceptHandler())

API = 'https://api.vk.com/method/'

params = {
    'access_token': conf.access_token,
    'v': conf.api_version,
    'count': 20,
    'owner_id': conf.group_id,
    'extended': 1,
}


async def fetch(session):
    async with session.get(f'{API}wall.get', params=params) as response:
        return await response.json()


async def ask(session: aiohttp.ClientSession, bot):
    try:
        response = await session.get(f'{API}wall.get', params=params)
    except Exception:
        logger.exception(f'Exception during wall check')
        return
    data = await response.json()
    logger.debug(f'Total: {data["response"]["count"]}')
    await process_updates(data['response'], bot)


@ThreadSwitcherWithDB.optimized
async def process_updates(data: Dict, bot):
    to_send = []
    async with db_in_thread():
        last_wall_post_id = db.query(func.max(WallPost.wall_post_id)).scalar()
        if not last_wall_post_id:
            last_wall_post_id = 0
        if max(item['id'] for item in data['items']) <= last_wall_post_id:
            return
        for item in data['profiles']:
            profile = Profile.create(
                profile_id=item['id'],
                first_name=item['first_name'],
                last_name=item['last_name'],
                data=item
            )
            db.add(profile)
        for item in data['items']:
            if item['id'] > last_wall_post_id:
                if item['from_id'] == conf.group_id:
                    continue  # TODO fix repost
                post = WallPost(
                    wall_post_id=item['id'],
                    text=item['text'],
                    profile_id=item['from_id'],
                    data=item
                )
                db.add(post)
                to_send.append(item['id'])
        db.commit()
    await send_updates(to_send, bot)


def render_message(post: WallPost):
    user = post.profile
    message = post.text or '>'
    text = f'[{user.first_name} {user.last_name}]({user.profile_link}) [@]({post.source}) '
    text = f'{text}\n{message}'
    return text


@ThreadSwitcherWithDB.optimized
async def send_updates(updates, bot):
    to_sleep = False
    if len(updates) > 1:
        to_sleep = True
    async with db_in_thread():
        updates = db.query(WallPost).filter(WallPost.wall_post_id.in_(updates))
    updates = sorted(updates, key=lambda u: u.wall_post_id)
    logger.info(f'New updates: {" ".join(str(u) for u in updates)}')
    for item in updates:
        text = render_message(item)
        try:
            await bot._bot.send_message(conf.channel_id, text, disable_web_page_preview=True, parse_mode='Markdown')
        except Exception:
            logger.exception('Error during sending new post!')
            continue
        if to_sleep:
            logger.info('Sleeping since there are multiple messages..')
            await asyncio.sleep(1)


async def main():
    bot = ForwardBot()
    scheduler = AsyncIOScheduler()
    scheduler.start()
    scheduler.add_job(
        ask, 'interval', (bot.session, bot),
        seconds=conf.interval, next_run_time=datetime.datetime.now()
    )
    bot_loop = asyncio.create_task(bot.loop())
    await asyncio.wait([bot_loop, ])


def run():
    logger.info('Running flforward service')
    asyncio.run(main())


if __name__ == '__main__':
    run()
