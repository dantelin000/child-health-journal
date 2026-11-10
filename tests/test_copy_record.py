"""Copying prepares a review form and never alters or saves the source record."""
import io
import unittest

import test_family as family
import test_health as baseline

from storage import get_db


class CopyTests(unittest.TestCase):
    setUp = family.FamilyTests.setUp
    get = family.FamilyTests.get
    post = family.FamilyTests.post
    login = family.FamilyTests.login
    member = family.FamilyTests.member
    form = family.FamilyTests.form
    counts = family.FamilyTests.counts

    def test_copy_review_and_explicit_save(self):
        self.login()
        self.member()
        form = self.form(temperature="38.2", symptoms="咳嗽\n晚上开始", medication="旧药量",
                         care="旧护理", notes="旧备注", documents=(io.BytesIO(family.PDF), "old.pdf"))
        self.assertEqual(self.post('/record/new', form).status_code, 303)
        before = self.counts()
        self.assertIn('复制上一条', self.get().get_data(as_text=True))
        html = self.get('/record/new?copy=last').get_data(as_text=True)
        self.assertEqual(self.counts(), before)
        self.assertIn('检查并保存新记录', html)
        self.assertIn('病例单不会复制', html)
        self.assertIn('value="cough" checked', html)
        self.assertIn('value="38.2"', html)
        self.assertIn('旧药量', html)
        self.assertNotIn('value="2026-11-02T10:30"', html)
        self.assertNotIn('old.pdf', html)
        new = self.form(csrf=baseline.csrf(self.get('/record/new?copy=1')), symptom_picker='1',
                        symptom_choices=['cough'], symptoms='今天好转', temperature='37',
                        medication='调整后', occurred='2026-11-03T11:00')
        self.assertEqual(self.post('/record/new?copy=1', new).status_code, 303)
        self.assertEqual(self.counts(), (1, 2, 1))
        with self.app.app_context():
            rows = get_db().execute('SELECT symptoms,temperature,medication FROM records ORDER BY id').fetchall()
            self.assertEqual(tuple(rows[0]), ('咳嗽\n晚上开始', '38.2', '旧药量'))
            self.assertEqual(tuple(rows[1]), ('咳嗽\n今天好转', '37', '调整后'))

    def test_choice_and_access_fail_safely(self):
        self.assertEqual(self.get('/record/new?copy=last').status_code, 302)
        self.login()
        for query in ['last', '0', '999999', 'x', '1%20OR%201=1']:
            self.assertEqual(self.get('/record/new?copy=' + query).status_code, 404)
        self.member()
        self.assertEqual(self.post('/record/new', self.form()).status_code, 303)
        self.assertEqual(self.post('/record/new?copy=1', {'csrf': 'bad'}).status_code, 400)
        self.assertEqual(self.counts(), (1, 1, 0))

    def test_copy_specific_record_and_legacy_requires_selection(self):
        self.login()
        self.member()
        self.assertEqual(self.post('/record/new', self.form(symptoms='第一条')).status_code, 303)
        self.assertEqual(self.post('/record/new', self.form(symptoms='第二条', occurred='2026-11-04T10:30')).status_code, 303)
        self.assertIn('第二条', self.get('/record/new?copy=last').get_data(as_text=True))
        self.assertIn('第一条', self.get('/record/new?copy=1').get_data(as_text=True))
        with self.app.app_context(), get_db() as db:
            db.execute('UPDATE records SET member_id=NULL WHERE id=1')
        html = self.get('/record/new?copy=1').get_data(as_text=True)
        self.assertIn('请选择家人', html)
        self.assertNotIn('value="1" selected', html)
