#!/usr/bin/env python3
"""Tests for the path and batching logic of the plex_to_cache daemon.

Runs without Unraid: the module is imported with a stubbed config, so the
pure functions can be exercised directly. Every case here corresponds to a
bug that was actually found, so a regression fails loudly instead of quietly
moving somebody's files to the wrong place.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
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


def fresh_job():
    ptc._job.clear()
    ptc._job.update(total=0, done=0, bytes=0, skipped=0, conflicts=0, recent=0,
                    kept=0, failed=0, dropped=0, current=None, stopped=False)


class StateInTempDir(unittest.TestCase):
    """Every file the module keeps state in, moved into a temporary directory."""

    STATE = ("TRACKED_FILES", "TRACKED_LOCK", "STATUS_FILE", "MOVE_QUEUE",
             "MOVE_LOCK", "MOVE_PID", "MOVE_STATUS", "LOCK_FILE")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = {name: getattr(ptc, name) for name in self.STATE}
        for name in self.STATE:
            setattr(ptc, name, os.path.join(self.tmp, name.lower()))
        ptc._shutting_down.clear()
        ptc._requested.clear()
        ptc.active_cache_paths = set()
        ptc._pending_copies = set()
        fresh_job()

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(ptc, name, value)
        ptc._shutting_down.clear()
        ptc._requested.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, path, content="x", age_seconds=7200):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content)
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path


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


class MappedFolders(unittest.TestCase):

    def test_prefixes_need_a_boundary(self):
        """/mnt/user0 is not a folder inside /mnt/user."""
        configure(ARRAY_ROOT="/mnt/user", CACHE_ROOT="/mnt/cache",
                  DOCKER_MAPPINGS="/media:/mnt/user/Media;/other:/mnt/user0/Other")
        self.assertIn("/mnt/cache/Media", ptc._mapped_cache_dirs())
        self.assertNotIn("/mnt/cache0/Other", ptc._mapped_cache_dirs())

    def test_the_pool_itself_is_never_a_media_folder(self):
        """A mapping of the whole user share must not hand appdata to the mover."""
        configure(ARRAY_ROOT="/mnt/user", CACHE_ROOT="/mnt/cache",
                  DOCKER_MAPPINGS="/data:/mnt/user")
        self.assertEqual(ptc._mapped_cache_dirs(), set())

    def test_trailing_slashes_in_the_roots_are_ignored(self):
        tmp = tempfile.mkdtemp()
        real = ptc.CONFIG_FILE
        try:
            ptc.CONFIG_FILE = os.path.join(tmp, "settings.cfg")
            Path(ptc.CONFIG_FILE).write_text('CACHE_ROOT="/mnt/cache/"\nARRAY_ROOT="/mnt/user/"\n')
            ptc.load_config()
            self.assertEqual(ptc.cfg("CACHE_ROOT"), "/mnt/cache")
            self.assertEqual(ptc.cfg("ARRAY_ROOT"), "/mnt/user")
        finally:
            ptc.CONFIG_FILE = real
            shutil.rmtree(tmp, ignore_errors=True)


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

    def test_an_interrupted_transfer_leaves_nothing_behind(self):
        """--inplace would leave a truncated file under the destination's name,
        which Unraid would serve to a stream that starts mid-copy. --partial
        would keep the unfinished data for a resume that rsync never does
        between two local paths - up to a whole film of it on the cache."""
        source = Path(ptc.__file__).read_text()
        self.assertNotIn('"--inplace"', source)
        self.assertNotIn('"--partial"', source)
        self.assertNotIn('--partial-dir=', source)


class MoveBatch(StateInTempDir):
    """A move to the array must never pull a file out from under a playback."""

    def setUp(self):
        super().setUp()
        configure(ARRAY_ROOT="/mnt/user", CACHE_ROOT="/mnt/cache")

    def _run(self, tracked, requests, protected=frozenset(), result=("moved", 1024)):
        moved = []

        def fake_move(path):
            moved.append(path)
            return result

        protect = protected if callable(protected) else (lambda: set(protected))
        with mock.patch.object(ptc.TrackedFiles, 'load', return_value=tracked), \
             mock.patch.object(ptc, 'move_file_to_array', side_effect=fake_move), \
             mock.patch.object(ptc, '_protected_now', side_effect=protect), \
             mock.patch.object(ptc.TrackedFiles, 'remove_many') as untrack, \
             mock.patch.object(ptc, '_write_job'):
            ptc._move_batch(requests, set())
        return moved, untrack

    def test_streaming_file_is_left_on_cache(self):
        playing = "/mnt/cache/Media/playing.mkv"
        idle = "/mnt/cache/Media/idle.mkv"

        moved, _ = self._run({playing: 1.0, idle: 2.0},
                             [("auto", playing), ("auto", idle)], protected={playing})

        self.assertEqual(moved, [idle], "an active stream must not be moved")
        self.assertEqual(ptc._job["done"], 1)
        self.assertEqual(ptc._job["skipped"], 1)

    def test_everything_else_is_moved_counted_and_untracked_in_one_write(self):
        tracked = {"/mnt/cache/Media/a.mkv": 1.0, "/mnt/cache/Media/b.mkv": 2.0}
        moved, untrack = self._run(tracked, [("auto", p) for p in tracked])
        self.assertEqual(sorted(moved), sorted(tracked))
        self.assertEqual(ptc._job["done"], 2)
        self.assertEqual(ptc._job["bytes"], 2048)
        self.assertEqual(ptc._job["skipped"], 0)
        untrack.assert_called_once()
        self.assertEqual(sorted(untrack.call_args[0][0]), sorted(tracked))

    def test_auto_cleanup_only_moves_what_the_plugin_cached(self):
        moved, _ = self._run({}, [("auto", "/mnt/cache/Media/download.mkv")])
        self.assertEqual(moved, [], "a file not on the tracked list is not Auto Cleanup's")

    def test_a_folder_moves_everything_below_it(self):
        """The browser offers a button on a season or a whole series."""
        tracked = {
            "/mnt/cache/Media/Show/S01/e1.mkv": 1.0,
            "/mnt/cache/Media/Show/S01/e2.mkv": 2.0,
            "/mnt/cache/Media/Show/S02/e1.mkv": 3.0,
            "/mnt/cache/Media/Other/film.mkv":  4.0,
        }
        moved, _ = self._run(tracked, [("pick", "/mnt/cache/Media/Show/S01")])
        self.assertEqual(sorted(moved), ["/mnt/cache/Media/Show/S01/e1.mkv",
                                         "/mnt/cache/Media/Show/S01/e2.mkv"])

        fresh_job()
        moved, _ = self._run(tracked, [("pick", "/mnt/cache/Media/Show")])
        self.assertEqual(len(moved), 3, "the series folder covers both seasons")

    def test_moving_a_folder_still_spares_what_is_playing(self):
        """Selecting a season in the browser must not yank the episode someone
        is watching right now."""
        playing = "/mnt/cache/Media/Show/S01/e2.mkv"
        tracked = {
            "/mnt/cache/Media/Show/S01/e1.mkv": 1.0,
            playing: 2.0,
            "/mnt/cache/Media/Show/S01/e3.mkv": 3.0,
        }
        moved, _ = self._run(tracked, [("pick", "/mnt/cache/Media/Show/S01")],
                             protected={playing})
        self.assertNotIn(playing, moved)
        self.assertEqual(len(moved), 2)
        self.assertEqual(ptc._job["skipped"], 1)

    def test_a_stream_starting_mid_move_is_still_spared(self):
        """The protection is asked for per file. A move can run for hours, and
        a check done once at the start cannot know about a stream that begins
        halfway through."""
        first, second = "/mnt/cache/Media/S/a.mkv", "/mnt/cache/Media/S/b.mkv"
        answers = iter([set(), {second}])
        moved, _ = self._run({first: 1.0, second: 2.0}, [("pick", "/mnt/cache/Media/S")],
                             protected=lambda: next(answers))
        self.assertEqual(moved, [first])
        self.assertEqual(ptc._job["skipped"], 1)

    def test_a_sibling_folder_with_a_shared_prefix_is_not_swept_in(self):
        tracked = {
            "/mnt/cache/Media/Show/e1.mkv":  1.0,
            "/mnt/cache/Media/Show2/e1.mkv": 2.0,
        }
        moved, _ = self._run(tracked, [("pick", "/mnt/cache/Media/Show")])
        self.assertEqual(moved, ["/mnt/cache/Media/Show/e1.mkv"],
                         "Show2 must not be dragged along by Show")

    def test_a_path_asked_for_twice_is_moved_once(self):
        path = "/mnt/cache/Media/a.mkv"
        moved, _ = self._run({path: 1.0}, [("pick", path), ("auto", path), ("pick", path)])
        self.assertEqual(moved, [path])

    def test_a_stop_ends_the_batch_and_leaves_the_rest(self):
        tracked = {f"/mnt/cache/Media/{c}.mkv": float(i) for i, c in enumerate("abc")}

        def stop_after_first(path):
            ptc._shutting_down.set()
            return "moved", 1

        with mock.patch.object(ptc.TrackedFiles, 'load', return_value=tracked), \
             mock.patch.object(ptc, 'move_file_to_array', side_effect=stop_after_first) as move, \
             mock.patch.object(ptc, '_protected_now', return_value=set()), \
             mock.patch.object(ptc.TrackedFiles, 'remove_many') as untrack, \
             mock.patch.object(ptc, '_write_job'):
            ptc._move_batch([("auto", p) for p in tracked], set())
        self.assertEqual(move.call_count, 1)
        self.assertEqual(untrack.call_args[0][0], ["/mnt/cache/Media/a.mkv"],
                         "what was moved before the stop is recorded")


class MovesBeyondTheTrackedList(StateInTempDir):
    """An explicit selection reaches files the plugin never copied - but only
    inside the mapped media folders."""

    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.tmp, "cache")
        self.array = os.path.join(self.tmp, "user")
        self.media = os.path.join(self.cache, "Media")
        os.makedirs(self.media)
        os.makedirs(os.path.join(self.cache, "downloads"))
        os.makedirs(os.path.join(self.array, "Media"))
        configure(ARRAY_ROOT=self.array, CACHE_ROOT=self.cache,
                  DOCKER_MAPPINGS="/media:" + os.path.join(self.array, "Media"))

    def _move(self, requests, tracked=None):
        moved = []
        with mock.patch.object(ptc.TrackedFiles, 'load', return_value=tracked or {}), \
             mock.patch.object(ptc, 'move_file_to_array',
                               side_effect=lambda p: (moved.append(p), ("moved", 0))[1]), \
             mock.patch.object(ptc, '_protected_now', return_value=set()), \
             mock.patch.object(ptc.TrackedFiles, 'remove_many'), \
             mock.patch.object(ptc, '_write_job'):
            ptc._move_batch(requests, set())
        return moved

    def test_only_mapped_folders_are_walked(self):
        wanted = self.make(os.path.join(self.media, "film.mkv"))
        self.make(os.path.join(self.cache, "downloads", "linux.mkv"))
        found = ptc._cache_media_files(30 * 60)
        self.assertIn(wanted, found)
        self.assertEqual(len(found), 1, "a separate downloads share must stay out of scope")

    def test_recently_written_files_are_left_alone_and_counted(self):
        """The count was missing before: the summary said nothing about them."""
        fresh = self.make(os.path.join(self.media, "importing.mkv"), age_seconds=60)
        old = self.make(os.path.join(self.media, "settled.mkv"))
        recent = []
        self.assertEqual(list(ptc._cache_media_files(30 * 60, recent=recent)), [old])
        self.assertEqual(recent, [fresh])

        moved = self._move([("pick", self.media)])
        self.assertEqual(moved, [old])
        self.assertEqual(ptc._job["recent"], 1)

    def test_hidden_files_and_folders_are_not_media(self):
        """.Recycle.Bin holds deleted media; ._ files are what a Mac leaves."""
        real = self.make(os.path.join(self.media, "Show", "e1.mkv"))
        self.make(os.path.join(self.media, "Show", "._e1.mkv"))
        self.make(os.path.join(self.media, ".Recycle.Bin", "deleted.mkv"))
        self.assertEqual(list(ptc._cache_media_files(0)), [real])

    def test_an_explicit_selection_covers_untracked_media(self):
        """Clicking To Array on a folder means move it, tracked or not."""
        untracked = self.make(os.path.join(self.media, "Serie", "e1.mkv"))
        self.assertEqual(self._move([("pick", os.path.join(self.media, "Serie"))]), [untracked])

    def test_a_selection_outside_the_mapped_folders_finds_nothing(self):
        """The mover enforces containment itself rather than trusting the path
        it was handed."""
        outside = self.make(os.path.join(self.cache, "downloads", "linux.mkv"))
        self.assertEqual(self._move([("pick", os.path.join(self.cache, "downloads"))]), [])
        self.assertTrue(os.path.exists(outside))

    def test_moving_one_file_does_not_walk_the_whole_library(self):
        for i in range(50):
            self.make(os.path.join(self.media, "Riesig", f"f{i}.mkv"))
        wanted = self.make(os.path.join(self.media, "Klein", "one.mkv"))
        found = ptc._cache_media_files(0, roots=[wanted])
        self.assertEqual(list(found), [wanted],
                         "a single file must not pull in the rest of the tree")

    def test_an_untracked_file_with_a_different_twin_on_the_array_stays(self):
        """Two versions of something: only a person can say which to keep."""
        cache_file = self.make(os.path.join(self.media, "clash.mkv"), content="new version")
        self.make(os.path.join(self.array, "Media", "clash.mkv"), content="old", age_seconds=90000)
        self.assertEqual(self._move([("pick", self.media)]), [])
        self.assertEqual(ptc._job["conflicts"], 1)
        self.assertTrue(os.path.exists(cache_file))

    def test_an_untracked_file_with_an_identical_twin_is_a_lost_copy(self):
        """A plugin copy that fell off the tracked list is identical to the
        original. It used to be reported as a name clash and left behind."""
        cache_file = self.make(os.path.join(self.media, "lost.mkv"))
        twin = os.path.join(self.array, "Media", "lost.mkv")
        shutil.copy2(cache_file, twin)
        self.assertEqual(self._move([("pick", self.media)]), [cache_file])
        self.assertEqual(ptc._job["conflicts"], 0)


class MoveFileToArray(StateInTempDir):
    """What happens when the array already has a file of that name."""

    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.tmp, "cache")
        self.array = os.path.join(self.tmp, "user")
        os.makedirs(os.path.join(self.cache, "Media"))
        os.makedirs(os.path.join(self.array, "Media"))
        configure(ARRAY_ROOT=self.array, CACHE_ROOT=self.cache,
                  DOCKER_MAPPINGS="/media:" + os.path.join(self.array, "Media"))
        # rsync as far as these tests care: copy with attributes, drop the source
        self.rsync = mock.patch.object(ptc, 'rsync_transfer', side_effect=self._fake_rsync)
        self.rsync.start()

    def tearDown(self):
        self.rsync.stop()
        super().tearDown()

    @staticmethod
    def _fake_rsync(src, dst, remove_source=True):
        shutil.copy2(src, dst)
        if remove_source:
            os.remove(src)

    def pair(self, cache_content, array_content, cache_age, array_age):
        c = self.make(os.path.join(self.cache, "Media", "Show", "e1.mkv"), cache_content, cache_age)
        a = None
        if array_content is not None:
            a = self.make(os.path.join(self.array, "Media", "Show", "e1.mkv"), array_content, array_age)
        return c, a

    def test_not_on_the_array_yet_it_is_moved_and_its_folder_made_like_the_cache_one(self):
        c, _ = self.pair("data", None, 100, 0)
        os.chmod(os.path.dirname(c), 0o777)
        self.assertEqual(ptc.move_file_to_array(c), ("moved", 4))
        a = os.path.join(self.array, "Media", "Show", "e1.mkv")
        self.assertEqual(Path(a).read_text(), "data")
        self.assertFalse(os.path.exists(c))
        self.assertEqual(os.stat(os.path.dirname(a)).st_mode & 0o777, 0o777,
                         "a folder made as root with 0755 would lock out Sonarr")

    def test_an_identical_copy_is_just_dropped(self):
        c, a = self.pair("same", "same", 5000, 5000)
        self.assertEqual(ptc.move_file_to_array(c), ("dropped", 4))
        self.assertFalse(os.path.exists(c))
        self.assertEqual(Path(a).read_text(), "same")

    def test_a_copy_changed_on_the_cache_replaces_the_one_on_the_array(self):
        """Sonarr swapping in a better release under the same name writes to
        the cache copy. Deleting it because the array 'already has it' threw
        the upgrade away."""
        c, a = self.pair("better release", "old", 60, 90000)
        self.assertEqual(ptc.move_file_to_array(c)[0], "moved")
        self.assertEqual(Path(a).read_text(), "better release")
        self.assertFalse(os.path.exists(c))

    def test_a_stale_cache_copy_gives_way_to_a_newer_array_file(self):
        c, a = self.pair("old", "newer on the array", 90000, 60)
        self.assertEqual(ptc.move_file_to_array(c)[0], "dropped")
        self.assertEqual(Path(a).read_text(), "newer on the array")

    def test_same_age_different_size_is_left_for_a_person(self):
        c, a = self.pair("one version", "another", 5000, 5000)
        self.assertEqual(ptc.move_file_to_array(c), ("kept", 0))
        self.assertTrue(os.path.exists(c))
        self.assertEqual(Path(a).read_text(), "another")

    def test_a_path_outside_the_cache_root_is_never_touched(self):
        """Its 'place on the array' is the file itself, and a file compared with
        itself looks like a duplicate that can go."""
        stray = self.make(os.path.join(self.tmp, "elsewhere", "e1.mkv"))
        self.assertEqual(ptc.move_file_to_array(stray), ("kept", 0))
        self.assertTrue(os.path.exists(stray))

    def test_without_the_array_mounted_nothing_is_created_on_the_ram_disk(self):
        c, _ = self.pair("data", None, 100, 0)
        shutil.rmtree(self.array)
        self.assertEqual(ptc.move_file_to_array(c)[0], "failed")
        self.assertFalse(os.path.exists(self.array))
        self.assertTrue(os.path.exists(c))

    def test_a_stopped_transfer_leaves_the_cache_file_and_no_empty_folders(self):
        c, _ = self.pair("data", None, 100, 0)
        with mock.patch.object(ptc, 'rsync_transfer', side_effect=ptc.TransferStopped()):
            self.assertEqual(ptc.move_file_to_array(c), ("stopped", 0))
        self.assertTrue(os.path.exists(c))
        self.assertFalse(os.path.exists(os.path.join(self.array, "Media", "Show")),
                         "the season folder was made for this move only")
        self.assertTrue(os.path.isdir(os.path.join(self.array, "Media")))


class CopyToCacheAdoption(StateInTempDir):
    """Only a copy of the file on the array is the plugin's."""

    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.tmp, "cache")
        self.array = os.path.join(self.tmp, "user")
        configure(ARRAY_ROOT=self.array, CACHE_ROOT=self.cache,
                  DOCKER_MAPPINGS="/media:" + os.path.join(self.array, "Media"))

    def test_a_download_that_is_only_on_the_cache_is_not_adopted(self):
        """The user share shows the cache copy, so comparing against it
        compared the file with itself and every fresh download that got
        played ended up on the tracked list."""
        self.make(os.path.join(self.cache, "Media", "new.mkv"))
        with mock.patch.object(ptc, 'rsync_transfer') as rsync:
            ptc.copy_file_to_cache(os.path.join(self.array, "Media", "new.mkv"))
        rsync.assert_not_called()
        self.assertEqual(ptc.TrackedFiles.load(), {})

    def test_an_identical_copy_is_adopted(self):
        cache_file = self.make(os.path.join(self.cache, "Media", "film.mkv"))
        os.makedirs(os.path.join(self.array, "Media"))
        shutil.copy2(cache_file, os.path.join(self.array, "Media", "film.mkv"))
        ptc.copy_file_to_cache(os.path.join(self.array, "Media", "film.mkv"))
        self.assertIn(cache_file, ptc.TrackedFiles.load())


class StorageReadiness(StateInTempDir):
    """On boot the service starts before the array. Judging the tracked list
    by what exists at that moment emptied it."""

    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.tmp, "cache")
        self.array = os.path.join(self.tmp, "user")
        configure(ARRAY_ROOT=self.array, CACHE_ROOT=self.cache)

    def test_not_ready_while_the_pool_is_missing_or_empty(self):
        self.assertFalse(ptc._storage_ready())
        os.makedirs(self.cache)
        self.assertFalse(ptc._storage_ready(), "an empty mount point is not a mounted pool")
        os.makedirs(os.path.join(self.cache, "Media"))
        self.assertFalse(ptc._storage_ready(), "the array is not there yet")
        os.makedirs(self.array)
        self.assertTrue(ptc._storage_ready())

    def test_the_tracked_list_survives_a_start_before_the_array(self):
        ptc.TrackedFiles.save({os.path.join(self.cache, "Media", "a.mkv"): 1.0})
        state = {"storage": None, "last_streams": {}, "last_days_check": 0}
        with mock.patch.object(ptc, 'get_active_streams', return_value={}):
            self.assertEqual(ptc._daemon_pass(state), frozenset())
        self.assertEqual(len(ptc.TrackedFiles.load()), 1)

    def test_reconciled_once_the_array_is_up(self):
        state = {"storage": None, "last_streams": {}, "last_days_check": 0}
        with mock.patch.object(ptc, 'reconcile_tracked_files') as reconcile, \
             mock.patch.object(ptc, 'get_active_streams', return_value={}):
            ptc._daemon_pass(state)
            reconcile.assert_not_called()
            os.makedirs(os.path.join(self.cache, "Media"))
            os.makedirs(self.array)
            ptc._daemon_pass(state)
            ptc._daemon_pass(state)
        reconcile.assert_called_once()


class Reconcile(StateInTempDir):

    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.tmp, "cache")
        self.array = os.path.join(self.tmp, "user")
        os.makedirs(os.path.join(self.array, "Media"))
        configure(ARRAY_ROOT=self.array, CACHE_ROOT=self.cache,
                  DOCKER_MAPPINGS="/media:" + os.path.join(self.array, "Media"))

    def test_only_identical_twins_are_adopted(self):
        copy = self.make(os.path.join(self.cache, "Media", "copy.mkv"))
        shutil.copy2(copy, os.path.join(self.array, "Media", "copy.mkv"))
        self.make(os.path.join(self.cache, "Media", "other.mkv"), "different")
        self.make(os.path.join(self.array, "Media", "other.mkv"), "x", age_seconds=99999)
        self.make(os.path.join(self.cache, "Media", "only-here.mkv"))

        ptc.reconcile_tracked_files()
        self.assertEqual(list(ptc.TrackedFiles.load()), [copy])

    def test_leftovers_of_interrupted_copies_are_removed(self):
        leftover = os.path.join(self.cache, "Media", "Show", ptc.LEGACY_PARTIAL_DIR, "e1.mkv")
        self.make(leftover)
        ptc.reconcile_tracked_files()
        self.assertFalse(os.path.exists(os.path.dirname(leftover)))


class PoolSpace(StateInTempDir):
    """ZFS answers statvfs per dataset, without the children."""

    def setUp(self):
        super().setUp()
        self.mounts = os.path.join(self.tmp, "mounts")
        self._real = ptc.PROC_MOUNTS
        ptc.PROC_MOUNTS = self.mounts
        configure(CACHE_ROOT=self.tmp)

    def tearDown(self):
        ptc.PROC_MOUNTS = self._real
        super().tearDown()

    def test_a_zfs_pool_is_measured_as_a_whole(self):
        Path(self.mounts).write_text(f"cache {self.tmp} zfs rw 0 0\n"
                                     f"cache/Media {self.tmp}/Media zfs rw 0 0\n")
        done = mock.Mock(stdout="800\t200\n")
        with mock.patch.object(ptc.subprocess, 'run', return_value=done) as run:
            self.assertEqual(ptc._pool_space(), (800, 1000, 200))
        self.assertEqual(run.call_args[0][0][-1], "cache", "the pool, not the dataset")

    def test_other_filesystems_use_statvfs(self):
        Path(self.mounts).write_text(f"/dev/sdb1 {self.tmp} xfs rw 0 0\n")
        with mock.patch.object(ptc.subprocess, 'run') as run:
            used, total, free = ptc._pool_space()
        run.assert_not_called()
        self.assertGreater(total, 0)

    def test_mount_points_with_spaces_are_read(self):
        Path(self.mounts).write_text("/dev/sdc1 /mnt/my\\040pool btrfs rw 0 0\n")
        self.assertEqual(ptc._mount_of("/mnt/my pool/x"), ("/mnt/my pool", "/dev/sdc1", "btrfs"))

    def test_space_just_freed_is_counted_before_the_pool_reports_it(self):
        """Without it eviction would move 25 files to make room for one."""
        configure(CACHE_ROOT=self.tmp, CACHE_MAX_USAGE="80")
        gb = 1024 ** 3
        with mock.patch.object(ptc, '_pool_space', return_value=(90 * gb, 100 * gb, 10 * gb)):
            self.assertFalse(ptc.cache_has_room_for(gb))
            self.assertTrue(ptc.cache_has_room_for(gb, freed=15 * gb))


class ApiRobustness(unittest.TestCase):

    def test_an_address_without_a_scheme_does_not_escape(self):
        """It used to raise, and abort the whole pass of the main loop."""
        errors = []
        self.assertIsNone(ptc.api_get("192.168.1.10/status/sessions", {}, errors=errors))
        self.assertTrue(errors)

    def test_a_server_that_stops_answering_is_said_once(self):
        ptc._api_state.clear()
        with mock.patch.object(ptc, 'log') as log:
            for _ in range(3):
                ptc._note_api("Plex", False, ["refused"])
            ptc._note_api("Plex", True, [])
        self.assertEqual(log.call_count, 2, "one line when it stops, one when it is back")


class TrackedListWrites(StateInTempDir):
    """The list lives on the USB flash drive, so a move must not rewrite it
    once per file."""

    def test_removing_many_entries_writes_once(self):
        paths = {f"/mnt/cache/Media/e{i}.mkv": float(i) for i in range(51)}
        ptc.TrackedFiles.save(paths)

        writes = []
        real_save = ptc.TrackedFiles.save
        try:
            ptc.TrackedFiles.save = lambda t: (writes.append(1), real_save(t))[1]
            ptc.TrackedFiles.remove_many(list(paths))
        finally:
            ptc.TrackedFiles.save = real_save

        self.assertEqual(len(writes), 1, "one write for the whole batch")
        self.assertEqual(ptc.TrackedFiles.load(), {})

    def test_removing_nothing_writes_nothing(self):
        ptc.TrackedFiles.save({"/mnt/cache/Media/a.mkv": 1.0})
        writes = []
        real_save = ptc.TrackedFiles.save
        try:
            ptc.TrackedFiles.save = lambda t: (writes.append(1), real_save(t))[1]
            ptc.TrackedFiles.remove_many([])
            ptc.TrackedFiles.remove_many(["/mnt/cache/Media/not-tracked.mkv"])
        finally:
            ptc.TrackedFiles.save = real_save
        self.assertEqual(writes, [])

    def test_nested_changes_do_not_wait_on_their_own_lock(self):
        """flock belongs to the open file: a second open and flock inside the
        first would block for ever."""
        with ptc.TrackedFiles.locked():
            ptc.TrackedFiles.add("/mnt/cache/Media/a.mkv")
        self.assertIn("/mnt/cache/Media/a.mkv", ptc.TrackedFiles.load())


class MoveQueue(StateInTempDir):
    """Requests from the web UI and the service must not be lost - not when
    several arrive at once, and not when one arrives while a move runs."""

    def test_requests_come_out_in_order_and_the_queue_is_left_empty(self):
        ptc._queue_append(["pick\t/mnt/cache/Media/A"])
        ptc._queue_append(["auto\t/mnt/cache/Media/b.mkv", "pick\t/mnt/cache/Media/C"])
        self.assertEqual(ptc._queue_take(), [("pick", "/mnt/cache/Media/A"),
                                             ("auto", "/mnt/cache/Media/b.mkv"),
                                             ("pick", "/mnt/cache/Media/C")])
        self.assertTrue(os.path.exists(ptc.MOVE_QUEUE),
                        "emptied, not deleted: a writer may have it open")
        self.assertEqual(ptc._queue_take(), [])

    def test_malformed_lines_are_ignored(self):
        Path(ptc.MOVE_QUEUE).write_text("no tab here\nbogus\t/x\npick\t\npick\t/ok\n")
        self.assertEqual(ptc._queue_take(), [("pick", "/ok")])

    def test_paths_keep_their_spaces(self):
        ptc._queue_append(["pick\t/mnt/cache/Media/ A folder "])
        self.assertEqual(ptc._queue_take(), [("pick", "/mnt/cache/Media/ A folder ")])


class Mover(StateInTempDir):
    """The mover runs in a process of its own, so that Stop leaves it running."""

    def setUp(self):
        super().setUp()
        configure(ARRAY_ROOT="/mnt/user", CACHE_ROOT="/mnt/cache")
        self.batches = []
        patches = [
            mock.patch.object(ptc, 'setup_logging'),
            mock.patch.object(ptc, 'load_config'),
            mock.patch.object(ptc.signal, 'signal'),
            mock.patch.object(ptc, '_start_mover'),
            mock.patch.object(ptc, '_move_batch',
                              side_effect=lambda reqs, seen: self.batches.append(reqs)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_it_works_through_the_queue_and_leaves_no_pid_behind(self):
        ptc._queue_append(["pick\t/mnt/cache/Media/A"])
        self.assertEqual(ptc.run_mover(), 0)
        self.assertEqual(self.batches, [[("pick", "/mnt/cache/Media/A")]])
        self.assertFalse(os.path.exists(ptc.MOVE_PID))
        status = ptc._read_json(ptc.MOVE_STATUS)
        self.assertFalse(status["active"])
        self.assertGreater(status["finished"], 0)

    def test_a_second_mover_leaves_the_queue_to_the_first(self):
        ptc._queue_append(["pick\t/mnt/cache/Media/A"])
        with open(ptc.MOVE_LOCK, "a") as held:
            ptc.fcntl.flock(held, ptc.fcntl.LOCK_EX)
            self.assertTrue(ptc.mover_running())
            self.assertEqual(ptc.run_mover(), 0)
        self.assertEqual(self.batches, [])
        self.assertEqual(ptc._queue_take(), [("pick", "/mnt/cache/Media/A")])
        self.assertFalse(ptc.mover_running())

    def test_a_request_arriving_as_it_finishes_is_not_lost(self):
        """Queued after the last look but before the lock is let go: its sender
        saw a mover running and started none."""
        real_finish = ptc._job_finish
        late = ["pick\t/mnt/cache/Media/late"]

        def finish_then_request():
            real_finish()
            if late:
                ptc._queue_append([late.pop()])

        with mock.patch.object(ptc, '_job_finish', side_effect=finish_then_request):
            ptc._queue_append(["pick\t/mnt/cache/Media/first"])
            ptc.run_mover()
        self.assertEqual(self.batches, [[("pick", "/mnt/cache/Media/first")],
                                        [("pick", "/mnt/cache/Media/late")]])

    def test_stop_drops_what_is_still_queued(self):
        ptc._shutting_down.set()
        ptc._queue_append(["pick\t/mnt/cache/Media/A", "pick\t/mnt/cache/Media/B"])
        ptc.run_mover()
        self.assertEqual(self.batches, [])
        status = ptc._read_json(ptc.MOVE_STATUS)
        self.assertTrue(status["stopped"])
        self.assertEqual(status["dropped"], 2)
        self.assertEqual(ptc._queue_take(), [])

    def test_the_stop_button_ends_the_running_transfer(self):
        proc = mock.Mock()
        ptc._current_rsync = proc
        try:
            ptc._request_stop()
        finally:
            ptc._current_rsync = None
        proc.terminate.assert_called_once()
        self.assertTrue(ptc._shutting_down.is_set())
        self.assertTrue(ptc._job["stopped"])

    def test_protection_includes_what_the_running_service_reports(self):
        ptc._stream_poll.update(at=time.time(), paths=frozenset({"/mnt/cache/Media/playing.mkv"}))
        ptc._write_json(ptc.STATUS_FILE, {"updated": int(time.time()),
                                          "protected": ["/mnt/cache/Media/copying.mkv"]})
        self.assertEqual(ptc._protected_now(), {"/mnt/cache/Media/playing.mkv",
                                                "/mnt/cache/Media/copying.mkv"})
        # A snapshot left by a service that has since stopped says nothing.
        ptc._write_json(ptc.STATUS_FILE, {"updated": int(time.time()) - 3600,
                                          "protected": ["/mnt/cache/Media/copying.mkv"]})
        self.assertEqual(ptc._protected_now(), {"/mnt/cache/Media/playing.mkv"})


class AutoCleanup(StateInTempDir):
    """Auto Cleanup hands its moves to the mover instead of doing them in the
    main loop, which stopped noticing new streams for as long as they took."""

    def setUp(self):
        super().setUp()
        configure(ARRAY_ROOT="/mnt/user", CACHE_ROOT="/mnt/cache")

    def test_what_is_playing_or_copying_or_already_asked_for_is_left_out(self):
        ptc.active_cache_paths = {"/mnt/cache/Media/playing.mkv"}
        ptc._pending_copies = {"/mnt/user/Media/copying.mkv"}
        with mock.patch.object(ptc, 'queue_moves') as queue_moves:
            ptc.request_moves(["/mnt/cache/Media/playing.mkv", "/mnt/cache/Media/copying.mkv",
                               "/mnt/cache/Media/done.mkv"], "Cleanup")
            ptc.request_moves(["/mnt/cache/Media/done.mkv"], "Cleanup")
        queue_moves.assert_called_with("auto", [])
        self.assertEqual(queue_moves.call_args_list[0][0], ("auto", ["/mnt/cache/Media/done.mkv"]))

    def test_smart_cleanup_only_sends_back_what_the_plugin_cached(self):
        tmp_cache = os.path.join(self.tmp, "cache")
        tmp_array = os.path.join(self.tmp, "user")
        configure(ARRAY_ROOT=tmp_array, CACHE_ROOT=tmp_cache, CLEANUP_MODE="smart",
                  EPISODE_KEEP_PREVIOUS="1")
        season = os.path.join(tmp_cache, "Show", "S01")
        ours = self.make(os.path.join(season, "Show.S01E01.mkv"))
        self.make(os.path.join(season, "Show.S01E02.mkv"))   # a download, untracked
        self.make(os.path.join(tmp_array, "Show", "S01", "Show.S01E05.mkv"))
        ptc.TrackedFiles.save({ours: 1.0})
        with mock.patch.object(ptc, 'request_moves') as request, \
             mock.patch.object(ptc, 'enqueue_copy'):
            ptc.handle_series(os.path.join(tmp_array, "Show", "S01", "Show.S01E05.mkv"))
        request.assert_called_once_with([ours], "Smart Cleanup")


class DaemonPass(StateInTempDir):
    """One round of the main loop with somebody watching episode 5."""

    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.tmp, "cache")
        self.array = os.path.join(self.tmp, "user")
        configure(ARRAY_ROOT=self.array, CACHE_ROOT=self.cache, COPY_DELAY="0",
                  CLEANUP_MODE="smart", EPISODE_KEEP_PREVIOUS="2",
                  DOCKER_MAPPINGS="/tv:" + os.path.join(self.array, "TV"))
        self.season = os.path.join(self.array, "TV", "Show", "S01")
        for e in range(1, 7):
            self.make(os.path.join(self.season, f"Show.S01E0{e}.mkv"))
        cached = {}
        for e in (1, 2, 3):
            src = os.path.join(self.season, f"Show.S01E0{e}.mkv")
            dst = ptc.array_to_cache(src)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            cached[dst] = 1.0
        ptc.TrackedFiles.save(cached)
        ptc.stream_timers.clear()

    def test_upcoming_episodes_are_copied_and_old_ones_sent_back(self):
        streams = {"/tv/Show/S01/Show.S01E05.mkv": {"service": "plex", "id": "1", "progress": 0.5}}
        state = {"storage": None, "last_streams": {}, "last_days_check": 0}
        with mock.patch.object(ptc, 'get_active_streams', return_value=streams), \
             mock.patch.object(ptc, 'reconcile_tracked_files'), \
             mock.patch.object(ptc, 'enqueue_copy') as enqueue, \
             mock.patch.object(ptc, 'queue_moves') as queue_moves:
            ptc._daemon_pass(state)                  # the stream is noticed
            enqueue.assert_not_called()
            protected = ptc._daemon_pass(state)      # the copy delay has passed

        copied = sorted(os.path.basename(c[0][0]) for c in enqueue.call_args_list)
        self.assertEqual(copied, ["Show.S01E05.mkv", "Show.S01E06.mkv"])
        queue_moves.assert_called_once()
        kind, paths = queue_moves.call_args[0]
        self.assertEqual(kind, "auto")
        self.assertEqual(sorted(os.path.basename(p) for p in paths),
                         ["Show.S01E01.mkv", "Show.S01E02.mkv"], "E03 is within the two kept")
        self.assertIn(ptc.array_to_cache(os.path.join(self.season, "Show.S01E05.mkv")), protected)

        ptc.write_status(protected)
        status = ptc._read_json(ptc.STATUS_FILE)
        self.assertEqual(status["cached_files"], 3)
        self.assertEqual(status["stream_paths"],
                         [ptc.array_to_cache(os.path.join(self.season, "Show.S01E05.mkv"))])


class MoverHandoff(StateInTempDir):

    def test_a_mover_is_started_only_when_none_is_running(self):
        with mock.patch.object(ptc.subprocess, 'Popen') as popen:
            ptc.queue_moves("auto", ["/mnt/cache/Media/a.mkv"])
            self.assertEqual(popen.call_count, 1)
            args, kwargs = popen.call_args
            self.assertEqual(args[0][-1], "--move")
            self.assertTrue(kwargs.get("start_new_session"),
                            "in a session of its own, or stopping the service takes it along")
            with open(ptc.MOVE_LOCK, "a") as held:
                ptc.fcntl.flock(held, ptc.fcntl.LOCK_EX)
                ptc.queue_moves("auto", ["/mnt/cache/Media/b.mkv"])
            self.assertEqual(popen.call_count, 1, "the running mover takes the new request")
        self.assertEqual([p for _k, p in ptc._queue_take()],
                         ["/mnt/cache/Media/a.mkv", "/mnt/cache/Media/b.mkv"])
        ptc._movers.clear()


class StopSparesTheMover(unittest.TestCase):
    """Stopping the service, or updating the plugin, must not end a move to
    the array. Both find the service by its command line, and the mover's is
    the same one with --move at the end."""

    SCRIPT = "/usr/local/emhttp/plugins/plex_to_cache/scripts/plex_to_cache.py"
    SERVICE = f"python3 {SCRIPT}"
    MOVER = f"/usr/bin/python3 {SCRIPT} --move"

    def test_the_stop_pattern_matches_the_service_only(self):
        rc = (ROOT / "src" / "rc.plex_to_cache").read_text()
        self.assertIn('pkill -f "$SERVICE_PATTERN"', rc)
        # Let bash expand the two assignments, rather than guessing at quoting.
        lines = [l for l in rc.splitlines() if l.startswith(("PYTHON_SCRIPT=", "SERVICE_PATTERN="))]
        out = subprocess.run(["bash", "-c", "\n".join(lines) + '\nprintf %s "$SERVICE_PATTERN"'],
                             capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        pattern = out.stdout
        self.assertEqual(pattern, self.SCRIPT + "$")
        self.assertTrue(re.search(pattern, self.SERVICE))
        self.assertFalse(re.search(pattern, self.MOVER))

    def _pkill_patterns(self, block):
        return [m.group(1) or m.group(2)
                for m in re.finditer(r"pkill -f (?:'([^']+)'|(\S+))", block)]

    def test_an_update_spares_the_mover_and_an_uninstall_does_not(self):
        plg = (ROOT / "plex_to_cache.plg").read_text()
        pre_install = plg.split('<FILE Run="/bin/bash">', 1)[1].split("</FILE>", 1)[0]
        uninstall = plg.split('Method="remove">', 1)[1].split("</FILE>", 1)[0]

        for pattern in self._pkill_patterns(pre_install):
            self.assertTrue(re.search(pattern, self.SERVICE), pattern)
            self.assertFalse(re.search(pattern, self.MOVER), f"an update would end the move: {pattern}")
        patterns = self._pkill_patterns(uninstall)
        self.assertTrue(any(re.search(p, self.MOVER) for p in patterns),
                        "an uninstall has to end the mover too")

    def test_the_array_events_are_installed(self):
        plg = (ROOT / "plex_to_cache.plg").read_text()
        for event in ("stopping", "disks_mounted"):
            self.assertIn(f'plugins/plex_to_cache/event/{event}" Mode="0755"', plg)


class WebUiActions(unittest.TestCase):
    """A request for an action the PHP does not handle fell through to the
    settings save - with none of the form's fields in it, which switched every
    media server off and cleared the Docker mappings."""

    def setUp(self):
        self.php = (ROOT / "src" / "plex_to_cache.php").read_text()

    def test_every_action_the_page_calls_has_a_handler(self):
        called = set(re.findall(r"plex_to_cache\.php\?action=([a-z_]+)", self.php))
        handled = set(re.findall(r"\$ptc_action === '([a-z_]+)'", self.php))
        self.assertTrue(called)
        self.assertLessEqual(called, handled, f"no handler for {sorted(called - handled)}")

    def test_an_unknown_action_is_refused_before_the_save(self):
        guard = self.php.index("if ($ptc_action !== '')")
        save = self.php.index("// POST: Save settings")
        self.assertLess(guard, save)

    def test_the_web_ui_starts_the_script_that_is_installed(self):
        """It pointed at plugins/plex_to_cache/plex_to_cache.py, which does not
        exist, so moving anything with the service stopped always failed."""
        build = (ROOT / "build_plg.py").read_text()
        installed = re.search(r'<FILE Name="(/[^"]+/plex_to_cache\.py)"', build).group(1)
        self.assertIn(f'$ptc_daemon_script = "{installed.replace("plex_to_cache/", "$ptc_plugin/", 1)}"',
                      self.php)


if __name__ == "__main__":
    unittest.main(verbosity=2)
