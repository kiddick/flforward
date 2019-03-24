from loguru import logger
from sqla_wrapper import SQLAlchemy
from sqlalchemy import BigInteger, Column, ForeignKey, Integer, JSON, String, func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload, relationship

from forward import conf
from .utils import db_session_scope

db = SQLAlchemy(conf.db_uri, scopefunc=db_session_scope)


class BaseModel(db.Model):
    __abstract__ = True

    @classmethod
    def get_by_id(cls, model_id):
        try:
            return db.query(cls).get(model_id)
        except SQLAlchemyError:
            logger.exception()
            raise


class Admin(BaseModel):
    admin_id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, unique=True)


class Profile(BaseModel):
    profile_id = Column(Integer, primary_key=True)
    first_name = Column(String)
    last_name = Column(String)
    data = Column(JSON)

    def __str__(self):
        return f'{self.profile_id} - {self.first_name} {self.last_name}'

    @classmethod
    def create(cls, profile_id, first_name, last_name, data):
        profile = db.query(cls).filter(cls.profile_id == profile_id).one_or_none()
        if profile:
            return profile
        return cls(profile_id=profile_id, first_name=first_name, last_name=last_name, data=data)

    @classmethod
    def create_from_item(cls, item):
        return cls.create(profile_id=item['id'], first_name=item['first_name'], last_name=item['last_name'], data=item)

    @property
    def profile_link(self):
        return f'https://vk.com/id{self.profile_id}'


def max_size(sizes):
    sizes.sort(key=lambda k: k['height'])
    return sizes[-1]['url']


class WallPost(BaseModel):
    wall_post_id = Column(Integer, primary_key=True)
    text = Column(String)
    comments = Column(Integer)
    likes = Column(Integer)
    data = Column(JSON)
    profile_id = Column(Integer, ForeignKey(Profile.profile_id), nullable=True)
    profile = relationship('Profile')
    message_id = Column(Integer)

    @classmethod
    def create_from_item(cls, item):
        wall_post = cls(
            wall_post_id=item['id'],
            text=item['text'],
            comments=item['comments']['count'],
            likes=item['likes']['count'],
            profile_id=item['from_id'],
            data=item
        )
        return wall_post

    @property
    def source(self):
        return f'https://vk.com/wall{conf.group_id}_{self.wall_post_id}'

    @property
    def photo_attachments(self):
        attachments = self.data.get('attachments')
        if not attachments:
            return
        attachments = [a for a in attachments if a['type'] == 'photo']
        if not attachments:
            return
        return [{'type': 'photo', 'media': max_size(attach['photo']['sizes'])} for attach in attachments]

    @classmethod
    def get_updates(cls, updates):
        updates = db.query(cls).filter(cls.wall_post_id.in_(updates)).options(joinedload(cls.profile))
        updates = sorted(updates, key=lambda u: u.wall_post_id)
        return updates

    @classmethod
    def get_existing_to_update(cls, items, load_profiles=False):
        to_update = db.query(cls).filter(cls.wall_post_id.in_(items))
        if load_profiles:
            to_update = to_update.options(joinedload(cls.profile))
        return to_update

    @classmethod
    def get_last_wall_post_id(cls):
        return db.query(func.max(cls.wall_post_id)).scalar()

    def update_existing(self, item):
        to_send = False
        if self.text != item['text']:
            self.text = item['text']
            to_send = True
        if self.comments != item['comments']['count']:
            self.comments = item['comments']['count']
            to_send = True
        if self.likes != item['likes']['count']:
            self.likes = item['likes']['count']
            to_send = True
        if to_send:
            self.data = item
        return to_send

    def __str__(self):
        return f'{self.wall_post_id} - {self.text}'
