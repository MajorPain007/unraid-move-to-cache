package mtc

import (
	"os"
	"strings"
	"testing"
	"time"
)

func TestMoverExitProtocol(t *testing.T) {
	sb := newSandbox(t)
	c := sb.file(MntDir+"/cache/Media/late.mkv", 5, past)
	trackAdd(c)
	SelfBin = sb.Root + "/none"
	moverAfterUnlock = func() {
		moverAfterUnlock = func() {}
		queueAppend([]string{"auto\t" + c})
	}
	defer func() { moverAfterUnlock = func() {} }()
	if runMover() != 0 {
		t.Fatal("mover failed")
	}
	if exists(c) || !exists(MntDir+"/user0/Media/late.mkv") {
		t.Fatal("request after unlock was lost")
	}
}

func TestMoverSkips(t *testing.T) {
	sb := newSandbox(t)
	dir := MntDir + "/cache/Media/Show"
	playing := sb.file(dir+"/S01E01.mkv", 5, past)
	fresh := sb.file(dir+"/S01E02.mkv", 5, time.Now())
	other := sb.file(dir+"/S01E03.mkv", 5, past)
	sb.file(MntDir+"/user0/Media/Show/S01E03.mkv", 9, past)
	ok := sb.file(dir+"/S01E04.mkv", 5, past)
	fileWrite(StatusFile, jsonEncode(M{"updated": time.Now().Unix(), "protected": []string{playing}}), 0o644)
	queueAppend([]string{"pick\t" + dir})
	runMover()
	var job moveJob
	jsonReadInto(MoveStatus, &job)
	if job.Skipped != 1 || job.Recent != 1 || job.Conflicts != 1 || job.Done != 1 || exists(ok) || !exists(playing) || !exists(fresh) || !exists(other) {
		t.Fatalf("%+v", job)
	}
	for _, s := range []string{"being played", "written in the last", "different file of that name"} {
		if !strings.Contains(sb.log(), s) {
			t.Errorf("log lacks %q", s)
		}
	}
}

func TestMoverRefusesUntrackedAutoAndOutside(t *testing.T) {
	sb := newSandbox(t)
	untracked := sb.file(MntDir+"/cache/Media/a.mkv", 5, past)
	outside := sb.file(MntDir+"/cache/Other/b.mkv", 5, past)
	os.Symlink(MntDir+"/cache/Other", MntDir+"/cache/Media/link")
	queueAppend([]string{"auto\t" + untracked, "pick\t" + outside, "pick\t" + MntDir + "/cache/Media/link", "pick\t" + MntDir + "/cache/Media/../Other"})
	runMover()
	if !exists(untracked) || !exists(outside) {
		t.Fatal("moved what it must not")
	}
}

func TestMoverOnce(t *testing.T) {
	sb := newSandbox(t)
	c := sb.file(MntDir+"/cache/Media/a.mkv", 5, past)
	trackAdd(c)
	os.Setenv("FAKE_RSYNC_LOG", sb.Root+"/rsync.log")
	defer os.Unsetenv("FAKE_RSYNC_LOG")
	queueAppend([]string{"auto\t" + c, "pick\t" + c, "pick\t" + MntDir + "/cache/Media"})
	runMover()
	b, _ := os.ReadFile(sb.Root + "/rsync.log")
	if strings.Count(string(b), "\n") != 1 {
		t.Fatalf("rsync runs: %q", b)
	}
	if len(loadTracked()) != 0 {
		t.Fatal("still tracked")
	}
}
