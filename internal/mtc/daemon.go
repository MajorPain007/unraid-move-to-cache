package mtc

import (
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

var (
	fsReadDir = os.ReadDir
	fsStat    = os.Stat
	nowFn     = time.Now
)

const (
	copyFailCooldown = 300 * time.Second
	moveRetry        = 600 * time.Second
	evictMaxFiles    = 25
)

type copyInfo struct {
	Src     string `json:"src"`
	Dst     string `json:"dst"`
	Name    string `json:"name"`
	Size    int64  `json:"size"`
	Started int64  `json:"started"`
}

type serverState struct {
	OK        bool   `json:"ok"`
	Kind      string `json:"kind"`
	Message   string `json:"message"`
	FailSince int64  `json:"fail_since"`
	last      []stream
}

type daemon struct {
	mu        sync.Mutex
	pending   map[string]bool
	order     []string
	wake      chan struct{}
	current   *copyInfo
	failed    map[string]time.Time
	requested map[string]time.Time
	active    map[string]bool

	storage    *bool
	notReady   time.Time
	timers     map[string]time.Time
	handled    map[string]bool
	lastStream map[string]stream
	deletion   map[string]time.Time
	servers    map[string]*serverState
	lastDays   time.Time
	prefetched map[string]bool
}

func newDaemon() *daemon {
	return &daemon{pending: map[string]bool{}, wake: make(chan struct{}, 1), failed: map[string]time.Time{},
		requested: map[string]time.Time{}, active: map[string]bool{}, timers: map[string]time.Time{},
		handled: map[string]bool{}, lastStream: map[string]stream{}, deletion: map[string]time.Time{},
		servers: map[string]*serverState{}, prefetched: map[string]bool{}}
}

func (d *daemon) enqueue(arrayPath string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.pending[arrayPath] {
		return
	}
	d.pending[arrayPath] = true
	d.order = append(d.order, arrayPath)
	select {
	case d.wake <- struct{}{}:
	default:
	}
}

func (d *daemon) next() (string, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if len(d.order) == 0 {
		return "", false
	}
	p := d.order[0]
	d.order = d.order[1:]
	return p, true
}

func (d *daemon) pendingCache() map[string]bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := map[string]bool{}
	for p := range d.pending {
		out[C.arrayToCache(p)] = true
	}
	return out
}

func (d *daemon) worker() {
	for !stopped() {
		p, ok := d.next()
		if !ok {
			select {
			case <-d.wake:
			case <-stopCtx.Done():
				return
			case <-time.After(time.Second):
			}
			continue
		}
		d.copyFile(p)
		d.mu.Lock()
		delete(d.pending, p)
		d.current = nil
		d.mu.Unlock()
	}
}

func (d *daemon) copyFile(arrayPath string) {
	if C.excluded(arrayPath) || !C.isMedia(filepath.Base(arrayPath)) || !storageReady() {
		return
	}
	cachePath := C.arrayToCache(arrayPath)
	if exists(cachePath) {
		if tw, err := twin(cachePath); err == nil && tw == twinSame {
			d.mu.Lock()
			delete(d.deletion, cachePath)
			d.mu.Unlock()
			if _, ok := loadTracked()[cachePath]; !ok {
				trackAdd(cachePath)
			}
		}
		return
	}
	d.mu.Lock()
	last := d.failed[cachePath]
	d.mu.Unlock()
	if nowFn().Sub(last) < copyFailCooldown {
		return
	}
	st, err := fsStat(arrayPath)
	if err != nil {
		return
	}
	sp, ok := poolSpace()
	if !ok {
		logError("Cannot read the size of the cache pool at %s", C.Cache)
		return
	}
	if !hasRoom(sp, st.Size(), cachePath, 0) && !d.evict(st.Size(), cachePath, sp) {
		logWarn("[Copy] No room on the cache for %s", filepath.Base(arrayPath))
		return
	}
	notifyResolved("cache-full")
	d.mu.Lock()
	d.current = &copyInfo{arrayPath, cachePath, filepath.Base(arrayPath), st.Size(), nowFn().Unix()}
	d.mu.Unlock()
	logInfo("[Copy] -> %s", filepath.Base(arrayPath))
	if err := copyToCache(arrayPath); err != nil {
		if err == errStopped {
			logInfo("[Copy] Stopped: %s", filepath.Base(arrayPath))
			return
		}
		logError("Copy failed for %s: %v", filepath.Base(arrayPath), err)
		d.mu.Lock()
		d.failed[cachePath] = nowFn()
		d.mu.Unlock()
		return
	}
	trackAdd(cachePath)
	d.mu.Lock()
	delete(d.failed, cachePath)
	d.mu.Unlock()
}

func (d *daemon) evict(need int64, dest string, sp space) bool {
	if !C.S.On("ENABLE_CACHE_EVICTION") || !storageReady() {
		return false
	}
	protected := d.pendingCache()
	d.mu.Lock()
	for p := range d.active {
		protected[p] = true
	}
	for p := range d.requested {
		protected[p] = true
	}
	d.mu.Unlock()
	t := loadTracked()
	var freed int64
	evicted := 0
	var untrack []string
	defer func() { updateTracked(nil, untrack) }()
	for _, p := range t.sortedByAge() {
		if hasRoom(sp, need, dest, freed) {
			return true
		}
		if evicted >= evictMaxFiles || stopped() {
			break
		}
		if protected[p] {
			continue
		}
		if !exists(p) {
			untrack = append(untrack, p)
			continue
		}
		logInfo("[Evict] %s (making room on cache)", filepath.Base(p))
		res, n := moveFileToArray(p, nil)
		if res == resStopped {
			return false
		}
		if res != resFailed {
			untrack = append(untrack, p)
		}
		if res == resMoved || res == resDropped {
			freed += n
			evicted++
		}
	}
	if hasRoom(sp, need, dest, freed) {
		return true
	}
	notifyOnce("cache-full", "Cache is full", "The cache pool has no room for new copies and nothing the plugin cached can be moved away. Check CACHE_MAX_USAGE or free some space.")
	return false
}

func (d *daemon) requestMoves(paths []string, label string) {
	now := nowFn()
	pend := d.pendingCache()
	d.mu.Lock()
	for p, when := range d.requested {
		if now.Sub(when) >= moveRetry {
			delete(d.requested, p)
		}
	}
	var fresh []string
	seen := map[string]bool{}
	for _, p := range paths {
		if seen[p] || d.requested[p] != (time.Time{}) || d.active[p] || pend[p] {
			continue
		}
		seen[p] = true
		d.requested[p] = now
		fresh = append(fresh, p)
	}
	d.mu.Unlock()
	for _, p := range fresh {
		logInfo("[%s] %s", label, filepath.Base(p))
	}
	if err := queueMoves("auto", fresh); err != nil {
		logError("Cannot hand files to the mover: %v", err)
	}
}

func seasonEpisodes(dir string) (map[int][]string, bool) {
	entries, err := fsReadDir(dir)
	if err != nil {
		return nil, false
	}
	out := map[int][]string{}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".") {
			continue
		}
		if ep, ok := parseEpisode(e.Name()); ok {
			out[ep] = append(out[ep], e.Name())
		}
	}
	return out, true
}

func sortedEps(m map[int][]string) []int {
	out := make([]int, 0, len(m))
	for e := range m {
		out = append(out, e)
	}
	sort.Ints(out)
	return out
}

func selectBatch(all []int, current, size, tol, prefetch int) []int {
	size, tol, prefetch = max(size, 1), max(tol, 0), max(prefetch, 0)
	var upcoming []int
	for _, e := range all {
		if e >= current {
			upcoming = append(upcoming, e)
		}
	}
	if len(all) <= size+tol {
		return upcoming
	}
	var batches [][]int
	for i := 0; i < len(all); i += size {
		batches = append(batches, append([]int{}, all[i:min(i+size, len(all))]...))
	}
	if n := len(batches); n >= 2 && len(batches[n-1]) <= tol {
		batches[n-2] = append(batches[n-2], batches[n-1]...)
		batches = batches[:n-1]
	}
	idx := len(batches) - 1
	for i, b := range batches {
		if current <= b[len(b)-1] {
			idx = i
			break
		}
	}
	var sel []int
	remaining := 0
	for _, e := range batches[idx] {
		if e >= current {
			sel = append(sel, e)
		}
		if e > current {
			remaining++
		}
	}
	if remaining <= prefetch && idx+1 < len(batches) {
		sel = append(sel, batches[idx+1]...)
	}
	return sel
}

func (d *daemon) batchOf(all []int, current int) []int {
	s := C.S
	if !s.On("ENABLE_EPISODE_BATCHING") {
		var out []int
		for _, e := range all {
			if e >= current {
				out = append(out, e)
			}
		}
		return out
	}
	return selectBatch(all, current, s.Int("EPISODE_BATCH_SIZE"), s.Int("EPISODE_BATCH_TOLERANCE"), s.Int("EPISODE_BATCH_PREFETCH"))
}

func (d *daemon) handleMovie(arrayPath string) {
	d.enqueue(arrayPath)
	dir, base := filepath.Dir(arrayPath), filepath.Base(arrayPath)
	stem := strings.TrimSuffix(base, filepath.Ext(base)) + "."
	entries, err := fsReadDir(dir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if e.Name() != base && strings.HasPrefix(e.Name(), stem) && C.isMedia(e.Name()) {
			d.enqueue(filepath.Join(dir, e.Name()))
		}
	}
}

func (d *daemon) handleSeries(arrayPath string, ep int) {
	season := filepath.Dir(arrayPath)
	if C.S["CLEANUP_MODE"] == "smart" {
		cacheSeason := C.arrayToCache(season)
		threshold := ep - C.S.Int("EPISODE_KEEP_PREVIOUS")
		t := loadTracked()
		var old []string
		if entries, err := os.ReadDir(cacheSeason); err == nil {
			for _, e := range entries {
				p := filepath.Join(cacheSeason, e.Name())
				if n, ok := parseEpisode(e.Name()); ok && n < threshold {
					if _, tr := t[p]; tr {
						old = append(old, p)
					}
				}
			}
		}
		d.requestMoves(old, "Smart Cleanup")
	}
	eps, ok := seasonEpisodes(season)
	if !ok || len(eps) == 0 {
		return
	}
	all := sortedEps(eps)
	for _, e := range d.batchOf(all, ep) {
		for _, f := range eps[e] {
			d.enqueue(filepath.Join(season, f))
		}
	}
	if C.S.On("ENABLE_NEXT_SEASON_PREFETCH") {
		remaining := 0
		for _, e := range all {
			if e > ep {
				remaining++
			}
		}
		if remaining <= C.S.Int("EPISODE_BATCH_PREFETCH") {
			d.prefetchNext(season)
		}
	}
}

func (d *daemon) prefetchNext(season string) {
	n, ok := seasonNumber(filepath.Base(season))
	if !ok {
		return
	}
	show := filepath.Dir(season)
	entries, err := fsReadDir(show)
	if err != nil {
		return
	}
	for _, e := range entries {
		m, ok := seasonNumber(e.Name())
		next := filepath.Join(show, e.Name())
		if !ok || m != n+1 || !e.IsDir() || C.excluded(next) {
			continue
		}
		eps, ok := seasonEpisodes(next)
		if !ok || len(eps) == 0 {
			return
		}
		all := sortedEps(eps)
		want := d.batchOf(all, all[0])
		if !d.prefetched[next] {
			d.prefetched[next] = true
			logInfo("[Prefetch] Next season: %s/%s (%d episodes)", filepath.Base(show), e.Name(), len(want))
		}
		for _, x := range want {
			for _, f := range eps[x] {
				d.enqueue(filepath.Join(next, f))
			}
		}
		return
	}
}

func (d *daemon) pollServers() (map[string]stream, bool) {
	all := map[string]stream{}
	complete := true
	minutes := int64(C.S.Int("NOTIFY_OFFLINE_MINUTES")) * 60
	for _, def := range servers {
		if !C.S.On("ENABLE_" + def.Key) {
			delete(d.servers, def.Key)
			continue
		}
		st := d.servers[def.Key]
		if st == nil {
			st = &serverState{OK: true}
			d.servers[def.Key] = st
		}
		list, err := C.conn(def).streams()
		now := nowFn().Unix()
		if err != nil {
			complete = false
			if st.OK || st.Kind != err.Kind {
				logWarn("%s does not answer (%s) - nothing played on it is cached until it does. %s", def.Label, err.Msg, advice[err.Kind])
			}
			if st.OK {
				st.FailSince = now
			}
			st.OK, st.Kind, st.Message = false, err.Kind, err.Msg
			if err.Kind == "certificate" {
				notifyOnce("cert:"+def.Key, def.Label+": certificate check failed", err.Msg+". Open Move to Cache, Settings, Test connection and trust the certificate, or switch off Verify certificate.")
			} else if now-st.FailSince >= minutes {
				notifyOnce("offline:"+def.Key, def.Label+" is not reachable", def.Label+" has not answered for "+strconv.FormatInt((now-st.FailSince)/60, 10)+" minutes: "+err.Msg)
			}
			list = st.last
		} else {
			if !st.OK {
				logInfo("%s is answering again", def.Label)
			}
			st.OK, st.Kind, st.Message, st.FailSince, st.last = true, "", "", 0, list
			notifyResolved("cert:" + def.Key)
			notifyResolved("offline:" + def.Key)
		}
		for _, s := range list {
			all[s.Path] = s
		}
	}
	return all, complete
}

func (d *daemon) pass() {
	ready := storageReady()
	if d.storage == nil || *d.storage != ready {
		d.storage = &ready
		if ready {
			reconcile()
			notifyResolved("pool")
		} else {
			logInfo("Waiting for the cache pool and the array to be mounted")
			d.notReady = nowFn()
		}
	}
	if !ready {
		if nowFn().Sub(d.notReady) > 10*time.Minute {
			notifyOnce("pool", "Cache pool missing", C.Cache+" or "+C.physicalArray()+" has not been available for 10 minutes. Nothing is cached until it is.")
		}
		d.mu.Lock()
		d.active = map[string]bool{}
		d.mu.Unlock()
		return
	}
	streams, complete := d.pollServers()
	activePaths := map[string]bool{}
	for dp := range streams {
		if p, ok := C.streamArrayPath(dp); ok {
			activePaths[p] = true
		}
	}
	active := map[string]bool{}
	for p := range activePaths {
		active[C.arrayToCache(p)] = true
	}
	d.mu.Lock()
	d.active = active
	retry := len(d.failed) > 0
	d.mu.Unlock()
	now := nowFn()
	keys := make([]string, 0, len(activePaths))
	for p := range activePaths {
		keys = append(keys, p)
	}
	sort.Strings(keys)
	for _, p := range keys {
		started, ok := d.timers[p]
		if !ok {
			logInfo("[Stream] Active: %s", filepath.Base(p))
			d.timers[p] = now
			continue
		}
		if now.Sub(started) < time.Duration(C.S.Int("COPY_DELAY"))*time.Second || (d.handled[p] && !retry) {
			continue
		}
		d.handled[p] = true
		if ep, ok := parseEpisode(filepath.Base(p)); ok {
			d.handleSeries(p, ep)
		} else {
			d.handleMovie(p)
		}
	}
	for p := range d.timers {
		if !activePaths[p] {
			delete(d.timers, p)
			delete(d.handled, p)
		}
	}
	if complete {
		switch C.S["CLEANUP_MODE"] {
		case "smart":
			d.smartCleanup(streams)
		case "days":
			if now.Sub(d.lastDays) > time.Hour {
				maxAge := float64(C.S.Int("CACHE_MAX_DAYS") * 86400)
				var old []string
				for p, ts := range loadTracked() {
					if float64(now.Unix())-ts > maxAge && exists(p) {
						old = append(old, p)
					}
				}
				sort.Strings(old)
				d.requestMoves(old, "Days Cleanup")
				d.lastDays = now
			}
		}
		d.lastStream = streams
	} else {
		for k, v := range streams {
			d.lastStream[k] = v
		}
	}
}

func (d *daemon) smartCleanup(streams map[string]stream) {
	var t Tracked
	for dp, st := range d.lastStream {
		if _, still := streams[dp]; still {
			continue
		}
		arrayPath, ok := C.streamArrayPath(dp)
		if !ok || !watched(st) {
			continue
		}
		cachePath := C.arrayToCache(arrayPath)
		if !exists(cachePath) {
			continue
		}
		if t == nil {
			t = loadTracked()
		}
		ep, isEp := parseEpisode(filepath.Base(arrayPath))
		if !isEp {
			if _, ok := t[cachePath]; ok {
				d.deletion[cachePath] = nowFn()
			}
			continue
		}
		eps, ok := seasonEpisodes(filepath.Dir(arrayPath))
		if !ok {
			logWarn("Cannot read %s - not treating episode %d as the season finale", filepath.Dir(arrayPath), ep)
			continue
		}
		last := -1
		for e := range eps {
			last = max(last, e)
		}
		if last >= 0 && ep >= last {
			if entries, err := os.ReadDir(filepath.Dir(cachePath)); err == nil {
				for _, e := range entries {
					p := filepath.Join(filepath.Dir(cachePath), e.Name())
					if _, ok := t[p]; ok {
						d.deletion[p] = nowFn()
					}
				}
			}
		}
	}
	delay := time.Duration(C.S.Int("MOVIE_DELETE_DELAY")) * time.Second
	var due []string
	for p, at := range d.deletion {
		if nowFn().Sub(at) > delay {
			delete(d.deletion, p)
			if exists(p) {
				due = append(due, p)
			}
		}
	}
	sort.Strings(due)
	d.requestMoves(due, "Cleanup")
}

type statusStream struct {
	Title    string   `json:"title"`
	Service  string   `json:"service"`
	User     string   `json:"user"`
	Progress *float64 `json:"progress"`
	Host     string   `json:"host"`
	OnCache  bool     `json:"on_cache"`
	Skipped  string   `json:"skipped,omitempty"`
}

func (d *daemon) writeStatus() {
	d.mu.Lock()
	protected := map[string]bool{}
	for p := range d.active {
		protected[p] = true
	}
	for p := range d.pending {
		protected[C.arrayToCache(p)] = true
	}
	queue := append([]string{}, d.order...)
	cur := d.current
	d.mu.Unlock()
	streams := []statusStream{}
	for dp, st := range d.lastStream {
		row := statusStream{Title: st.Title, Service: st.Service, User: st.User, Progress: st.Progress}
		if p, ok := C.streamArrayPath(dp); ok {
			row.Host = p
			row.OnCache = exists(C.arrayToCache(p))
		} else {
			row.Host, _ = C.translate(dp)
			row.Skipped = "not under the Array Root, excluded, not a media file or no mapping"
		}
		streams = append(streams, row)
	}
	sort.Slice(streams, func(i, j int) bool { return streams[i].Title < streams[j].Title })
	prot := make([]string, 0, len(protected))
	for p := range protected {
		prot = append(prot, p)
	}
	sort.Strings(prot)
	names := make([]string, 0, len(queue))
	for _, q := range queue {
		names = append(names, filepath.Base(q))
	}
	srv := M{}
	for k, v := range d.servers {
		srv[k] = v
	}
	st := M{"updated": nowFn().Unix(), "storage_ready": d.storage != nil && *d.storage, "streams": streams,
		"copying": cur, "queue": names, "protected": prot, "servers": srv}
	fileWrite(StatusFile, jsonEncode(st), 0o644)
}

func runDaemon() int {
	logTag = ""
	loadConfig()
	if p := storageProblem(C.Cache, C.Array); p != "" {
		logError("%s The service does not start until the settings are corrected.", p)
		notifyOnce("roots", "Move to Cache did not start", p)
		return 1
	}
	lock := lockFile(DaemonLock, false)
	if lock == nil {
		logError("Another instance is already running")
		return 1
	}
	defer lock.Unlock()
	fileWrite(DaemonPid, []byte(strconv.Itoa(os.Getpid())+"\n"), 0o644)
	migrateTracked()
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	d := newDaemon()
	go d.worker()
	logInfo("Service started. Waiting for streams...")
	if C.S.On("ENABLE_EPISODE_BATCHING") {
		logInfo("Season batching enabled: batch=%d, tolerance=%d, prefetch=%d", C.S.Int("EPISODE_BATCH_SIZE"), C.S.Int("EPISODE_BATCH_TOLERANCE"), C.S.Int("EPISODE_BATCH_PREFETCH"))
	}
	for {
		d.pass()
		d.writeStatus()
		select {
		case <-sig:
			stopAll()
			for i := 0; i < 300; i++ {
				d.mu.Lock()
				busy := d.current != nil
				d.mu.Unlock()
				if !busy {
					break
				}
				time.Sleep(100 * time.Millisecond)
			}
			os.Remove(StatusFile)
			if readPid(DaemonPid) == os.Getpid() {
				os.Remove(DaemonPid)
			}
			logInfo("Service stopped.")
			return 0
		case <-time.After(time.Duration(C.S.Int("CHECK_INTERVAL")) * time.Second):
		}
	}
}

func init() {
	register("daemon", "run the service (started by \"service start\")", func(args []string) int { return runDaemon() })
}
