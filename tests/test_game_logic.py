import unittest

import main


class MafiaKeepGameLogicTests(unittest.TestCase):
    def test_mafia_target_requires_unique_winner(self):
        self.assertEqual(main.choose_mafia_target([10, 10, 20]), 10)
        self.assertIsNone(main.choose_mafia_target([10, 20]))
        self.assertIsNone(main.choose_mafia_target([]))

    def test_day_vote_ignores_dead_voters_and_dead_targets(self):
        game = {
            "players": {
                1: {"role": "Мирный", "alive": True},
                2: {"role": "Мафия", "alive": True},
                3: {"role": "Мирный", "alive": False},
            },
            "votes": {1: 2, 2: 1, 3: 2},
        }
        # The dead player's vote is ignored, so the living vote is a tie.
        self.assertIsNone(main.choose_day_victim(game))

    def test_mayor_vote_counts_twice(self):
        game = {
            "players": {
                1: {"role": "Мэр", "alive": True},
                2: {"role": "Мирный", "alive": True},
                3: {"role": "Мафия", "alive": True},
            },
            "votes": {1: 3, 2: 3, 3: 2},
        }
        self.assertEqual(main.choose_day_victim(game), 3)

    def test_dead_target_votes_are_ignored(self):
        game = {
            "players": {
                1: {"role": "Мирный", "alive": True},
                2: {"role": "Мафия", "alive": False},
            },
            "votes": {1: 2},
        }
        self.assertIsNone(main.choose_day_victim(game))

    def test_role_distribution_matches_player_count(self):
        for count in range(2, 21):
            roles = main.build_roles(count)
            self.assertEqual(len(roles), count)
            self.assertEqual(roles.count("Мафия") + roles.count("Дон") >= 1, True)

    def test_two_player_test_mode_has_special_roles(self):
        old = main.settings()["test_mode"]
        try:
            main.settings()["test_mode"] = True
            self.assertCountEqual(main.build_roles(2), ["Мафия", "Комиссар"])
        finally:
            main.settings()["test_mode"] = old

    def test_win_conditions(self):
        game = {"players": {
            1: {"role": "Мирный", "alive": True},
            2: {"role": "Комиссар", "alive": True},
        }, "test_mode_game": False}
        self.assertEqual(main.outcome(game), "town")
        game["players"][1] = {"role": "Мафия", "alive": True}
        game["players"][2] = {"role": "Мирный", "alive": True}
        self.assertEqual(main.outcome(game), "mafia")


if __name__ == "__main__":
    unittest.main()
