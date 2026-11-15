package mtc

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

const scanCap = 20000

func progressOf2(dst string, size, started int64) M {
	var done int64
	if t := tempOf(dst); t != "" {
		if st, err := os.Stat(t); err == nil {
			done = st.Size()
		}
	} else if st, err := os.Stat(dst); err == nil && st.Size() == size {
		done = size
	}
	out := M{"done": done, "size": size}
	if el := time.Now().Unix() - started; el > 0 && done > 0 {
		rate := float64(done) / float64(el)
		out["rate"] = rate
		out["eta"] = int64(float64(size-done) / rate)
	}
	return out
}

func overview(p M) (M, error) {
	loadConfig()
	out := M{"running": daemonRunning(), "disabled": exists(DisabledFlag), "now": time.Now().Unix()}
	if prob := storageProblem(C.Cache, C.Array); prob != "" {
		out["problem"] = prob
	}
	var st M
	if out["running"] == true && jsonReadInto(StatusFile, &st) {
		if c, ok := st["copying"].(M); ok {
			c["progress"] = progressOf2(str(c["dst"]), toInt64(c["size"]), toInt64(c["started"]))
			delete(c, "src")
		}
		out["status"] = st
	}
	mv := M{"running": moverRunning()}
	var job M
	if jsonReadInto(MoveStatus, &job) {
		if mv["running"] == true && str(job["current_dst"]) != "" {
			job["progress"] = progressOf2(str(job["current_dst"]), toInt64(job["current_size"]), toInt64(job["current_from"]))
		}
		mv["job"] = job
	}
	out["move"] = mv
	t := loadTracked()
	var n, bytes int64
	for p := range t {
		if s, err := os.Stat(p); err == nil {
			n++
			bytes += s.Size()
		}
	}
	out["cached"] = M{"files": n, "bytes": bytes}
	if sp, ok := poolSpace(); ok && sp.Total > 0 {
		out["pool"] = M{"used": sp.Used, "total": sp.Total, "free": sp.Free, "limit": C.S.Int("CACHE_MAX_USAGE")}
	}
	en := []string{}
	for _, d := range servers {
		if C.S.On("ENABLE_" + d.Key) {
			en = append(en, d.Label)
		}
	}
	out["servers_enabled"] = en
	return out, nil
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

func toInt64(v any) int64 {
	f, _ := v.(float64)
	return int64(f)
}

type summary struct {
	Files  int   `json:"files"`
	Size   int64 `json:"size"`
	Capped bool  `json:"capped"`
	Cached int   `json:"cached"`
	Busy   int   `json:"busy"`
}

func summarise(dir string, t Tracked, busy map[string]bool) summary {
	var s summary
	filepath.Walk(dir, func(p string, info os.FileInfo, err error) error {
		if err != nil || s.Capped {
			return filepath.SkipDir
		}
		if strings.HasPrefix(info.Name(), ".") && p != dir {
			if info.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if info.Mode().IsRegular() && C.isMedia(info.Name()) {
			s.Files++
			s.Size += info.Size()
			if _, ok := t[p]; ok {
				s.Cached++
				if busy[p] {
					s.Busy++
				}
			}
			s.Capped = s.Files >= scanCap
		}
		return nil
	})
	return s
}

func safePath(raw string) (string, bool) {
	p := strings.TrimRight(raw, "/")
	if p == "" || strings.ContainsAny(p, "\r\n\x00") {
		return "", false
	}
	real, err := filepath.EvalSymlinks(p)
	if err != nil {
		return "", false
	}
	return real, C.inMapped(real)
}

func streamBusy() map[string]bool {
	var st struct {
		Updated   int64    `json:"updated"`
		Protected []string `json:"protected"`
	}
	out := map[string]bool{}
	if jsonReadInto(StatusFile, &st) && time.Now().Unix()-st.Updated < statusFresh {
		for _, p := range st.Protected {
			out[p] = true
		}
	}
	return out
}

func browse(p M) (M, error) {
	loadConfig()
	t := loadTracked()
	busy := streamBusy()
	roots := C.mappedCacheDirs()
	type entry struct {
		Path    string `json:"path"`
		Name    string `json:"name"`
		Dir     bool   `json:"dir"`
		Size    int64  `json:"size"`
		Tracked bool   `json:"tracked"`
		InUse   bool   `json:"in_use"`
		*summary
	}
	list := []entry{}
	want := pstr(p, "path")
	here, parent := "", ""
	if want == "" {
		for _, r := range roots {
			if !isDir(r) {
				continue
			}
			name, _ := relativeTo(r, C.Cache)
			s := summarise(r, t, busy)
			list = append(list, entry{Path: r, Name: name, Dir: true, Size: s.Size, summary: &s})
		}
	} else {
		var ok bool
		if here, ok = safePath(want); !ok {
			return nil, apiFail("Path is outside the mapped media folders")
		}
		ents, err := os.ReadDir(here)
		if err != nil {
			return nil, apiFail("Cannot read " + here + ": " + err.Error())
		}
		for _, e := range ents {
			if strings.HasPrefix(e.Name(), ".") {
				continue
			}
			full := filepath.Join(here, e.Name())
			info, err := os.Stat(full)
			if err != nil {
				continue
			}
			if info.IsDir() {
				s := summarise(full, t, busy)
				list = append(list, entry{Path: full, Name: e.Name(), Dir: true, Size: s.Size, summary: &s})
			} else if info.Mode().IsRegular() && C.isMedia(e.Name()) {
				_, tr := t[full]
				list = append(list, entry{Path: full, Name: e.Name(), Size: info.Size(), Tracked: tr, InUse: busy[full]})
			}
		}
		isRoot := false
		for _, r := range roots {
			isRoot = isRoot || r == here
		}
		if !isRoot {
			parent = filepath.Dir(here)
		}
	}
	sort.SliceStable(list, func(i, j int) bool {
		if list[i].Dir != list[j].Dir {
			return list[i].Dir
		}
		return strings.ToLower(list[i].Name) < strings.ToLower(list[j].Name)
	})
	return M{"path": here, "parent": parent, "entries": list, "cache_root": C.Cache}, nil
}

func uncache(p M) (M, error) {
	loadConfig()
	raw, _ := p["paths"].([]any)
	var ok []string
	for _, r := range raw {
		s, _ := r.(string)
		if real, in := safePath(s); in {
			ok = append(ok, real)
		}
	}
	if len(ok) == 0 {
		return nil, apiFail("Path is outside the mapped media folders")
	}
	running := moverRunning()
	if err := queueMoves("pick", ok); err != nil {
		return nil, apiFail("Cannot write the move queue: " + err.Error())
	}
	label := filepath.Base(ok[0])
	if len(ok) > 1 {
		label = filepath.Base(ok[0]) + " and " + strconv.Itoa(len(ok)-1) + " more"
	}
	if running {
		return M{"message": "Added to the running move: " + label}, nil
	}
	return M{"message": "Moving " + label + " back to the array"}, nil
}

func selfService(cmd string) string {
	c := exec.Command(SelfBin, "service", cmd)
	var out bytes.Buffer
	c.Stdout, c.Stderr = &out, &out
	c.Run()
	return strings.TrimSpace(out.String())
}

func init() {
	registerAction("overview", overview)
	registerAction("browse", browse)
	registerAction("uncache", uncache)
	registerAction("log", func(p M) (M, error) { return M{"text": tail(LogFile, 300)}, nil })
	registerAction("service", func(p M) (M, error) {
		cmd := pstr(p, "cmd")
		if !inList(cmd, "start", "stop", "restart") {
			return nil, apiFail("Invalid command")
		}
		return M{"message": selfService(cmd), "running": daemonRunning()}, nil
	})
	registerAction("stop_move", func(p M) (M, error) { return M{"message": selfService("stop_move")}, nil })
}
