"""Synthetic symptom selections survive saves, edits and recovery without JavaScript."""

import unittest

import test_family as family

from storage import get_db


class SymptomTests(unittest.TestCase):
    setUp = family.FamilyTests.setUp
    get = family.FamilyTests.get
    post = family.FamilyTests.post
    login = family.FamilyTests.login
    member = family.FamilyTests.member
    form = family.FamilyTests.form

    def saved_symptoms(self):
        with self.app.app_context():
            return get_db().execute("SELECT symptoms FROM records WHERE id=1").fetchone()[0]

    def test_multiple_choices_save_edit_remove_and_search(self):
        self.login()
        self.member()
        data = self.form(symptom_picker="1", symptom_choices=["cough", "fever_38", "cough"],
                         symptoms="夜里开始，<script>synthetic</script>")
        self.assertEqual(self.post("/record/new", data).status_code, 303)
        self.assertEqual(self.saved_symptoms(), "发热 38°C、咳嗽\n夜里开始，<script>synthetic</script>")
        html = self.get("/record/1/edit").get_data(as_text=True)
        self.assertIn('value="cough" checked', html)
        self.assertIn('value="fever_38" checked', html)
        self.assertNotIn('value="vomiting" checked', html)
        self.assertIn("&lt;script&gt;synthetic&lt;/script&gt;", html)
        self.assertIn("夜里开始", self.get("/history?q=咳嗽&view=list").get_data(as_text=True))
        data.update(version="1", symptom_choices=["vomiting"], symptoms="继续观察")
        self.assertEqual(self.post("/record/1/edit", data).status_code, 303)
        self.assertEqual(self.saved_symptoms(), "呕吐\n继续观察")
        data.update(version="2", symptom_choices=[], symptoms="")
        self.assertEqual(self.post("/record/1/edit", data).status_code, 303)
        self.assertEqual(self.saved_symptoms(), "")

    def test_validation_and_csrf_recovery_keep_choices_and_details(self):
        self.login()
        self.member()
        data = self.form(symptom_picker="1", symptom_choices=["runny_nose", "cough"],
                         symptoms="额外细节", occurred="invalid")
        response = self.post("/record/new", data)
        self.assertEqual(response.status_code, 400)
        html = response.get_data(as_text=True)
        self.assertIn('value="runny_nose" checked', html)
        self.assertIn('value="cough" checked', html)
        self.assertIn("额外细节", html)
        data["csrf"] = "invalid"
        recovery = self.post("/record/new", data).get_data(as_text=True)
        self.assertIn("流鼻涕、咳嗽\n额外细节", recovery)

    def test_old_forms_and_free_text_are_preserved(self):
        self.login()
        self.member()
        data = self.form(symptoms="咳嗽加重\n低烧待观察")
        self.assertEqual(self.post("/record/new", data).status_code, 303)
        html = self.get("/record/1/edit").get_data(as_text=True)
        self.assertNotIn('value="cough" checked', html)
        self.assertIn("咳嗽加重\n低烧待观察", html)
        self.assertEqual(self.saved_symptoms(), "咳嗽加重\n低烧待观察")

    def test_combined_limit_and_unknown_choices(self):
        self.login()
        self.member()
        data = self.form(symptom_picker="1", symptom_choices=["cough"], symptoms="字" * 3998)
        self.assertEqual(self.post("/record/new", data).status_code, 400)
        data.update(symptom_choices=["cough", "<script>"], symptoms="补充")
        self.assertEqual(self.post("/record/new", data).status_code, 303)
        self.assertEqual(self.saved_symptoms(), "咳嗽\n补充")
