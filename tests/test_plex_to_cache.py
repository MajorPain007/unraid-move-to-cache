#!/usr/bin/env python3
"""Tests for the path and batching logic of the plex_to_cache daemon.

Runs without Unraid: the module is imported with a stubbed config, so the
pure functions can be exercised directly. Every case here corresponds to a
bug that was actually found, so a regression fails loudly instead of quietly
moving somebody's files to the wrong place.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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


class FlushCacheToArray(unittest.TestCase):
    """The manual flush must never pull a file out from under a playback."""

    def setUp(self):
        configure(ARRAY_ROOT="/mnt/user", CACHE_ROOT="/mnt/cache")
        ptc._flush_state.update(active=False, total=0, done=0, bytes=0,
                                skipped=0, failed=0, finished=0)
        ptc.active_cache_paths = set()
        ptc._pending_copies = set()

    def _run_flush(self, tracked):
        moved = []

        def fake_move(path, track=True):
            moved.append(path)
            return True, False, 1024

        with mock.patch.object(ptc.TrackedFiles, 'load', return_value=tracked), \
             mock.patch.object(ptc, 'move_file_to_array', side_effect=fake_move), \
             mock.patch.object(ptc, 'write_status'):
            ptc.flush_cache_to_array()
        return moved

    def test_streaming_file_is_left_on_cache(self):
        playing = "/mnt/cache/Media/playing.mkv"
        idle = "/mnt/cache/Media/idle.mkv"
        ptc.active_cache_paths = {playing}

        moved = self._run_flush({playing: 1.0, idle: 2.0})

        self.assertEqual(moved, [idle], "an active stream must not be moved")
        self.assertEqual(ptc._flush_state["done"], 1)
        self.assertEqual(ptc._flush_state["skipped"], 1)

    def test_queued_file_is_left_on_cache(self):
        """A file still on its way to the cache would otherwise be moved back
        mid-transfer."""
        queued_source = "/mnt/user/Media/queued.mkv"
        ptc._pending_copies = {queued_source}
        queued_cache = ptc.array_to_cache(queued_source)
        other = "/mnt/cache/Media/other.mkv"

        moved = self._run_flush({queued_cache: 1.0, other: 2.0})

        self.assertEqual(moved, [other])
        self.assertEqual(ptc._flush_state["skipped"], 1)

    def test_everything_else_is_moved_and_counted(self):
        tracked = {"/mnt/cache/Media/a.mkv": 1.0, "/mnt/cache/Media/b.mkv": 2.0}
        moved = self._run_flush(tracked)
        self.assertEqual(sorted(moved), sorted(tracked))
        self.assertEqual(ptc._flush_state["done"], 2)
        self.assertEqual(ptc._flush_state["bytes"], 2048)
        self.assertEqual(ptc._flush_state["skipped"], 0)
        self.assertFalse(ptc._flush_state["active"], "state must be cleared when done")

    def test_a_second_flush_does_not_start_while_one_runs(self):
        ptc._flush_state["active"] = True
        moved = self._run_flush({"/mnt/cache/Media/a.mkv": 1.0})
        self.assertEqual(moved, [], "the running flush owns the tracked list")


class FlushScopeAll(unittest.TestCase):
    """Scope "all" widens the flush to files the plugin never copied - but only
    inside the mapped media folders."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmp, "cache")
        self.array = os.path.join(self.tmp, "user")
        self.media = os.path.join(self.cache, "Media")
        os.makedirs(self.media)
        os.makedirs(os.path.join(self.cache, "downloads"))
        configure(ARRAY_ROOT=self.array, CACHE_ROOT=self.cache,
                  DOCKER_MAPPINGS="/media:" + os.path.join(self.array, "Media"),
                  FLUSH_SCOPE="all", FLUSH_MIN_AGE="30")
        ptc._flush_state.update(active=False, total=0, done=0, bytes=0,
                                skipped=0, conflicts=0, failed=0, finished=0)
        ptc.active_cache_paths = set()
        ptc._pending_copies = set()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, path, age_seconds=7200):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("x")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_only_mapped_folders_are_walked(self):
        wanted = self._make(os.path.join(self.media, "film.mkv"))
        self._make(os.path.join(self.cache, "downloads", "linux.mkv"))

        found = ptc._cache_media_files(30 * 60)

        self.assertIn(wanted, found)
        self.assertEqual(len(found), 1,
                         "a separate downloads share must stay out of scope")

    def test_recently_written_files_are_left_alone(self):
        self._make(os.path.join(self.media, "importing.mkv"), age_seconds=60)
        old = self._make(os.path.join(self.media, "settled.mkv"))

        found = ptc._cache_media_files(30 * 60)

        self.assertEqual(list(found), [old])

    def test_untracked_file_is_not_deleted_when_the_name_exists_on_the_array(self):
        """move_file_to_array drops the cache copy when the destination exists.
        That is right for a plugin copy and wrong for anything else."""
        cache_file = self._make(os.path.join(self.media, "clash.mkv"))
        self._make(os.path.join(self.array, "Media", "clash.mkv"))

        moved = []
        with mock.patch.object(ptc.TrackedFiles, 'load', return_value={}), \
             mock.patch.object(ptc, 'move_file_to_array',
                               side_effect=lambda p, track=True: (moved.append(p), (True, False, 0))[1]), \
             mock.patch.object(ptc, 'write_status'):
            ptc.flush_cache_to_array()

        self.assertEqual(moved, [], "the untracked file must not be moved")
        self.assertEqual(ptc._flush_state["conflicts"], 1)
        self.assertTrue(os.path.exists(cache_file))


if __name__ == "__main__":
    unittest.main(verbosity=2)
