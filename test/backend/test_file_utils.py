import unittest

from api.utils.file_utils import remove_identical_branches


class TestRemoveIdenticalBranches(unittest.TestCase):

    def test_extra_entries_on_right_terminates(self):
        # Used to loop forever when the right side had trailing entries
        left, right = remove_identical_branches([], [
            {"title": "a", "hash": "1"},
            {"title": "b", "hash": "2"},
        ])
        self.assertEqual(left, [])
        self.assertEqual([x["title"] for x in right], ["a", "b"])


    def test_identical_removed_differences_kept(self):
        left, right = remove_identical_branches(
            [{"title": "a", "hash": "1"}, {"title": "b", "hash": "2"}],
            [{"title": "a", "hash": "1"}, {"title": "b", "hash": "X"}, {"title": "c", "hash": "3"}],
        )
        self.assertEqual([x["title"] for x in left], ["b"])
        self.assertEqual([x["title"] for x in right], ["b", "c"])
