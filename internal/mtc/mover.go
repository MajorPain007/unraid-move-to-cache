package mtc

import (
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	moveMinAge  = 30 * time.Minute
	statusFresh = 90
)

func queueAppend(lines []string) error {
	os.MkdirAll(TmpDir, 0o755)
	f, err := os.OpenFile(MoveQueue, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	syscall.Flock(int(f.Fd()), syscall.LOCK_EX)
	_, err = f.WriteString(strings.Join(lines, "\n") + "\n")
	return err
}

type moveReq struct{ Kind, Path string }

func queueTake() []moveReq {
	f, err := os.OpenFile(MoveQueue, os.O_RDWR, 0)
	if err != nil {
		return nil
	}
	defer f.Close()
	syscall.Flock(int(f.Fd()), syscall.LOCK_EX)
	b, _ := os.ReadFile(MoveQueue)
	f.Truncate(0)
	var out []moveReq
	for _, line := range strings.Split(string(b), "\n") {
		kind, p, ok := strings.Cut(line, "\t")
		if ok && p != "" && (kind == "pick" || kind == "auto") {
			out = append(out, moveReq{kind, p})
		}
	}
	return out
}

func queuePending() bool {
	st, err := os.Stat(MoveQueue)
	return err == nil && st.Size() > 0
}

func moverRunning() bool { return lockHeld(MoveLock) }

func queueMoves(kind string, paths []string) error {
	if len(paths) == 0 {
		return nil
	}
	lines := make([]string, 0, len(paths))
	for _, p := range paths {
		if !strings.ContainsAny(p, "\n\r") {
			lines = append(lines, kind+"\t"+p)
		}
	}
	if err := queueAppend(lines); err != nil {
		return err
	}
	if !moverRunning() {
		return spawnDetached("move")
	}
	return nil
}

type moveJob struct {
	Started     int64  `json:"started"`
	Total       int    `json:"total"`
	Done        int    `json:"done"`
	Bytes       int64  `json:"bytes"`
	Current     string `json:"current"`
	CurrentDst  string `json:"current_dst"`
	CurrentSize int64  `json:"current_size"`
	CurrentFrom int64  `json:"current_from"`
	Skipped     int    `json:"skipped"`
	Conflicts   int    `json:"conflicts"`
	Recent      int    `json:"recent"`
	Kept        int    `json:"kept"`
	Failed      int    `json:"failed"`
	Dropped     int    `json:"dropped"`
	Stopped     bool   `json:"stopped"`
	Finished    int64  `json:"finished"`
}

type mover struct {
	job      moveJob
	seen     map[string]bool
	work     []moveItem
	untrack  []string
	pollAt   time.Time
	streamed map[string]bool
}

type moveItem struct {
	Path    string
	Tracked bool
}

func (m *mover) save() { fileWrite(MoveStatus, jsonEncode(m.job), 0o644) }

func cacheMediaFiles(roots []string, recent *[]string) map[string]float64 {
	mapped := C.mappedCacheDirs()
	found := map[string]float64{}
	now := time.Now()
	consider := func(p string, st os.FileInfo) {
		name := filepath.Base(p)
		if strings.HasPrefix(name, ".") || !C.isMedia(name) || C.excluded(p) || !st.Mode().IsRegular() {
			return
		}
		if now.Sub(st.ModTime()) < moveMinAge {
			*recent = append(*recent, p)
			return
		}
		found[p] = float64(st.ModTime().Unix())
	}
	for _, r := range roots {
		r = strings.TrimRight(r, "/")
		real, err := filepath.EvalSymlinks(r)
		if err != nil || real != r {
			continue
		}
		inside := false
		for _, m := range mapped {
			inside = inside || under(r, m)
		}
		if !inside {
			continue
		}
		st, err := os.Lstat(r)
		if err != nil {
			continue
		}
		if st.Mode().IsRegular() {
			consider(r, st)
			continue
		}
		filepath.Walk(r, func(p string, info os.FileInfo, err error) error {
			if err != nil {
				return nil
			}
			if info.IsDir() && p != r && strings.HasPrefix(info.Name(), ".") {
				return filepath.SkipDir
			}
			if !info.IsDir() {
				consider(p, info)
			}
			return nil
		})
	}
	return found
}

func (m *mover) plan(reqs []moveReq) {
	t := loadTracked()
	cand := map[string]float64{}
	var picks []string
	for _, r := range reqs {
		if r.Kind == "auto" {
			if ts, ok := t[r.Path]; ok {
				cand[r.Path] = ts
			}
		} else {
			picks = append(picks, r.Path)
		}
	}
	var recent []string
	if len(picks) > 0 {
		for p, ts := range t {
			for _, pk := range picks {
				if under(p, pk) && C.inMapped(p) {
					cand[p] = ts
				}
			}
		}
		for p, ts := range cacheMediaFiles(picks, &recent) {
			if _, ok := cand[p]; !ok {
				cand[p] = ts
			}
		}
	}
	keys := []string{}
	for p := range cand {
		if !m.seen[p] {
			keys = append(keys, p)
		}
	}
	sort.Slice(keys, func(i, j int) bool {
		if cand[keys[i]] != cand[keys[j]] {
			return cand[keys[i]] < cand[keys[j]]
		}
		return keys[i] < keys[j]
	})
	newRecent := 0
	for _, p := range recent {
		if _, ok := t[p]; !ok && !m.seen[p] {
			m.seen[p] = true
			newRecent++
			logInfo("[Move] Leaving %s: written in the last %d minutes", filepath.Base(p), int(moveMinAge.Minutes()))
		}
	}
	for _, p := range keys {
		m.seen[p] = true
		_, tr := t[p]
		m.work = append(m.work, moveItem{p, tr})
	}
	if len(keys) > 0 {
		more := ""
		if m.job.Total > 0 {
			more = "more "
		}
		logInfo("[Move] %d %sfile(s) to move back to the array", len(keys), more)
	}
	m.job.Total += len(keys)
	m.job.Recent += newRecent
	m.save()
}

func (m *mover) takeNew() {
	if !stopped() && queuePending() {
		m.plan(queueTake())
	}
}

func activeCachePaths() map[string]bool {
	out := map[string]bool{}
	for _, d := range servers {
		if !C.S.On("ENABLE_" + d.Key) {
			continue
		}
		list, err := C.conn(d).streams()
		if err != nil {
			logWarn("[Move] Cannot ask %s what is playing: %s", d.Label, err.Msg)
			continue
		}
		for _, st := range list {
			if p, ok := C.streamArrayPath(st.Path); ok {
				out[C.arrayToCache(p)] = true
			}
		}
	}
	return out
}

func (m *mover) protected() map[string]bool {
	if time.Since(m.pollAt) >= time.Duration(max(5, C.S.Int("CHECK_INTERVAL")))*time.Second {
		m.streamed = activeCachePaths()
		m.pollAt = time.Now()
	}
	out := map[string]bool{}
	for p := range m.streamed {
		out[p] = true
	}
	var st struct {
		Updated   int64    `json:"updated"`
		Protected []string `json:"protected"`
	}
	if jsonReadInto(StatusFile, &st) && time.Now().Unix()-st.Updated < statusFresh {
		for _, p := range st.Protected {
			out[p] = true
		}
	}
	return out
}

func (m *mover) batch(reqs []moveReq) {
	m.plan(reqs)
	tick := func() {
		m.takeNew()
		if m.job.CurrentDst != "" {
			m.save()
		}
	}
	for len(m.work) > 0 {
		m.takeNew()
		if stopped() {
			break
		}
		it := m.work[0]
		m.work = m.work[1:]
		name := filepath.Base(it.Path)
		if m.protected()[it.Path] {
			m.job.Skipped++
			logInfo("[Move] Skipping %s: being played, or about to be", name)
			continue
		}
		if !it.Tracked {
			tw, err := twin(it.Path)
			if err != nil {
				m.job.Failed++
				logError("[Move] Skipping %s: %v", name, err)
				continue
			}
			if tw != twinNone && tw != twinSame {
				m.job.Conflicts++
				logWarn("[Move] Skipping %s: a different file of that name is already on the array", name)
				continue
			}
		}
		dst, _ := C.cacheToArray(it.Path)
		var size int64
		if st, err := os.Stat(it.Path); err == nil {
			size = st.Size()
		}
		m.job.Current, m.job.CurrentDst, m.job.CurrentSize, m.job.CurrentFrom = name, dst, size, time.Now().Unix()
		m.save()
		res, n := moveFileToArray(it.Path, tick)
		m.job.Current, m.job.CurrentDst, m.job.CurrentSize = "", "", 0
		if res == resStopped {
			break
		}
		switch res {
		case resMoved, resDropped, resGone:
			m.job.Done++
			m.job.Bytes += n
			if res == resMoved {
				logInfo("[Move] %s (%s)", name, humanBytes(n))
			} else if res == resDropped {
				logInfo("[Move] %s (the array already had it)", name)
			}
		case resKept:
			m.job.Kept++
		default:
			m.job.Failed++
			notifyOnce("move-failed", "Move to the array failed", name+" could not be moved back to the array. The file stays on the cache. See the plugin log.")
		}
		if res != resFailed {
			m.untrack = append(m.untrack, it.Path)
		}
	}
	m.work = nil
	updateTracked(nil, m.untrack)
	m.untrack = nil
	m.save()
}

func (m *mover) finish() {
	j := &m.job
	parts := []string{strconv.Itoa(j.Done) + " of " + strconv.Itoa(j.Total) + " file(s) moved (" + humanBytes(j.Bytes) + ")"}
	add := func(n int, s string) {
		if n > 0 {
			parts = append(parts, strconv.Itoa(n)+" "+s)
		}
	}
	add(j.Skipped, "in use")
	add(j.Conflicts, "with a different file of that name on the array")
	add(j.Recent, "written in the last 30 minutes")
	add(j.Kept, "not matching the copy on the array")
	add(j.Failed, "failed")
	add(j.Dropped, "queued request(s) dropped")
	if j.Stopped || stopped() {
		j.Stopped = true
		logInfo("[Move] Stopped: %s. The rest stays on the cache.", strings.Join(parts, ", "))
	} else if j.Failed > 0 {
		logError("[Move] Done: %s", strings.Join(parts, ", "))
	} else {
		logInfo("[Move] Done: %s", strings.Join(parts, ", "))
	}
	j.Finished = time.Now().Unix()
	m.save()
	if p := readPid(MovePid); p == os.Getpid() {
		os.Remove(MovePid)
	}
}

var moverAfterUnlock = func() {}

func runMover() int {
	logTag = ""
	loadConfig()
	if p := storageProblem(C.Cache, C.Array); p != "" {
		logError("[Move] %s Nothing is moved until the settings are corrected.", p)
		return 1
	}
	lock := lockFile(MoveLock, false)
	if lock == nil {
		return 0
	}
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		<-sig
		stopAll()
	}()
	m := &mover{seen: map[string]bool{}}
	start := func() {
		m.job = moveJob{Started: time.Now().Unix()}
		fileWrite(MovePid, []byte(strconv.Itoa(os.Getpid())+"\n"), 0o644)
		m.save()
	}
	start()
	for {
		reqs := queueTake()
		if len(reqs) > 0 && !stopped() {
			m.batch(reqs)
			continue
		}
		m.job.Dropped += len(reqs)
		m.finish()
		lock.Unlock()
		moverAfterUnlock()
		if !queuePending() {
			return 0
		}
		if stopped() {
			spawnDetached("move")
			return 0
		}
		if lock = lockFile(MoveLock, false); lock == nil {
			return 0
		}
		start()
	}
}

func init() {
	register("move", "move queued files from the cache back to the array, then exit", func(args []string) int {
		return runMover()
	})
}
