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
