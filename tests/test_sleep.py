"""Sleep values persist through create, edit, copy, recovery and search."""
import unittest

import test_family as family
import test_health as baseline
from storage import get_db


class SleepTests(unittest.TestCase):
    setUp = family.FamilyTests.setUp
    get = family.FamilyTests.get
    post = family.FamilyTests.post
    login = family.FamilyTests.login
    member = family.FamilyTests.member
    form = family.FamilyTests.form

    def test_sleep_record_lifecycle(self):
        self.login()
        self.member()
        self.assertEqual(self.post('/record/new', self.form(sleep_time='21:30–07:00 夜醒两次')).status_code, 303)
        self.assertIn('21:30–07:00 夜醒两次', self.get().get_data(as_text=True))
        self.assertIn('21:30–07:00 夜醒两次', self.get('/record/new?copy=last').get_data(as_text=True))
        self.assertIn('21:30–07:00 夜醒两次', self.get('/history?view=list&q=夜醒两次').get_data(as_text=True))
        edit = self.form(csrf=baseline.csrf(self.get('/record/1/edit')), version='1', sleep_time='22:00–08:00')
        self.assertEqual(self.post('/record/1/edit', edit).status_code, 303)
        with self.app.app_context():
            self.assertEqual(get_db().execute('SELECT sleep_time FROM records').fetchone()[0], '22:00–08:00')
        old_form = self.form(csrf=baseline.csrf(self.get('/record/1/edit')), version='2')
        self.assertEqual(self.post('/record/1/edit', old_form).status_code, 303)
        with self.app.app_context():
            self.assertEqual(get_db().execute('SELECT sleep_time FROM records').fetchone()[0], '22:00–08:00')
        bad = self.form(csrf='bad', sleep_time='需要恢复的睡眠')
        self.assertIn('需要恢复的睡眠', self.post('/record/new', bad).get_data(as_text=True))

    def test_clock_picker_and_legacy_preservation(self):
        self.login()
        self.member()
        clocks = dict(sleep_picker='1', sleep_start='21:30', sleep_end='07:00')
        self.assertEqual(self.post('/record/new', self.form(**clocks)).status_code, 303)
        with self.app.app_context():
            self.assertEqual(get_db().execute('SELECT sleep_time FROM records WHERE id=1').fetchone()[0], '21:30–07:00')
        page = self.get('/record/new?copy=1').get_data(as_text=True)
        self.assertIn('type="time" name="sleep_start" step="60" value="21:30"', page)
        self.assertIn('name="sleep_end" step="60" value="07:00"', page)
        bad = self.form(sleep_picker='1', sleep_start='invalid', sleep_end='07:00')
        response = self.post('/record/new', bad)
        self.assertEqual(response.status_code, 400)
        self.assertIn('value="07:00"', response.get_data(as_text=True))
        self.assertIn('有效的入睡或醒来时间', response.get_data(as_text=True))
        self.assertEqual(self.post('/record/new', self.form(sleep_time='夜醒两次')).status_code, 303)
        html = self.get('/record/2/edit').get_data(as_text=True)
        self.assertIn('原有睡眠记录', html)
        self.assertIn('夜醒两次', html)
        edit = self.form(csrf=baseline.csrf(self.get('/record/2/edit')), version='1', sleep_picker='1', sleep_legacy='夜醒两次', sleep_start='22:00', sleep_end='08:00')
        self.assertEqual(self.post('/record/2/edit', edit).status_code, 303)
        with self.app.app_context():
            self.assertEqual(get_db().execute('SELECT sleep_time FROM records WHERE id=2').fetchone()[0], '夜醒两次')
        edit.update(csrf=baseline.csrf(self.get('/record/2/edit')), version='2', replace_sleep='1')
        self.assertEqual(self.post('/record/2/edit', edit).status_code, 303)
        with self.app.app_context():
            self.assertEqual(get_db().execute('SELECT sleep_time FROM records WHERE id=2').fetchone()[0], '22:00–08:00')
        recovery = self.form(csrf='bad', **clocks)
        self.assertIn('21:30–07:00', self.post('/record/new', recovery).get_data(as_text=True))

    def test_report_and_optional_clocks(self):
        self.assertEqual(self.get('/sleep-report').status_code, 302)
        self.login()
        self.member()
        self.assertEqual(self.post('/record/new', self.form(sleep_picker='1', sleep_start='21:45', sleep_end='')).status_code, 303)
        self.assertEqual(self.post('/record/new', self.form(sleep_picker='1', sleep_start='22:00', sleep_end='07:00')).status_code, 303)
        self.assertEqual(self.post('/record/new', self.form(sleep_picker='1', sleep_start='', sleep_end='')).status_code, 303)
        self.assertEqual(self.post('/record/new', self.form(sleep_picker='1', sleep_start='', sleep_end='08:00')).status_code, 303)
        with self.app.app_context():
            values = [r[0] for r in get_db().execute('SELECT sleep_time FROM records ORDER BY id')]
        self.assertEqual(values, ['入睡 21:45', '22:00–07:00', '', '醒来 08:00'])
        html = self.get('/sleep-report?start=2026-11-02&end=2026-11-02&member=1').get_data(as_text=True)
        self.assertIn('21:45', html)
        self.assertIn('22:00', html)
        self.assertIn('9 小时 0 分', html)
        self.assertIn('<strong>3</strong><span>条睡眠记录</span>', html)
        self.assertIn('value="21:45"', self.get('/record/new?copy=1').get_data(as_text=True))
        self.assertIn('开始日期不能晚于结束日期', self.get('/sleep-report?start=2026-11-03&end=2026-11-02').get_data(as_text=True))
        self.assertNotIn('21:45', self.get('/sleep-report?start=2026-11-03&end=2026-11-04').get_data(as_text=True))
