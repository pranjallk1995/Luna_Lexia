import unittest

from domain.tables import apply_edits, table_rows, table_values


class TableEditingTests(unittest.TestCase):
    def test_object_rows_round_trip_with_edits_deletions_and_additions(self):
        columns = ["label", "amount"]
        rows = table_rows([{"label": "Basic", "amount": 50000.0}, {"label": "HRA", "amount": 20000.5}], columns)
        self.assertEqual(rows, [{"label": "Basic", "amount": "50000"}, {"label": "HRA", "amount": "20000.5"}])
        state = {
            "edited_rows": {"1": {"amount": "21,000"}},
            "deleted_rows": [0],
            "added_rows": [{"label": "Bonus", "amount": "5000"}, {"label": None, "amount": " "}],
        }
        self.assertEqual(
            table_values(apply_edits(rows, state), columns),
            [{"label": "HRA", "amount": "21,000"}, {"label": "Bonus", "amount": "5000"}],
        )

    def test_plain_value_lists_use_one_column(self):
        rows = table_rows(["Python", "Docker"], None)
        state = {"edited_rows": {0: {"Value": "PyTorch"}}, "added_rows": [{"Value": "LangGraph"}, {}]}
        self.assertEqual(table_values(apply_edits(rows, state), None), ["PyTorch", "Docker", "LangGraph"])

    def test_empty_lists_and_missing_items(self):
        self.assertEqual(table_rows(None, ["date"]), [])
        self.assertEqual(table_values(apply_edits([], {"added_rows": [{"date": "2025-04-01"}]}), ["date"]), [{"date": "2025-04-01"}])


if __name__ == "__main__":
    unittest.main()
