import sqlite3
import unittest
from bot import Bot, ROOT, load_config


class FakeAPI:
    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        return {}


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.db = sqlite3.connect(':memory:')
        self.cfg = load_config()
        self.cfg.update(legal_ready=True, operator_name='Test Operator', contact_email='test@example.com')
        self.bot = Bot(self.api, self.db, self.cfg)

    def tearDown(self):
        self.db.close()

    def message(self, text, chat_type='private'):
        self.bot.handle({'message': {'chat': {'id': 123, 'type': chat_type}, 'text': text}})

    def click(self, data):
        self.bot.handle({'callback_query': {'id': 'test', 'from': {'id': 123}, 'data': data,
            'message': {'chat': {'id': 123, 'type': 'private'}}}})

    def consent(self):
        self.click('yes:' + self.bot.revision)

    def test_no_storage_before_consent_or_on_refusal(self):
        self.message('/start source_campaign')
        self.click('doc:consent')
        self.click('no')
        self.assertIsNone(self.bot.row(123))
        self.assertEqual(self.db.execute('SELECT count(*) FROM consents').fetchone()[0], 0)

    def test_consent_and_lesson_without_marketing(self):
        self.consent()
        self.click('lesson')
        self.assertEqual(self.bot.row(123)[1:], (1, 0))
        self.assertTrue(any('ИИ-бандит' in p.get('text', '') for _, p in self.api.calls))
        self.assertEqual(self.db.execute('SELECT kind FROM consents').fetchall(), [('personal_data',)])

    def test_optional_marketing_and_unsubscribe(self):
        self.consent()
        self.click('ads:' + self.bot.revision)
        self.assertEqual(self.bot.row(123)[1:], (1, 1))
        self.message('/unsubscribe')
        self.assertEqual(self.bot.row(123)[1:], (1, 0))

    def test_delete_erases_subscriber_and_consent(self):
        self.consent()
        self.message('/delete')
        self.assertIsNone(self.bot.row(123))
        self.assertEqual(self.db.execute('SELECT count(*) FROM consents').fetchone()[0], 0)
        self.click('ads:' + self.bot.revision)
        self.assertIsNone(self.bot.row(123))

    def test_repeated_consent_is_idempotent(self):
        self.consent()
        self.consent()
        self.assertEqual(self.db.execute('SELECT count(*) FROM consents').fetchone()[0], 1)

    def test_stale_consent_is_rejected(self):
        self.click('yes:old')
        self.assertIsNone(self.bot.row(123))

    def test_stopped_user_requires_fresh_consent(self):
        self.consent()
        self.message('/stop')
        self.message('/start')
        self.assertEqual(self.bot.row(123)[1:], (0, 0))
        self.consent()
        self.assertEqual(self.bot.row(123)[1:], (1, 0))

    def test_expiration(self):
        self.consent()
        with self.db:
            self.db.execute("UPDATE subscribers SET updated_at='2020-01-01T00:00:00+00:00'")
        self.bot.purge_expired()
        self.assertIsNone(self.bot.row(123))
        self.assertEqual(self.db.execute('SELECT count(*) FROM consents').fetchone()[0], 0)

    def test_groups_ignored(self):
        self.message('/start', 'group')
        self.assertEqual(self.api.calls, [])

    def test_unfinished_documents_disable_registration(self):
        self.bot.config['legal_ready'] = False
        self.consent()
        self.assertIsNone(self.bot.row(123))

    def test_telegram_message_limits(self):
        for text in [*self.bot.docs.values(), self.bot.welcome]:
            self.assertLess(len(text), 4096)

    def test_no_address_required_in_documents(self):
        self.assertNotIn('operator_address', self.cfg)
        self.assertNotIn('НЕ ЗАПОЛНЕН', self.bot.docs['consent'])
        self.assertIn('test@example.com', self.bot.docs['consent'])

    def test_restart_retains_subscription(self):
        self.consent()
        restarted = Bot(self.api, self.db, self.cfg)
        self.assertEqual(restarted.row(123)[1:], (1, 0))


if __name__ == '__main__':
    unittest.main()
