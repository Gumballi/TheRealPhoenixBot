import logging
import threading

from sqlalchemy import Column, Integer, UnicodeText, String, ForeignKey, UniqueConstraint, BigInteger, func, text

from tg_bot import dispatcher
from tg_bot.modules.sql import BASE, SESSION

LOGGER = logging.getLogger(__name__)


class Users(BASE):
    __tablename__ = "users"
    user_id = Column(BigInteger, primary_key=True)
    username = Column(UnicodeText)

    def __init__(self, user_id, username=None):
        self.user_id = user_id
        self.username = username

    def __repr__(self):
        return "<User {} ({})>".format(self.username, self.user_id)


class Chats(BASE):
    __tablename__ = "chats"
    chat_id = Column(String(14), primary_key=True)
    chat_name = Column(UnicodeText, nullable=False)
    username = Column(UnicodeText, default=None)

    def __init__(self, chat_id, chat_name, username=None):
        self.chat_id = str(chat_id)
        self.chat_name = chat_name
        self.username = username

    def __repr__(self):
        return "<Chat {} ({})>".format(self.chat_name, self.chat_id)


class ChatMembers(BASE):
    __tablename__ = "chat_members"
    priv_chat_id = Column(Integer, primary_key=True)
    # NOTE: Use dual primary key instead of private primary key?
    chat = Column(String(14),
                  ForeignKey("chats.chat_id",
                             onupdate="CASCADE",
                             ondelete="CASCADE"),
                  nullable=False)
    user = Column(BigInteger,
                  ForeignKey("users.user_id",
                             onupdate="CASCADE",
                             ondelete="CASCADE"),
                  nullable=False)
    __table_args__ = (UniqueConstraint('chat', 'user', name='_chat_members_uc'),)

    def __init__(self, chat, user):
        self.chat = chat
        self.user = user

    def __repr__(self):
        return "<Chat user {} ({}) in chat {} ({})>".format(self.user.username, self.user.user_id,
                                                            self.chat.chat_name, self.chat.chat_id)


Users.__table__.create(SESSION.bind, checkfirst=True)
Chats.__table__.create(SESSION.bind, checkfirst=True)
ChatMembers.__table__.create(SESSION.bind, checkfirst=True)


def __ensure_chat_username_column():
    """Add the ``username`` column to the ``chats`` table for existing DBs."""
    engine = SESSION.bind
    try:
        with engine.begin() as conn:
            conn.execute(text(
                'ALTER TABLE chats ADD COLUMN username VARCHAR'
            ))
        LOGGER.info("[users] added 'username' column to chats table")
    except Exception as err:
        LOGGER.info("[users] chats.username migrate skipped (%s)", err)

    try:
        from sqlalchemy import inspect
        cols = [c["name"] for c in inspect(engine).get_columns("chats")]
        if "username" not in cols:
            LOGGER.error("[users] 'username' column still missing after migration!")
    except Exception as err:
        LOGGER.info("[users] could not verify username column (%s)", err)


__ensure_chat_username_column()

INSERTION_LOCK = threading.RLock()


def ensure_bot_in_db():
    with INSERTION_LOCK:
        bot = Users(dispatcher.bot.id, dispatcher.bot.username)
        SESSION.merge(bot)
        SESSION.commit()


def update_user(user_id, username, chat_id=None, chat_name=None, chat_username=None):
    with INSERTION_LOCK:
        user = SESSION.query(Users).get(user_id)
        if not user:
            user = Users(user_id, username)
            SESSION.add(user)
            SESSION.flush()
        else:
            user.username = username

        if not chat_id or not chat_name:
            SESSION.commit()
            return

        chat = SESSION.query(Chats).get(str(chat_id))
        if not chat:
            chat = Chats(str(chat_id), chat_name, chat_username)
            SESSION.add(chat)
            SESSION.flush()

        else:
            chat.chat_name = chat_name
            if chat_username:
                chat.username = chat_username

        member = SESSION.query(ChatMembers).filter(ChatMembers.chat == chat.chat_id,
                                                   ChatMembers.user == user.user_id).first()
        if not member:
            chat_member = ChatMembers(chat.chat_id, user.user_id)
            SESSION.add(chat_member)

        SESSION.commit()


def get_userid_by_name(username):
    try:
        return SESSION.query(Users).filter(func.lower(Users.username) == username.lower()).all()
    finally:
        SESSION.close()


def get_name_by_userid(user_id):
    try:
        return SESSION.query(Users).get(int(user_id))
    finally:
        SESSION.close()


def get_chat_members(chat_id):
    try:
        return SESSION.query(ChatMembers).filter(ChatMembers.chat == str(chat_id)).all()
    finally:
        SESSION.close()


def get_all_chats():
    try:
        return SESSION.query(Chats).all()
    finally:
        SESSION.close()


def get_user_num_chats(user_id):
    try:
        return SESSION.query(ChatMembers).filter(ChatMembers.user == int(user_id)).count()
    finally:
        SESSION.close()


def get_user_com_chats(user_id):
    try:
        chat_members = SESSION.query(ChatMembers).filter(ChatMembers.user == int(user_id)).all()
        return [i.chat for i in chat_members]
    finally:
        SESSION.close()


def num_chats():
    try:
        return SESSION.query(Chats).count()
    finally:
        SESSION.close()


def num_users():
    try:
        return SESSION.query(Users).count()
    finally:
        SESSION.close()


def migrate_chat(old_chat_id, new_chat_id):
    with INSERTION_LOCK:
        chat = SESSION.query(Chats).get(str(old_chat_id))
        if chat:
            chat.chat_id = str(new_chat_id)
            SESSION.add(chat)

        SESSION.flush()

        chat_members = SESSION.query(ChatMembers).filter(ChatMembers.chat == str(old_chat_id)).all()
        for member in chat_members:
            member.chat = str(new_chat_id)
            SESSION.add(member)

        SESSION.commit()


ensure_bot_in_db()


def del_user(user_id):
    with INSERTION_LOCK:
        curr = SESSION.query(Users).get(user_id)
        if curr:
            SESSION.delete(curr)
            SESSION.commit()
            return True

        SESSION.query(ChatMembers).filter(ChatMembers.user == user_id).delete()
        SESSION.commit()
        SESSION.close()
    return False


def rem_chat(chat_id):
    with INSERTION_LOCK:
        chat = SESSION.query(Chats).get(str(chat_id))
        if chat:
            SESSION.delete(chat)
            SESSION.commit()
        else:
            SESSION.close()
