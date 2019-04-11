import asyncio
import datetime
import html
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List

import aiohttp
from aiotg import BotApiError, Chat
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from forward import conf
from forward.bot import ChatEditMedia, ForwardBot
from forward.model import Profile, WallPost, db
from forward.model.helpers import ThreadSwitcherWithDB, db_in_thread
from forward.model.utils import call_async


def init_logging():
    config = {
        'handlers': [
            {
                'sink': Path(conf.root_dir) / conf.log_file,
                'level': 'DEBUG'
            },
        ],
    }
    if conf.stdout_log:
        config['handlers'].append({'sink': sys.stdout, 'level': 'DEBUG'})
    logger.configure(**config)

    class InterceptHandler(logging.Handler):
        def emit(self, record):
            logger_opt = logger.opt(depth=6, exception=record.exc_info)
            logger_opt.log(record.levelname, record.getMessage())

    logging.getLogger().setLevel(logging.DEBUG)
    if conf.sql_log:
        logging.getLogger('sqlalchemy').setLevel(logging.DEBUG)
    logging.getLogger().addHandler(InterceptHandler())


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
    to_update = []
    async with db_in_thread():
        last_wall_post_id = WallPost.get_last_wall_post_id()
        if not last_wall_post_id:
            last_wall_post_id = 0
        if max(item['id'] for item in data['items']) <= last_wall_post_id:
            logger.info('No new updates')
            to_update = data['items']
        else:
            for item in data['profiles']:
                profile = Profile.create_from_item(item)
                db.add(profile)
            for item in data['items']:
                if item['id'] > last_wall_post_id:
                    if item['from_id'] == conf.group_id:
                        continue  # TODO fix repost
                    post = WallPost.create_from_item(item)
                    db.add(post)
                    to_send.append(item['id'])
                else:
                    to_update.append(item)
            db.commit()
    if to_update:
        await update_existing(to_update, bot)
    if to_send:
        await send_updates(to_send, bot)


def render_message(post: WallPost):
    logger.info('Render message')
    user = post.profile
    message = post.text or '>'
    message = html.escape(message)
    who = f'<a href="{user.profile_link}">{user.first_name} {user.last_name}</a>'
    original = f'<a href="{post.source}">@wall</a>'
    text = f'{who} {original} ❤{post.likes} ✒{post.comments}'
    text = f'{text}\n{message}'
    return text


@ThreadSwitcherWithDB.optimized
async def update_existing(to_update: List[Dict], bot):
    to_update = {item['id']: item for item in to_update}
    to_update_str = ' '.join(str(i) for i in to_update)
    logger.info(f'Updating existing: {to_update_str}')
    to_update_send = []
    async with db_in_thread():
        posts_to_update = WallPost.get_existing_to_update(list(to_update.keys()))
        for post in posts_to_update:
            if post.update_existing(to_update[post.wall_post_id]):
                to_update_send.append(post.wall_post_id)
        db.commit()
    to_update_send_str = ' '.join(str(i) for i in to_update_send)
    logger.info(f'Modified entities: {to_update_send_str}')
    if to_update_send:
        async with db_in_thread():
            posts_to_update_send = WallPost.get_existing_to_update(to_update_send, load_profiles=True)
        for post in posts_to_update_send:
            await EditSender(bot, post)()


class EditSender:
    def __init__(self, bot: ForwardBot, post: WallPost):
        self.chat = ChatEditMedia(bot._bot, conf.channel_id)
        self.photos = post.photo_attachments
        self.post = post
        self.text = render_message(post)

    async def edit_text(self):
        try:
            await self.chat.edit_text(
                self.post.message_id,
                text=render_message(self.post),
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        except BotApiError:
            logger.warning('ApiError: probably message is not modified!')

    async def edit_media(self):
        self.photos[0]['caption'] = self.text
        self.photos[0]['parse_mode'] = 'HTML'
        try:
            await self.chat.edit_message_media(self.post.message_id, media=json.dumps(self.photos[0]))
        except BotApiError:
            logger.error('Error during editing media')

    async def __call__(self):
        if self.photos:
            logger.info(f'Editing message media {self.post.message_id}')
            await self.edit_media()
        else:
            logger.info(f'Editing message text {self.post.message_id}')
            await self.edit_text()


class UpdatesSender:
    def __init__(self, bot, loop, item):
        self.loop = loop
        self.text = render_message(item)
        self.photos = item.photo_attachments
        self.likes = item.likes
        self.comments = item.comments
        self.chat = Chat(bot._bot, conf.channel_id)

    def send_photos(self):
        if len(self.text) > 1024:
            text = 'TOO LONG DESCRIPTION'
        else:
            text = self.text
        self.photos[0]['caption'] = text
        self.photos[0]['parse_mode'] = 'HTML'
        try:
            result = call_async(
                self.loop,
                lambda: self.chat.send_media_group(
                    media=json.dumps(self.photos),
                    disable_web_page_preview=True
                )
            )
        except Exception:
            logger.exception('Error during sending new post!')
            return
        return result['result'][0]['message_id']

    def send_text(self):
        try:
            result = call_async(
                self.loop,
                lambda: self.chat.send_text(
                    self.text,
                    disable_web_page_preview=True,
                    parse_mode='HTML',
                )
            )
        except Exception:
            logger.exception('Error during sending new post!')
            return
        return result['result']['message_id']

    def __call__(self):
        if self.photos:
            return self.send_photos()
        else:
            return self.send_text()


def _send_updates(updates, bot, to_sleep, loop):
    updates = WallPost.get_updates(updates)
    for item in updates:
        message_id = UpdatesSender(bot, loop, item)()
        if message_id:
            item.message_id = message_id
            db.add(item)
    db.commit()
    # if to_sleep:
    #     logger.info('Sleeping since there are multiple messages..')
    #     await asyncio.sleep(1)


async def send_updates(updates, bot):
    to_sleep = False
    if len(updates) > 1:
        to_sleep = True
    updates_str = ' '.join(str(i) for i in updates)
    logger.info(f'Sending new messages : {updates_str}')
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: _send_updates(updates, bot, to_sleep, loop))


async def main(run_scheduler=True):
    bot = ForwardBot()
    if run_scheduler:
        scheduler = AsyncIOScheduler()
        scheduler.start()
        scheduler.add_job(
            ask, 'interval', (bot.session, bot),
            seconds=conf.interval, next_run_time=datetime.datetime.now()
        )
    bot_loop = asyncio.create_task(bot.loop())
    await asyncio.wait([bot_loop, ])


def run():
    init_logging()
    logger.info('Running flforward service')
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('Shutting down..')


if __name__ == '__main__':
    run()
