package mtc

import (
	"fmt"
	"os"
	"sync"
	"testing"
	"time"
)

func TestMigrateTrackedList(t *testing.T) {
	newSandbox(t)
	os.WriteFile(LegacyList, []byte("/mnt/cache/Media/a.mkv|1700000000.5\n/mnt/cache/Media/b|c.mkv|1700000001\n/mnt/cache/Media/old.mkv\n\n"), 0o644)
	migrateTracked()
	tr := loadTracked()
	if tr["/mnt/cache/Media/a.mkv"] != 1700000000.5 || tr["/mnt/cache/Media/b|c.mkv"] != 1700000001 || tr["/mnt/cache/Media/old.mkv"] < float64(time.Now().Unix()-5) {
		t.Fatalf("%v", tr)
	}
	if exists(LegacyList) {
		t.Fatal("old list left")
	}
}

func TestTrackedConcurrentWriters(t *testing.T) {
	newSandbox(t)
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			trackAdd(fmt.Sprintf("/x/%d.mkv", i))
		}(i)
	}
	wg.Wait()
	if n := len(loadTracked()); n != 20 {
		t.Fatalf("%d entries", n)
	}
}

func TestSameFilesystemNeverDeleted(t *testing.T) {
	sb := newSandbox(t)
	sb.mounts("cache fuse.shfs", "user fuse.shfs", "user0 fuse.shfs")
	c := sb.file(MntDir+"/cache/Media/a.mkv", 10, past)
	sb.file(MntDir+"/user0/Media/a.mkv", 10, past)
	if _, err := twin(c); err == nil {
		t.Fatal("twin on one filesystem")
	}
	if res, _ := moveFileToArray(c, nil); res != resFailed || !exists(c) {
		t.Fatalf("moved: %s", res)
	}
}

func TestExit23OlderTargetKeepsSource(t *testing.T) {
	sb := newSandbox(t)
	c := sb.file(MntDir+"/cache/Media/a.mkv", 10, past)
	os.Chtimes(c, time.Now(), time.Now())
	os.Setenv("FAKE_RSYNC_EXIT", "23")
	defer os.Unsetenv("FAKE_RSYNC_EXIT")
	sb.file(MntDir+"/user0/Media/b.mkv", 10, past)
	if err := transfer(c, MntDir+"/user0/Media/b.mkv", true, nil); err == nil || !exists(c) {
		t.Fatal("older target counted as complete")
	}
	os.Chtimes(MntDir+"/user0/Media/b.mkv", time.Now(), time.Now())
	if err := transfer(c, MntDir+"/user0/Media/b.mkv", true, nil); err != nil || exists(c) {
		t.Fatalf("complete target not accepted: %v", err)
	}
}

func TestNewerWins(t *testing.T) {
	sb := newSandbox(t)
	newer := time.Now().Add(-time.Hour)
	c := sb.file(MntDir+"/cache/Media/a.mkv", 20, newer)
	a := sb.file(MntDir+"/user0/Media/a.mkv", 10, past)
	if res, _ := moveFileToArray(c, nil); res != resMoved || exists(c) {
		t.Fatalf("cache newer: %s", res)
	}
	if st, _ := os.Stat(a); st.Size() != 20 {
		t.Fatal("array not replaced")
	}
	c = sb.file(MntDir+"/cache/Media/b.mkv", 20, past)
	sb.file(MntDir+"/user0/Media/b.mkv", 10, newer)
	if res, _ := moveFileToArray(c, nil); res != resDropped || exists(c) {
		t.Fatalf("array newer: %s", res)
	}
	c = sb.file(MntDir+"/cache/Media/c.mkv", 20, past)
	sb.file(MntDir+"/user0/Media/c.mkv", 10, past)
	if res, _ := moveFileToArray(c, nil); res != resKept || !exists(c) {
		t.Fatalf("unclear: %s", res)
	}
	c = sb.file(MntDir+"/cache/New/d.mkv", 5, past)
	if res, _ := moveFileToArray(c, nil); res != resMoved || !exists(MntDir+"/user0/New/d.mkv") {
		t.Fatalf("only on cache: %s", res)
	}
}

func TestNothingWithoutStorage(t *testing.T) {
	sb := newSandbox(t)
	trackAdd(MntDir + "/cache/Media/a.mkv")
	os.Remove(MntDir + "/cache/.keep")
	if storageReady() {
		t.Fatal("empty mount point counted as ready")
	}
	d := newDaemon()
	d.pass()
	sb.file(MntDir+"/user0/Media/x.mkv", 5, past)
	d.copyFile(MntDir + "/user/Media/x.mkv")
	if d.evict(1, MntDir+"/cache/Media/x.mkv", space{100, 100, 0}) || len(loadTracked()) != 1 || exists(MntDir+"/cache/Media/x.mkv") {
		t.Fatal("judged the list without storage")
	}
}

func TestReconcileAdoptsOnlyCopies(t *testing.T) {
	sb := newSandbox(t)
	sb.file(MntDir+"/cache/Media/same.mkv", 10, past)
	sb.file(MntDir+"/user0/Media/same.mkv", 10, past)
	sb.file(MntDir+"/cache/Media/diff.mkv", 11, past)
	sb.file(MntDir+"/user0/Media/diff.mkv", 10, past)
	sb.file(MntDir+"/cache/Other/out.mkv", 10, past)
	sb.file(MntDir+"/user0/Other/out.mkv", 10, past)
	trackAdd(MntDir + "/cache/Media/gone.mkv")
	reconcile()
	tr := loadTracked()
	if _, ok := tr[MntDir+"/cache/Media/same.mkv"]; !ok || len(tr) != 1 {
		t.Fatalf("%v", tr)
	}
}

func TestOwnerNotThroughSymlink(t *testing.T) {
	sb := newSandbox(t)
	target := sb.file(sb.Root+"/target", 1, past)
	os.Chmod(target, 0o600)
	os.Symlink(target, sb.Root+"/link")
	st, _ := os.Stat(sb.file(sb.Root+"/orig", 1, past))
	setOwner(sb.Root+"/link", st, true)
	if s, _ := os.Stat(target); s.Mode().Perm() != 0o600 {
		t.Fatal("followed the symlink")
	}
}

func TestStopEndsRsync(t *testing.T) {
	sb := newSandbox(t)
	c := sb.file(MntDir+"/cache/Media/a.mkv", 10, past)
	os.Setenv("FAKE_RSYNC_SLEEP", "5")
	defer os.Unsetenv("FAKE_RSYNC_SLEEP")
	go func() { time.Sleep(300 * time.Millisecond); stopAll() }()
	start := time.Now()
	res, _ := moveFileToArray(c, nil)
	if res != resStopped || !exists(c) || time.Since(start) > 3*time.Second || exists(MntDir+"/user0/Media") {
		t.Fatalf("%s after %s", res, time.Since(start))
	}
}
