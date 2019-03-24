from aiotg import Bot, Chat

from forward import conf
from forward.model import Admin, db
from forward.model.helpers import ThreadSwitcherWithDB, db_in_thread


@ThreadSwitcherWithDB.optimized
async def reg(chat: Chat, match):
    async with db_in_thread():
        admin = db.query(Admin).filter(Admin.chat_id == chat.id).one_or_none()
    if admin:
        await chat.send_text('You are already registered!')
        return
    async with db_in_thread():
        admin = Admin(chat_id=chat.id)
        db.add(admin)
        db.commit()
    await chat.send_text('You are successfully registered!')


async def get_chat_id(chat, match):
    await chat.send_text(f'Chat id: {chat.id}')


class ChatEditMedia(Chat):

    def edit_message_media(self, message_id, media, **options):
        return self.bot.api_call(
            "editMessageMedia",
            chat_id=str(self.id),
            message_id=message_id,
            media=media,
            **options
        )

    def edit_text(self, message_id, text, **options):
        """
        Edit the message in this chat.

        :param int message_id: ID of the message to edit
        :param str text: Text to edit the message to
        :param dict markup: Markup options
        :param str parse_mode: Text parsing mode (``"Markdown"``, ``"HTML"`` or
            ``None``)
        """

        return self.bot.edit_message_text(
            self.id,
            message_id,
            text,
            **options
        )


class ForwardBot:
    def __init__(self):
        self._bot = Bot(conf.bot_token, proxy=conf.tele_proxy)
        self.session = self._bot.session
        self.loop = self._bot.loop
        self.init_handlers()

    def init_handlers(self):
        self._bot.add_command(r'/reg', reg)
        self._bot.add_command(r'/ch', get_chat_id)

    @ThreadSwitcherWithDB.optimized
    async def notify_admins(self, text, **options):
        async with db_in_thread():
            admins = db.query(Admin).all()
        for admin in admins:
            await self._bot.send_message(admin.chat_id, text, **options)
