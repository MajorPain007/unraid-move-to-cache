package mtc

import (
	"fmt"
	"net/http/httptest"
	"os"
	"sync"
	"testing"
)

func TestSymlinkOnCacheNeverDeletesTheOriginal(t *testing.T) {
	sb := newSandbox(t)
	a := sb.file(MntDir+"/user0/Media/Movies/a.mkv", 10, past)
	os.MkdirAll(MntDir+"/cache/Media", 0o755)
	os.Symlink(MntDir+"/user0/Media/Movies", MntDir+"/cache/Media/Movies")
	c := MntDir + "/cache/Media/Movies/a.mkv"
	if res, _ := moveFileToArray(c, nil); res == resDropped || res == resMoved || !exists(a) {
		t.Fatalf("%s, original exists: %v", res, exists(a))
	}
	d := newDaemon()
	d.copyFile(MntDir + "/user/Media/Movies/a.mkv")
	if _, ok := loadTracked()[c]; ok {
		t.Fatal("symlinked file taken for a cache copy")
	}
}

func TestStreamPathBoundary(t *testing.T) {
	sb := newSandbox(t)
	sb.settings(map[string]string{"ARRAY_ROOT": MntDir + "/user/Media", "DOCKER_MAPPINGS": "/data:" + MntDir + "/user/Media2"})
	if p, ok := C.streamArrayPath("/data/x.mkv"); ok {
		t.Fatalf("sibling folder accepted: %s", p)
	}
}

func TestBrowseStaysInMappedFolders(t *testing.T) {
	sb := newSandbox(t)
	sb.file(MntDir+"/cache/Media/a.mkv", 1, past)
	os.MkdirAll(MntDir+"/cache/Other", 0o755)
	os.Symlink(MntDir+"/cache/Other", MntDir+"/cache/Media/link")
	for _, p := range []string{"/etc", MntDir + "/cache/Other", MntDir + "/cache/Media/link", MntDir + "/cache/Media/../Other"} {
		if r := apiDispatch("browse", M{"path": p}); r["ok"] != false {
			t.Errorf("%s listed", p)
		}
	}
	if r := apiDispatch("browse", M{"path": MntDir + "/cache/Media"}); r["ok"] != true {
		t.Fatalf("mapped folder refused: %v", r)
	}
}

func TestSavedTokenOnlyToSavedAddress(t *testing.T) {
	sb := newSandbox(t)
	ts := httptest.NewServer(fakePlex(t, "tok"))
	defer ts.Close()
	sb.settings(map[string]string{"PLEX_TOKEN": "tok", "PLEX_URL": "http://192.0.2.1:32400"})
	r := apiDispatch("test", M{"service": "plex", "url": ts.URL})
	if r["success"] != false || r["kind"] != "token" {
		t.Fatalf("saved token sent to a new address: %v", r)
	}
}

func TestTrackedConcurrentReaders(t *testing.T) {
	newSandbox(t)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(2)
		go func(i int) { defer wg.Done(); trackAdd(fmt.Sprintf("/x/%d", i)) }(i)
		go func() { defer wg.Done(); loadTracked() }()
	}
	wg.Wait()
}
