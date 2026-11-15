package mtc

import (
	"encoding/json"
	"os"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type Tracked map[string]float64

var trackedCache struct {
	mtime time.Time
	size  int64
	data  Tracked
}

func loadTracked() Tracked {
	st, err := os.Stat(TrackedFile)
	if err != nil {
		return Tracked{}
	}
	if trackedCache.data != nil && st.ModTime().Equal(trackedCache.mtime) && st.Size() == trackedCache.size {
		return trackedCache.data.clone()
	}
	t := Tracked{}
	if !jsonReadInto(TrackedFile, &t) {
		logError("%s cannot be read - treating it as empty and leaving it unchanged", TrackedFile)
		return Tracked{}
	}
	trackedCache.mtime, trackedCache.size, trackedCache.data = st.ModTime(), st.Size(), t
	return t.clone()
}

func (t Tracked) clone() Tracked {
	out := make(Tracked, len(t))
	for k, v := range t {
		out[k] = v
	}
	return out
}

func (t Tracked) sortedByAge() []string {
	keys := make([]string, 0, len(t))
	for k := range t {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool {
		if t[keys[i]] != t[keys[j]] {
			return t[keys[i]] < t[keys[j]]
		}
		return keys[i] < keys[j]
	})
	return keys
}

func saveTracked(t Tracked) error {
	b, _ := json.MarshalIndent(t, "", " ")
	return fileWrite(TrackedFile, append(b, '\n'), 0o600)
}

func updateTracked(add map[string]float64, drop []string) {
	if len(add) == 0 && len(drop) == 0 {
		return
	}
	l := lockFile(TrackedLock, true)
	defer l.Unlock()
	if !exists(TrackedFile) && exists(LegacyList) {
		migrateTrackedLocked()
	}
	t := Tracked{}
	if exists(TrackedFile) && !jsonReadInto(TrackedFile, &t) {
		logError("%s cannot be read - not changing it", TrackedFile)
		return
	}
	changed := false
	for _, p := range drop {
		if _, ok := t[p]; ok {
			delete(t, p)
			changed = true
		}
	}
	for p, ts := range add {
		if _, ok := t[p]; !ok {
			t[p] = ts
			changed = true
		}
	}
	if changed {
		if err := saveTracked(t); err != nil {
			logError("Cannot write %s: %v", TrackedFile, err)
		}
	}
}

func trackAdd(p string) {
	updateTracked(map[string]float64{p: float64(time.Now().Unix())}, nil)
}

func migrateTracked() {
	l := lockFile(TrackedLock, true)
	defer l.Unlock()
	migrateTrackedLocked()
}

func migrateTrackedLocked() {
	if exists(TrackedFile) || !exists(LegacyList) {
		return
	}
	b, err := os.ReadFile(LegacyList)
	if err != nil {
		logError("Cannot read %s: %v", LegacyList, err)
		return
	}
	t := Tracked{}
	now := float64(time.Now().Unix())
	for _, line := range strings.Split(string(b), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		if i := strings.LastIndexByte(line, '|'); i >= 0 {
			if ts, err := strconv.ParseFloat(line[i+1:], 64); err == nil {
				t[line[:i]] = ts
			}
			continue
		}
		t[strings.TrimSpace(line)] = now
	}
	if err := saveTracked(t); err != nil {
		logError("Cannot write %s: %v", TrackedFile, err)
		return
	}
	os.Remove(LegacyList)
	logInfo("Took over %d cached file(s) from the previous version", len(t))
}

func storageReady() bool {
	f, err := os.Open(C.Cache)
	if err != nil {
		return false
	}
	names, _ := f.Readdirnames(1)
	f.Close()
	return len(names) > 0 && isDir(C.physicalArray())
}

type space struct{ Used, Total, Free int64 }

var zfsWarned = map[string]bool{}

var poolSpace = realPoolSpace

func realPoolSpace() (space, bool) {
	var st syscall.Statfs_t
	if syscall.Statfs(C.Cache, &st) != nil {
		return space{}, false
	}
	bs := int64(st.Bsize)
	sp := space{Total: int64(st.Blocks) * bs, Free: int64(st.Bavail) * bs}
	sp.Used = sp.Total - int64(st.Bfree)*bs
	if m, ok := mountOf(C.Cache, readMounts()); ok && m.Type == "zfs" {
		pool := strings.SplitN(m.Source, "/", 2)[0]
		r := runCmd(15*time.Second, ZfsBin, "list", "-Hp", "-o", "used,avail", pool)
		f := strings.Fields(r.Stdout)
		if r.Code == 0 && len(f) == 2 {
			used, e1 := strconv.ParseInt(f[0], 10, 64)
			avail, e2 := strconv.ParseInt(f[1], 10, 64)
			if e1 == nil && e2 == nil {
				return space{used, used + avail, avail}, true
			}
		}
		if !zfsWarned[pool] {
			zfsWarned[pool] = true
			logWarn("Cannot read the size of ZFS pool %s (%s) - the usage limit is judged from %s alone", pool, r.Message(), C.Cache)
		}
	}
	return sp, true
}

func nearestDir(p string) string {
	for d := p; ; {
		parent := d[:max(strings.LastIndexByte(d, '/'), 0)]
		if parent == "" {
			return "/"
		}
		if isDir(parent) {
			return parent
		}
		d = parent
	}
}

const roomMargin = 512 << 20

func hasRoom(sp space, size int64, dest string, freed int64) bool {
	used := max(sp.Used-freed, 0)
	free := sp.Free + freed
	if sp.Total > 0 && float64(used)/float64(sp.Total)*100 >= float64(C.S.Int("CACHE_MAX_USAGE")) {
		return false
	}
	if dest != "" {
		var st syscall.Statfs_t
		if syscall.Statfs(nearestDir(dest), &st) == nil {
			free = min(free, int64(st.Bavail)*int64(st.Bsize)+freed)
		}
	}
	return free > size+roomMargin
}
