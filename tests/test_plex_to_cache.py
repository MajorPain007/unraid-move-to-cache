#!/usr/bin/env python3
"""Tests for the path and batching logic of the plex_to_cache daemon.

Runs without Unraid: the module is imported with a stubbed config, so the
pure functions can be exercised directly. Every case here corresponds to a
bug that was actually found, so a regression fails loudly instead of quietly
moving somebody's files to the wrong place.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import plex_to_cache as ptc  # noqa: E402


def configure(**overrides):
    """Point the module at a known configuration."""
    ptc.config = dict(ptc.DEFAULT_CONFIG)
    ptc.config.update(overrides)
    mappings = {}
    for pair in ptc.config.get("DOCKER_MAPPINGS", "").split(';'):
        if ':' in pair:
            k, v = pair.split(':', 1)
            mappings[k.strip()] = v.strip()
    ptc.docker_mappings = mappings


class PathRoundTrip(unittest.TestCase):
    """A file copied to cache must come back to where it came from."""

    def test_default_user_share(self):
        configure(ARRAY_ROOT="/mnt/user", CACHE_ROOT="/mnt/cache")
        src = "/mnt/user/Media/Show/S01/ep.mkv"
        cache = ptc.array_to_cache(src)
        self.assertEqual(cache, "/mnt/cache/Media/Show/S01/ep.mkv")
        # back onto the array, never through the cache-inclusive share
        self.assertEqual(ptc.cache_to_array(cache), "/mnt/user0/Media/Show/S01/ep.mkv")

    def test_array_root_below_the_share(self):
        """ARRAY_ROOT is a free-text field; a deeper root must still round-trip."""
        configure(ARRAY_ROOT="/mnt/user/Media", CACHE_ROOT="/mnt/cache")
        src = "/mnt/user/Media/Show/ep.mkv"
        cache = ptc.array_to_cache(src)
        self.assertEqual(cache, "/mnt/cache/Show/ep.mkv")
        self.assertEqual(ptc.cache_to_array(cache), "/mnt/user0/Media/Show/ep.mkv")

    def test_root_outside_the_user_share(self):
        """An unassigned device has no /mnt/user0 twin - stay on that pool."""
        configure(ARRAY_ROOT="/mnt/disks/pool", CACHE_ROOT="/mnt/cache")
        src = "/mnt/disks/pool/Show/ep.mkv"
        cache = ptc.array_to_cache(src)
        self.assertEqual(cache, "/mnt/cache/Show/ep.mkv")
        self.assertEqual(ptc.cache_to_array(cache), "/mnt/disks/pool/Show/ep.mkv")

    def test_unrelated_path_is_left_alone(self):
        configure(ARRAY_ROOT="/mnt/user", CACHE_ROOT="/mnt/cache")
        self.assertEqual(ptc.array_to_cache("/somewhere/else.mkv"), "/somewhere/else.mkv")
        self.assertEqual(ptc.cache_to_array("/somewhere/else.mkv"), "/somewhere/else.mkv")

    def test_sibling_prefix_is_not_treated_as_a_child(self):
        """/mnt/cache-backup must not be mistaken for something under /mnt/cache."""
        configure(ARRAY_ROOT="/mnt/user", CACHE_ROOT="/mnt/cache")
        self.assertEqual(ptc.cache_to_array("/mnt/cache-backup/x.mkv"),
                         "/mnt/cache-backup/x.mkv")


class DockerPathTranslation(unittest.TestCase):

    def test_longest_prefix_wins_regardless_of_order(self):
        configure(ARRAY_ROOT="/mnt/user",
                  DOCKER_MAPPINGS="/media:/mnt/user/Media;/media/movies:/mnt/user/Filme")
        self.assertEqual(ptc.translate_docker_path("/media/movies/Film.mkv"),
                         "/mnt/user/Filme/Film.mkv")
        self.assertEqual(ptc.translate_docker_path("/media/tv/Show/ep.mkv"),
                         "/mnt/user/Media/tv/Show/ep.mkv")

    def test_prefix_needs_a_path_boundary(self):
        configure(ARRAY_ROOT="/mnt/user", DOCKER_MAPPINGS="/data:/mnt/user/Data")
        self.assertEqual(ptc.translate_docker_path("/data/x.mkv"), "/mnt/user/Data/x.mkv")
        # /database is a different directory, not a child of /data
        self.assertEqual(ptc.translate_docker_path("/database/dump.mkv"),
                         "/database/dump.mkv")

    def test_relative_host_path_is_taken_as_a_share_name(self):
        configure(ARRAY_ROOT="/mnt/user", DOCKER_MAPPINGS="/media:Media")
        self.assertEqual(ptc.translate_docker_path("/media/x.mkv"), "/mnt/user/Media/x.mkv")

    def test_windows_separators(self):
        configure(ARRAY_ROOT="/mnt/user", DOCKER_MAPPINGS="/media:/mnt/user/Media")
        self.assertEqual(ptc.translate_docker_path("\\media\\Show\\ep.mkv"),
                         "/mnt/user/Media/Show/ep.mkv")


class EpisodeParsing(unittest.TestCase):

    def test_common_naming_schemes(self):
        self.assertEqual(ptc.parse_episode("Show.S01E05.mkv"), 5)
        self.assertEqual(ptc.parse_episode("Show.s2e11.1080p.mkv"), 11)
        self.assertEqual(ptc.parse_episode("Show 1x05.mkv"), 5)

    def test_resolution_is_not_an_episode(self):
        self.assertIsNone(ptc.parse_episode("Movie.1920x1080.mkv"))

    def test_movie_has_none(self):
        self.assertIsNone(ptc.parse_episode("Some Movie (2021).mkv"))


class BatchSelection(unittest.TestCase):

    def test_short_season_is_cached_whole(self):
        configure(ENABLE_EPISODE_BATCHING="True", EPISODE_BATCH_SIZE="30",
                  EPISODE_BATCH_TOLERANCE="10")
        eps = list(range(1, 37))          # 36 <= 30 + 10
        self.assertEqual(set(ptc.select_batch_episodes(eps, 1)), set(eps))

    def test_long_season_is_split(self):
        configure(ENABLE_EPISODE_BATCHING="True", EPISODE_BATCH_SIZE="30",
                  EPISODE_BATCH_TOLERANCE="10")
        eps = list(range(1, 101))
        selected = ptc.select_batch_episodes(eps, 1)
        self.assertLess(len(selected), len(eps))
        self.assertIn(1, selected)

    def test_selection_never_looks_backwards(self):
        configure(ENABLE_EPISODE_BATCHING="True", EPISODE_BATCH_SIZE="30",
                  EPISODE_BATCH_TOLERANCE="10")
        eps = list(range(1, 101))
        selected = ptc.select_batch_episodes(eps, 50)
        self.assertTrue(all(e >= 50 for e in selected),
                        f"selection reaches behind the current episode: {sorted(selected)[:5]}")


class ConfigFallbacks(unittest.TestCase):

    def test_bad_integer_falls_back_to_the_default_not_zero(self):
        configure(CHECK_INTERVAL="not a number")
        self.assertEqual(ptc.cfg("CHECK_INTERVAL", as_int=True),
                         int(ptc.DEFAULT_CONFIG["CHECK_INTERVAL"]))

    def test_bool_parsing(self):
        for value, expected in (("True", True), ("true", True), ("1", True),
                                ("yes", True), ("False", False), ("", False)):
            configure(ENABLE_PLEX=value)
            self.assertEqual(ptc.cfg("ENABLE_PLEX", as_bool=True), expected, value)


class RsyncCommand(unittest.TestCase):

    def test_partial_data_is_not_exposed_under_the_final_name(self):
        """--inplace would leave a truncated file at the destination path,
        which Unraid would happily serve to a stream that starts mid-copy."""
        source = Path(ptc.__file__).read_text()
        self.assertNotIn('"--inplace"', source)
        self.assertIn('--partial-dir', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
