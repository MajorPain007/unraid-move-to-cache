package mtc

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeServer struct {
	mu       sync.Mutex
	sessions []string
	fail     bool
	ts       *httptest.Server
}

func (f *fakeServer) set(fail bool, sessions ...string) {
	f.mu.Lock()
	f.fail, f.sessions = fail, sessions
	f.mu.Unlock()
}

func newFake(t *testing.T, sb *sandbox) *fakeServer {
	f := &fakeServer{}
	f.ts = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		if f.fail {
			w.WriteHeader(500)
			return
		}
		var items []string
		for i, s := range f.sessions {
			path, prog, _ := strings.Cut(s, "@")
			if prog == "" {
				prog = "10"
			}
			items = append(items, fmt.Sprintf(`{"ratingKey":"%d","title":"t","viewOffset":%s,"duration":100,"Media":[{"Part":[{"file":%q}]}]}`, i, prog, path))
		}
		w.Write([]byte(`{"MediaContainer":{"Metadata":[` + strings.Join(items, ",") + `]}}`))
	}))
	t.Cleanup(f.ts.Close)
	sb.settings(map[string]string{"ENABLE_PLEX": "yes", "PLEX_URL": f.ts.URL, "PLEX_TOKEN": "t", "COPY_DELAY": "0", "CLEANUP_MODE": "smart", "MOVIE_DELETE_DELAY": "0"})
	return f
}

func (d *daemon) queued() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := append([]string{}, d.order...)
	sort.Strings(out)
	return out
}

func (d *daemon) drain() {
	for {
		p, ok := d.next()
		if !ok {
			return
		}
		d.copyFile(p)
		d.mu.Lock()
		delete(d.pending, p)
		d.mu.Unlock()
	}
}

func TestMovieCopied(t *testing.T) {
	sb := newSandbox(t)
	f := newFake(t, sb)
	m := MntDir + "/user0/Media/Film"
	sb.file(m+"/Film.mkv", 5, past)
	sb.file(m+"/Film.mp4", 5, past)
	sb.file(m+"/Film 2.mkv", 5, past)
	f.set(false, "/media/Film/Film.mkv")
	d := newDaemon()
	d.pass()
	d.pass()
	q := d.queued()
	if len(q) != 2 || !strings.HasSuffix(q[0], "Film.mkv") || !strings.HasSuffix(q[1], "Film.mp4") {
		t.Fatalf("%v", q)
	}
	d.drain()
	if _, ok := loadTracked()[MntDir+"/cache/Media/Film/Film.mkv"]; !ok || !exists(MntDir+"/cache/Media/Film/Film.mkv") {
		t.Fatal("not copied or not tracked")
	}
}

func TestSelectBatch(t *testing.T) {
	r := func(a, b int) []int {
		var o []int
		for i := a; i <= b; i++ {
			o = append(o, i)
		}
		return o
	}
	cases := []struct {
		all          []int
		cur, s, t, p int
		want         []int
	}{
		{r(1, 36), 5, 30, 10, 4, r(5, 36)},
		{r(1, 50), 27, 30, 10, 4, r(27, 50)},
		{r(1, 50), 10, 30, 10, 4, r(10, 30)},
		{r(1, 65), 31, 30, 5, 4, r(31, 65)},
		{r(1, 100), 1, 30, 10, 4, r(1, 30)},
		{r(1, 100), 26, 30, 10, 4, r(26, 60)},
		{r(1, 100), 95, 30, 10, 4, r(91, 100)[4:]},
	}
	for _, c := range cases {
		got := fmt.Sprint(selectBatch(c.all, c.cur, c.s, c.t, c.p))
		if got != fmt.Sprint(c.want) {
			t.Errorf("%d of %d: %s, want %v", c.cur, len(c.all), got, c.want)
		}
	}
}

func TestFailedPollKeepsStreams(t *testing.T) {
	sb := newSandbox(t)
	f := newFake(t, sb)
	c := sb.file(MntDir+"/cache/Media/Film/Film.mkv", 5, past)
	sb.file(MntDir+"/user0/Media/Film/Film.mkv", 5, past)
	trackAdd(c)
	f.set(false, "/media/Film/Film.mkv@95")
	d := newDaemon()
	d.pass()
	f.set(true)
	d.pass()
	if len(d.deletion) != 0 || len(d.lastStream) != 1 || !d.active[c] {
		t.Fatalf("failed poll counted as stream end: %v %v", d.deletion, d.lastStream)
	}
	f.set(false, "/media/Film/Film.mkv@96")
	d.pass()
	if len(d.deletion) != 0 {
		t.Fatal("cleanup while still playing")
	}
}

type counter struct {
	mu sync.Mutex
	n  int
}

func TestNoArrayAccessForHandledStream(t *testing.T) {
	sb := newSandbox(t)
	f := newFake(t, sb)
	for i := 1; i <= 5; i++ {
		sb.file(fmt.Sprintf("%s/user0/Media/Show/S01/Show S01E%02d.mkv", MntDir, i), 5, past)
	}
	f.set(false, "/media/Show/S01/Show S01E02.mkv")
	var c counter
	arrayRoot := MntDir + "/user"
	orig, origStat := fsReadDir, fsStat
	fsReadDir = func(p string) ([]os.DirEntry, error) {
		if under(p, arrayRoot) || under(p, MntDir+"/user0") {
			c.mu.Lock()
			c.n++
			c.mu.Unlock()
		}
		return orig(p)
	}
	fsStat = func(p string) (os.FileInfo, error) {
		if under(p, arrayRoot) || under(p, MntDir+"/user0") {
			c.mu.Lock()
			c.n++
			c.mu.Unlock()
		}
		return origStat(p)
	}
	defer func() { fsReadDir, fsStat = orig, origStat }()
	d := newDaemon()
	d.pass()
	d.pass()
	d.drain()
	reads := 0
	trackedRead := 0
	trackedCache.data = loadTracked()
	before := trackedCache.mtime
	c.n = 0
	for i := 0; i < 10; i++ {
		d.pass()
		d.writeStatus()
		d.drain()
		if !trackedCache.mtime.Equal(before) {
			trackedRead++
		}
	}
	reads = c.n
	if reads != 0 || trackedRead != 0 {
		t.Fatalf("%d array accesses, %d list reads after the first round", reads, trackedRead)
	}
}

func TestSmartCleanupMovie(t *testing.T) {
	sb := newSandbox(t)
	f := newFake(t, sb)
	for name, prog := range map[string]string{"Seen": "95", "Half": "50"} {
		c := sb.file(MntDir+"/cache/Media/"+name+"/"+name+".mkv", 5, past)
		sb.file(MntDir+"/user0/Media/"+name+"/"+name+".mkv", 5, past)
		trackAdd(c)
		f.set(false, "/media/"+name+"/"+name+".mkv@"+prog)
		os.WriteFile(sb.Root+"/mtc", []byte("#!/bin/sh\n"), 0o755)
		SelfBin = sb.Root + "/mtc"
		d := newDaemon()
		d.pass()
		f.set(false)
		d.pass()
		time.Sleep(10 * time.Millisecond)
		d.pass()
	}
	b, _ := os.ReadFile(MoveQueue)
	if !strings.Contains(string(b), "auto\t"+MntDir+"/cache/Media/Seen/Seen.mkv") || strings.Contains(string(b), "Half") {
		t.Fatalf("queue %q", b)
	}
}

func TestFinaleNotGuessed(t *testing.T) {
	sb := newSandbox(t)
	f := newFake(t, sb)
	for i := 1; i <= 2; i++ {
		c := sb.file(fmt.Sprintf("%s/cache/Media/Show/S01/Show S01E0%d.mkv", MntDir, i), 5, past)
		sb.file(fmt.Sprintf("%s/user0/Media/Show/S01/Show S01E0%d.mkv", MntDir, i), 5, past)
		trackAdd(c)
	}
	f.set(false, "/media/Show/S01/Show S01E02.mkv@95")
	d := newDaemon()
	d.pass()
	orig := fsReadDir
	fsReadDir = func(p string) ([]os.DirEntry, error) { return nil, os.ErrPermission }
	defer func() { fsReadDir = orig }()
	f.set(false)
	d.pass()
	if len(d.deletion) != 0 {
		t.Fatal("season cleared on an unreadable folder")
	}
}

func ptr(f float64) *float64 { return &f }

func TestEvictionOldestFirstNeverPlaying(t *testing.T) {
	sb := newSandbox(t)
	newFake(t, sb)
	for i, name := range []string{"old", "playing", "mid", "new"} {
		c := sb.file(MntDir+"/cache/Media/"+name+".mkv", 5, past)
		sb.file(MntDir+"/user0/Media/"+name+".mkv", 5, past)
		updateTracked(map[string]float64{c: float64(1000 + i)}, nil)
	}
	d := newDaemon()
	d.active[MntDir+"/cache/Media/playing.mkv"] = true
	sp := space{Used: 0, Total: 1 << 40, Free: roomMargin}
	free := sp.Free
	ok := d.evict(1<<40, "", space{Used: 0, Total: 1 << 41, Free: free})
	if ok || exists(MntDir+"/cache/Media/new.mkv") || !exists(MntDir+"/cache/Media/playing.mkv") {
		t.Fatalf("ok=%v", ok)
	}
	if !strings.Contains(sb.notified(), "Cache is full") {
		t.Fatal("no notification")
	}
}

func TestEvictionStopsWhenRoom(t *testing.T) {
	sb := newSandbox(t)
	for i, name := range []string{"a", "b", "c"} {
		c := sb.file(MntDir+"/cache/Media/"+name+".mkv", 100, past)
		sb.file(MntDir+"/user0/Media/"+name+".mkv", 100, past)
		updateTracked(map[string]float64{c: float64(i)}, nil)
	}
	d := newDaemon()
	if !d.evict(150, "", space{Used: 0, Total: 1 << 40, Free: roomMargin}) {
		t.Fatal("no room made")
	}
	if exists(MntDir+"/cache/Media/a.mkv") || exists(MntDir+"/cache/Media/b.mkv") || !exists(MntDir+"/cache/Media/c.mkv") {
		t.Fatal("wrong files evicted")
	}
}

func TestOfflineNotification(t *testing.T) {
	sb := newSandbox(t)
	f := newFake(t, sb)
	f.set(true)
	clock := time.Now()
	nowFn = func() time.Time { return clock }
	defer func() { nowFn = time.Now }()
	d := newDaemon()
	d.pass()
	clock = clock.Add(9 * time.Minute)
	d.pass()
	if sb.notified() != "" {
		t.Fatal("notified too early")
	}
	clock = clock.Add(2 * time.Minute)
	d.pass()
	d.pass()
	if strings.Count(sb.notified(), "not reachable") != 1 {
		t.Fatalf("%q", sb.notified())
	}
	f.set(false)
	d.pass()
	f.set(true)
	d.pass()
	clock = clock.Add(11 * time.Minute)
	d.pass()
	if strings.Count(sb.notified(), "not reachable") != 2 {
		t.Fatalf("%q", sb.notified())
	}
}

func TestCertificateNotification(t *testing.T) {
	sb := newSandbox(t)
	ts := httptest.NewTLSServer(fakePlex(t, "t"))
	defer ts.Close()
	sb.settings(map[string]string{"ENABLE_PLEX": "yes", "PLEX_URL": ts.URL, "PLEX_TOKEN": "t"})
	d := newDaemon()
	d.pass()
	if !strings.Contains(sb.notified(), "certificate check failed") || !strings.Contains(sb.log(), "trust") {
		t.Fatalf("%q\n%s", sb.notified(), sb.log())
	}
}
