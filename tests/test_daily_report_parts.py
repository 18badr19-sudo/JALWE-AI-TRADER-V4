import unittest
from unittest.mock import patch

from core.telegram_text import telegram_report_parts
import railway_controller as controller


class DailyReportPartsTests(unittest.TestCase):
    split = staticmethod(telegram_report_parts)

    def test_long_report_preserved_and_all_parts_fit_telegram_units(self):
        message = '🎯 الأسباب\n' * 1000
        parts = self.split(message)
        self.assertGreater(len(parts), 1)
        self.assertEqual(''.join(parts), message)
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 3800 for p in parts))

    def test_short_report_stays_one_message(self):
        self.assertEqual(self.split('تقرير اليوم'), ['تقرير اليوم'])
        self.assertEqual(self.split(''), [''])

    def test_controller_preserves_entire_report_and_markup(self):
        message = '🔬 عوامل القفزات\n' * 600 + '🧠 تعلم الأنماط: يجمع بيانات تدريب مستقبلية\nلا تطبيق تلقائي.'
        markup = {'inline_keyboard': [[{'text': 'عرض', 'callback_data': 'view'}]]}
        with patch.object(controller, 'TELEGRAM_CHAT_ID', 'test'), \
             patch.object(controller, 'tg_call') as tg:
            self.assertTrue(controller.send_message({'text': message, 'reply_markup': markup}))
        payloads = [c.args[1] for c in tg.call_args_list]
        self.assertGreater(len(payloads), 1)
        self.assertEqual(''.join(p['text'] for p in payloads), message)
        self.assertTrue(all(len(p['text'].encode('utf-16-le'))//2 <= 3800 for p in payloads))
        import json
        self.assertTrue(all(json.loads(p['reply_markup']) == markup for p in payloads))

    def test_auto_report_only_marks_sent_after_all_parts_and_retries_failed_delivery(self):
        packet = dict(packet_id='one', health=dict(state='READY', completed_session='2026-10-14',
                                                   completed_session_forward_pending=0))
        message = 'تقرير\n' * 800 + 'تعلم الأنماط والقيود البحثية'
        state = {}
        with patch('market_move_research.read_packet', return_value=packet), \
             patch('market_move_research.report_text', return_value=message), \
             patch.object(controller, 'TELEGRAM_CHAT_ID', 'test'), \
             patch.object(controller, 'controller_state', state), \
             patch.object(controller, 'AUTO_DAILY_REPORT', True), \
             patch.object(controller, 'save_state'), patch('builtins.print'), \
             patch.object(controller, 'tg_call', side_effect=[{'ok': True}, RuntimeError('offline')]) as tg:
            controller.maybe_send_move_research_report()
            self.assertEqual(tg.call_count, 2)
            self.assertNotIn('last_move_report_session', state)
            tg.side_effect = None
            tg.reset_mock()
            controller.maybe_send_move_research_report()
            self.assertEqual(''.join(c.args[1]['text'] for c in tg.call_args_list), message)
            self.assertEqual(state['last_move_report_session'], '2026-10-14')
            controller.maybe_send_move_research_report()
            self.assertEqual(tg.call_count, 2)

    def test_unconfigured_chat_cannot_mark_research_report_sent(self):
        packet = dict(packet_id='one', health=dict(state='READY', completed_session='2026-10-14',
                                                   completed_session_forward_pending=0))
        state = {}
        with patch('market_move_research.read_packet', return_value=packet), \
             patch('market_move_research.report_text', return_value='research'), \
             patch.object(controller, 'TELEGRAM_CHAT_ID', ''), \
             patch.object(controller, 'controller_state', state), \
             patch.object(controller, 'AUTO_DAILY_REPORT', True), \
             patch.object(controller, 'save_state'), patch.object(controller, 'tg_call') as tg:
            controller.maybe_send_move_research_report()
        tg.assert_not_called()
        self.assertNotIn('last_move_report_session', state)
