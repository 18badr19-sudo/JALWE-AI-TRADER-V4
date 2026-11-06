import copy
import unittest
from market_move_research import priority_link_report_lines, utc
from prior_day_priority_bus import END, load
class PriorityLinkReportTests(unittest.TestCase):
 def test_expired_saved_publication_is_history(self):
  link={'state':'PUBLISHED','session_day':'2026-10-29','eligible':106}; original=copy.deepcopy(link)
  text='\n'.join(priority_link_report_lines(link,utc('2026-11-06T14:00:00Z')))
  self.assertIn('انتهت التجربة',text);self.assertIn('آخر نشر ناجح محفوظ',text)
  self.assertIn('2026-10-29',text);self.assertIn('106',text);self.assertEqual(link,original)
 def test_expiry_exact_boundary(self):
  self.assertIn('انتهت التجربة',priority_link_report_lines({},utc(END))[0])
 def test_active_period_never_claims_delivery(self):
  text='\n'.join(priority_link_report_lines({'state':'PUBLISHED'},utc('2026-10-29T14:00:00Z')))
  self.assertIn('غير متحقق',text);self.assertNotIn('انتهت التجربة',text)
 def test_waiting_not_labeled_success(self):
  text='\n'.join(priority_link_report_lines({'state':'WAITING_PREOPEN_SNAPSHOT'},utc('2026-11-06T14:00:00Z')))
  self.assertIn('آخر حالة محفوظة',text);self.assertNotIn('آخر نشر ناجح',text)
 def test_reader_remains_expired(self):
  self.assertEqual(load(utc('2026-11-06T14:00:00Z'),paper=True)[1]['state'],'DISABLED_OR_EXPIRED')
