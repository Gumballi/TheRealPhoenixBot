import logging
import threading

from sqlalchemy import Column, Integer, String, UnicodeText, inspect, text

from tg_bot.modules.sql import SESSION, BASE

LOGGER = logging.getLogger(__name__)


class WikiSettings(BASE):
    __tablename__ = "daily_wiki_settings"
    chat_id = Column(String(14), primary_key=True)
    time = Column(String(5), default="12:00")
    offset = Column(String(10), default="0")

    def __init__(self, chat_id, time="12:00", offset="0"):
        self.chat_id = str(chat_id)
        self.time = time
        self.offset = offset


WikiSettings.__table__.create(checkfirst=True)


def __ensure_offset_column():
    """Add the ``offset`` column for DBs created before it existed.

    ``offset`` is a reserved word in Postgres/SQLite, so it must be quoted in
    raw SQL.  If the column already exists the ALTER will fail on a duplicate
    column, which is fine -- we verify existence afterwards.
    """
    engine = SESSION.bind
    try:
        with engine.begin() as conn:
            conn.execute(text(
                'ALTER TABLE daily_wiki_settings ADD COLUMN "offset" VARCHAR(10) DEFAULT \'0\''
            ))
        LOGGER.info("[wiki] added 'offset' column to daily_wiki_settings")
    except Exception as err:
        LOGGER.info("[wiki] offset column migrate skipped (%s)", err)

    # Verify the column actually exists now (fresh DBs get it via create()).
    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("daily_wiki_settings")]
        if "offset" not in cols:
            LOGGER.error("[wiki] 'offset' column still missing after migration!")
    except Exception as err:
        LOGGER.info("[wiki] could not verify offset column (%s)", err)


__ensure_offset_column()

INSERTION_LOCK = threading.RLock()

# chat_id -> (time, offset_minutes)
WIKI_CHATS = {}


def set_chat(chat_id, time="12:00", offset="0"):
    with INSERTION_LOCK:
        row = SESSION.query(WikiSettings).get(str(chat_id))

        if row:
            row.time = time
            row.offset = offset
        else:
            row = WikiSettings(str(chat_id), time, offset)

        SESSION.merge(row)
        SESSION.commit()
        WIKI_CHATS[str(chat_id)] = (time, int(offset))
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
    return WIKI_CHATS.get(str(chat_id), (None, 0))[0]


def get_offset(chat_id):
    return WIKI_CHATS.get(str(chat_id), (None, 0))[1]


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
            offset = 0
            try:
                offset = int(row.offset or 0)
            except (TypeError, ValueError):
                offset = 0
            WIKI_CHATS[row.chat_id] = (row.time, offset)
    except Exception as err:
        LOGGER.error("[wiki] failed to load wiki chats: %s", err)
    finally:
        SESSION.close()


__load_wiki_chats()