"""АГЕНТ001: consent-first welcome bot. No automatic marketing broadcasts."""
import hashlib
import html
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("agent001")


def now():
    return datetime.now(timezone.utc).isoformat()


def load_config():
    path = ROOT / "config.json"
    cfg = json.loads((path if path.exists() else ROOT / "config.example.json").read_text("utf-8-sig"))
    for field in cfg:
        value = os.environ.get(field.upper())
        if value is not None:
            cfg[field] = value.lower() == "true" if field == "legal_ready" else value
    return cfg


def documents(c):
    name = c["operator_name"]
    contact = c["contact_email"]
    operator = f'{name}. {c["operator_status"]}. Email для обращений и отзыва согласия: {contact}.'
    return {
        "policy": f'''ПОЛИТИКА ОБРАБОТКИ ПЕРСОНАЛЬНЫХ ДАННЫХ
Бот «АГЕНТ001». Версия {c["documents_version"]}.

Оператор: {operator}

Цель обработки: предоставить запрошенные материалы и обеспечить работу подписки в боте. При отдельном согласии — направлять информационные и рекламные сообщения о материалах, обучении и курсе «АГЕНТ001».

Данные: Telegram ID и ID личного чата; дата, время, содержание и версия согласий; статус подписки. Имя профиля, username, номер телефона и содержание произвольных сообщений в базу бота не записываются.

Основание: согласие пользователя. Обработка автоматизированная: сбор, запись, систематизация, накопление, хранение, уточнение, извлечение, использование, блокирование, удаление и уничтожение. Публикация данных и продажа третьим лицам не предусмотрены. Для доставки сообщений используется Telegram; его собственная обработка регулируется его документами. Для размещения бота используется Bothost, локация Россия. База подписчиков хранится отдельно от исходного кода в постоянном каталоге сервера.

Данные хранятся до отзыва согласия или достижения цели, но не более 12 месяцев с последнего действия пользователя в боте. После этого удаляются из активной базы. При наличии резервных копий запрос на удаление распространяется и на них; для контроля исполнения можно обратиться к оператору по указанному email.

Пользователь может запросить сведения об обработке, исправление или удаление данных, отозвать согласие. Команда /delete удаляет запись пользователя и журнал его согласий из активной базы, /stop отключает сообщения, /unsubscribe отключает рекламную подписку. Обращения: {contact}. Отзыв согласия прекращает обработку на его основании, кроме случаев, когда закон допускает другое основание.

Доступ к базе ограничивается оператором и уполномоченными им лицами. Ключ бота хранится отдельно от исходного кода. Сведения о пользователях не включаются в технические логи.
''',
        "consent": f'''СОГЛАСИЕ НА ОБРАБОТКУ ПЕРСОНАЛЬНЫХ ДАННЫХ
Версия {c["documents_version"]}.

Нажимая кнопку «Даю согласие», я свободно, своей волей и в своём интересе даю согласие оператору: {operator}

Цель: обеспечение работы бота «АГЕНТ001», сохранение моей заявки на бесплатный урок и предоставление запрошенных материалов.

Перечень данных: Telegram ID, ID личного чата, дата и время согласия, версия и содержание согласия, статус подписки.

Разрешённые действия с использованием средств автоматизации: сбор, запись, систематизация, накопление, хранение, уточнение, извлечение, использование, блокирование, удаление, уничтожение. Доставка сообщений осуществляется через Telegram.

Согласие действует до достижения цели, отзыва согласия или истечения 12 месяцев с последнего действия в боте — в зависимости от того, что наступит раньше. Отзыв: /delete в боте или письмо на {contact}. После /delete данные удаляются из активной базы. Обработка на иных законных основаниях допускается в предусмотренных законом случаях.

Это согласие не включает согласие на рекламные рассылки: оно запрашивается отдельно.
''',
        "marketing": f'''СОГЛАСИЕ НА ИНФОРМАЦИОННЫЕ И РЕКЛАМНЫЕ СООБЩЕНИЯ
Версия {c["documents_version"]}.

Нажимая «Хочу новости курса», я соглашаюсь получать от {name} в этом Telegram-боте информационные и рекламные сообщения о курсе «АГЕНТ001», обучении и материалах по нейросетям. Для этого разрешаю использовать мой Telegram ID и ID личного чата, хранить дату и время согласия, его содержание и статус подписки.

Согласие добровольное, отказ не ограничивает доступ к запрошенному бесплатному уроку. Действует до отзыва или удаления данных в соответствии с политикой. Отписка: /unsubscribe или /stop в боте; обращение: {contact}.
'''
    }


class ApiError(Exception):
    def __init__(self, code, retry_after=0):
        self.code, self.retry_after = code, retry_after
        super().__init__(f"Telegram API error {code}")


class Telegram:
    def __init__(self, token):
        self.base = "https://api.telegram.org/bot" + token + "/"

    def call(self, method, **params):
        request = urllib.request.Request(self.base + method,
            data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                payload = json.load(error)
            except Exception:
                payload = {}
            raise ApiError(error.code, payload.get("parameters", {}).get("retry_after", 0)) from None
        except (OSError, ValueError):
            raise ApiError(0) from None
        if not result.get("ok"):
            raise ApiError(result.get("error_code", 0))
        return result["result"]


def keyboard(*rows):
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


class Bot:
    def __init__(self, api, db, config):
        self.api, self.db, self.config = api, db, config
        self.docs = documents(config)
        self.revision = hashlib.sha256(json.dumps(self.docs, sort_keys=True).encode()).hexdigest()[:12]
        self.welcome = (ROOT / "welcome.html").read_text("utf-8").strip()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY, consent_revision TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, marketing INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS consents (
                id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
                kind TEXT NOT NULL, accepted_at TEXT NOT NULL, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')

    def send(self, chat, text, markup=None):
        args = dict(chat_id=chat, text=text, parse_mode="HTML", link_preview_options={"is_disabled": True})
        if markup:
            args["reply_markup"] = markup
        return self.api.call("sendMessage", **args)

    def row(self, chat):
        return self.db.execute("SELECT consent_revision, active, marketing FROM subscribers WHERE chat_id=?", (chat,)).fetchone()

    def gate(self, chat):
        self.send(chat, "Перед началом 👇\n\nЧтобы пользоваться ботом и получить запрошенный урок, ознакомься с политикой и отдельным согласием на обработку данных.\n\nНажимая «Даю согласие», ты принимаешь условия документа «Согласие на обработку данных».\n\nОтозвать согласие и удалить данные можно командой /delete.", keyboard(
            [("Политика обработки данных", "doc:policy")],
            [("Согласие на обработку данных", "doc:consent")],
            [("Даю согласие", "yes:" + self.revision), ("Не согласен", "no")]))

    def welcome_message(self, chat):
        self.send(chat, self.welcome, {"inline_keyboard": [[{"text": "Забрать инструкции в канале", "url": self.config["channel_url"]}]]})

    def marketing_prompt(self, chat):
        self.send(chat, "Хочешь получать здесь ещё и новости, материалы и предложения по курсу АГЕНТ001?\n\nЭто отдельная добровольная подписка. Бесплатный урок пришлю независимо от твоего выбора.", keyboard(
            [("Условия подписки", "doc:marketing")],
            [("Хочу новости курса", "ads:" + self.revision)], [("Только бесплатный урок", "lesson")]))

    def handle(self, update):
        callback = update.get("callback_query")
        message = (callback.get("message") if callback else update.get("message")) or {}
        if message.get("chat", {}).get("type") != "private":
            return
        chat = message["chat"]["id"]
        if callback and callback.get("from", {}).get("id") != chat:
            return
        if callback:
            try:
                self.api.call("answerCallbackQuery", callback_query_id=callback["id"])
            except ApiError as error:
                if error.code != 400:
                    raise
        raw = message.get("text", "").split()
        command = raw[0].split("@")[0] if raw else ""
        data = callback.get("data", "") if callback else ""
        if not callback and command == "/delete":
            with self.db:
                self.db.execute("DELETE FROM consents WHERE chat_id=?", (chat,))
                self.db.execute("DELETE FROM subscribers WHERE chat_id=?", (chat,))
            self.send(chat, "Твои данные удалены из базы бота. Подписки отключены. Историю чата в Telegram можно удалить самостоятельно. Вернуться: /start")
            return
        if not callback and command in ("/stop", "/unsubscribe"):
            with self.db:
                self.db.execute("UPDATE subscribers SET marketing=0, active=CASE WHEN ?='/stop' THEN 0 ELSE active END, updated_at=? WHERE chat_id=?", (command, now(), chat))
                if self.row(chat):
                    self.db.execute("INSERT INTO consents(chat_id, kind, accepted_at, document) VALUES(?,?,?,?)", (chat, "withdraw", now(), command))
            self.send(chat, "Все сообщения отключены. Вернуться: /start" if command == "/stop" else "Новости и предложения курса отключены. Запрошенный бесплатный урок ты по-прежнему получишь.")
            return
        if data.startswith("doc:") or (not callback and command == "/privacy"):
            kind = data[4:] if callback else "policy"
            if kind in self.docs:
                self.send(chat, html.escape(self.docs[kind]))
            return
        if data == "no":
            self.send(chat, "Окей, подписку не оформляю. Если передумаешь, нажми /start.")
            return
        if not self.config.get("legal_ready"):
            self.send(chat, "Бот готовится к запуску. Скоро здесь появится запись на бесплатный урок. Пока загляни в канал 👇", {"inline_keyboard": [[{"text": "Перейти в канал", "url": self.config["channel_url"]}]]})
            return
        current = self.row(chat)
        if data.startswith("yes:"):
            if data != "yes:" + self.revision:
                self.gate(chat)
                return
            with self.db:
                if not current or current[0] != self.revision or not current[1]:
                    timestamp = now()
                    self.db.execute("INSERT INTO subscribers VALUES(?,?,?,?,1,0) ON CONFLICT(chat_id) DO UPDATE SET consent_revision=excluded.consent_revision, active=1, marketing=0, updated_at=excluded.updated_at", (chat, self.revision, timestamp, timestamp))
                    self.db.execute("INSERT INTO consents(chat_id,kind,accepted_at,document) VALUES(?,?,?,?)", (chat, "personal_data", timestamp, self.docs["consent"]))
            self.welcome_message(chat)
            if not self.row(chat)[2]:
                self.marketing_prompt(chat)
            return
        if not current or current[0] != self.revision or not current[1]:
            self.gate(chat)
            return
        with self.db:
            self.db.execute("UPDATE subscribers SET updated_at=? WHERE chat_id=?", (now(), chat))
        if data.startswith("ads:"):
            if data != "ads:" + self.revision:
                self.marketing_prompt(chat)
                return
            with self.db:
                if not current[2]:
                    self.db.execute("UPDATE subscribers SET marketing=1 WHERE chat_id=?", (chat,))
                    self.db.execute("INSERT INTO consents(chat_id,kind,accepted_at,document) VALUES(?,?,?,?)", (chat, "marketing", now(), self.docs["marketing"]))
            self.send(chat, "Готово 😎 Пришлю урок и новости курса сюда. Отписаться от новостей: /unsubscribe")
        elif data == "lesson":
            with self.db:
                self.db.execute("UPDATE subscribers SET marketing=0 WHERE chat_id=?", (chat,))
            self.send(chat, "Договорились! Пришлю сюда бесплатный урок, когда он будет готов.")
        elif command == "/subscribe":
            self.marketing_prompt(chat)
        else:
            self.welcome_message(chat)

    def purge_expired(self):
        with self.db:
            self.db.execute("DELETE FROM consents WHERE chat_id IN (SELECT chat_id FROM subscribers WHERE datetime(updated_at) <= datetime('now','-12 months'))")
            self.db.execute("DELETE FROM subscribers WHERE datetime(updated_at) <= datetime('now','-12 months')")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        raise SystemExit("Set BOT_TOKEN in the environment.")
    if config["legal_ready"] and not all(config.get(k) for k in ("operator_name", "contact_email")):
        raise SystemExit("Complete operator details before enabling LEGAL_READY.")
    api = Telegram(token)
    while True:
        try:
            info = api.call("getMe")
            webhook = api.call("getWebhookInfo")
            break
        except ApiError as error:
            if error.code == 401:
                raise SystemExit("Invalid bot token.") from None
            log.warning("Waiting for Telegram connection; code=%s", error.code)
            time.sleep(max(5, min(error.retry_after, 300)))
    if webhook.get("url"):
        raise SystemExit("An existing webhook is configured. No changes made.")
    data_dir = Path(os.environ.get("DATA_DIR", ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data_dir / "subscribers.db")
    db.execute("PRAGMA secure_delete=ON")
    bot = Bot(api, db, config)
    bot.purge_expired()
    last = db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
    offset = int(last[0]) if last else 0
    log.info("Bot @%s started; subscription enabled=%s", info["username"], config["legal_ready"])
    purged = time.monotonic()
    while True:
        try:
            for update in api.call("getUpdates", offset=offset, timeout=25, allowed_updates=["message", "callback_query"]):
                try:
                    bot.handle(update)
                except ApiError as error:
                    if error.code != 403:
                        raise
                    msg = update.get("message") or update.get("callback_query", {}).get("message", {})
                    with db:
                        db.execute("UPDATE subscribers SET active=0,marketing=0 WHERE chat_id=?", (msg.get("chat", {}).get("id"),))
                offset = update["update_id"] + 1
                with db:
                    db.execute("INSERT OR REPLACE INTO meta VALUES('offset',?)", (str(offset),))
            if time.monotonic() - purged > 3600:
                bot.purge_expired()
                purged = time.monotonic()
        except ApiError as error:
            if error.code in (401, 409):
                raise SystemExit(f"Bot stopped: API code {error.code}. Check token / competing instance.") from None
            log.warning("Temporary API failure, code=%s", error.code)
            time.sleep(max(5, min(error.retry_after, 300)))


if __name__ == "__main__":
    main()
