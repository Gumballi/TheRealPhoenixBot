import threading

from sqlalchemy import Column, Integer, String, UnicodeText

from tg_bot.modules.sql import SESSION, BASE


class WikiSettings(BASE):
    __tablename__ = "daily_wiki_settings"
    chat_id = Column(String(14), primary_key=True)
    time = Column(String(5), default="12:00")

    def __init__(self, chat_id, time="12:00"):
        self.chat_id = str(chat_id)
        self.time = time


WikiSettings.__table__.create(checkfirst=True)
INSERTION_LOCK = threading.RLock()

WIKI_CHATS = {}


def set_chat(chat_id, time="12:00"):
    with INSERTION_LOCK:
        row = SESSION.query(WikiSettings).get(str(chat_id))

        if row:
            row.time = time
        else:
            row = WikiSettings(str(chat_id), time)

        SESSION.merge(row)
        SESSION.commit()
        WIKI_CHATS[str(chat_id)] = time
        return True


def rem_chat(chat_id):
    with INSERTION_LOCK:
        row = SESSION.query(WikiSettings).get(str(chat_id))
        if row:
            SESSION.delete(row)
            SESSION.commit()
            WIKI_CHATS.pop(str(chat_id), None)
            return True

        SESSION.close()
        return False


def get_time(chat_id):
    return WIKI_CHATS.get(str(chat_id))


def get_all_chats():
    return list(WIKI_CHATS.keys())


def num_chats():
    return len(WIKI_CHATS)


def migrate_chat(old_chat_id, new_chat_id):
    with INSERTION_LOCK:
        row = SESSION.query(WikiSettings).get(str(old_chat_id))
        if row:
            row.chat_id = str(new_chat_id)
            SESSION.commit()
            WIKI_CHATS[str(new_chat_id)] = WIKI_CHATS.pop(str(old_chat_id))


def __load_wiki_chats():
    global WIKI_CHATS
    try:
        all_rows = SESSION.query(WikiSettings).all()
        for row in all_rows:
            WIKI_CHATS[row.chat_id] = row.time
    finally:
        SESSION.close()


__load_wiki_chats()
