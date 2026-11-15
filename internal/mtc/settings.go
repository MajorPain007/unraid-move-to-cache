package mtc

import (
	"fmt"
	"net/url"
	"os"
	"path"
	"regexp"
	"strconv"
	"strings"
)

type servDef struct {
	Key, Label, TokenKey string
}

var servers = []servDef{
	{"PLEX", "Plex", "PLEX_TOKEN"},
	{"EMBY", "Emby", "EMBY_API_KEY"},
	{"JELLYFIN", "Jellyfin", "JELLYFIN_API_KEY"},
}

var settingKeys = []string{
	"ENABLE_PLEX", "PLEX_URL", "PLEX_TOKEN", "PLEX_VERIFY_TLS", "PLEX_CERT_SHA256",
	"ENABLE_EMBY", "EMBY_URL", "EMBY_API_KEY", "EMBY_VERIFY_TLS", "EMBY_CERT_SHA256",
	"ENABLE_JELLYFIN", "JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_VERIFY_TLS", "JELLYFIN_CERT_SHA256",
	"CHECK_INTERVAL", "CACHE_MAX_USAGE", "COPY_DELAY",
	"CLEANUP_MODE", "MOVIE_DELETE_DELAY", "EPISODE_KEEP_PREVIOUS", "CACHE_MAX_DAYS",
	"EXCLUDE_DIRS", "MEDIA_FILETYPES", "ARRAY_ROOT", "CACHE_ROOT", "DOCKER_MAPPINGS",
	"ENABLE_EPISODE_BATCHING", "EPISODE_BATCH_SIZE", "EPISODE_BATCH_TOLERANCE", "EPISODE_BATCH_PREFETCH",
	"ENABLE_CACHE_EVICTION", "ENABLE_NEXT_SEASON_PREFETCH", "NOTIFY_OFFLINE_MINUTES",
}

var settingDefaults = map[string]string{
	"ENABLE_PLEX": "no", "PLEX_URL": "http://localhost:32400", "PLEX_VERIFY_TLS": "yes",
	"ENABLE_EMBY": "no", "EMBY_URL": "http://localhost:8096", "EMBY_VERIFY_TLS": "yes",
	"ENABLE_JELLYFIN": "no", "JELLYFIN_URL": "http://localhost:8096", "JELLYFIN_VERIFY_TLS": "yes",
	"CHECK_INTERVAL": "10", "CACHE_MAX_USAGE": "80", "COPY_DELAY": "30",
	"CLEANUP_MODE": "none", "MOVIE_DELETE_DELAY": "1800", "EPISODE_KEEP_PREVIOUS": "2",
	"CACHE_MAX_DAYS": "7", "MEDIA_FILETYPES": ".mkv .mp4 .avi",
	"ARRAY_ROOT": "/mnt/user", "CACHE_ROOT": "/mnt/cache",
	"ENABLE_EPISODE_BATCHING": "no", "EPISODE_BATCH_SIZE": "30", "EPISODE_BATCH_TOLERANCE": "10",
	"EPISODE_BATCH_PREFETCH": "4", "ENABLE_CACHE_EVICTION": "yes", "ENABLE_NEXT_SEASON_PREFETCH": "no",
	"NOTIFY_OFFLINE_MINUTES": "10",
}

var numberRanges = map[string][2]int{
	"CHECK_INTERVAL": {2, 3600}, "CACHE_MAX_USAGE": {1, 99}, "COPY_DELAY": {0, 86400},
	"MOVIE_DELETE_DELAY": {0, 604800}, "EPISODE_KEEP_PREVIOUS": {0, 100}, "CACHE_MAX_DAYS": {1, 3650},
	"EPISODE_BATCH_SIZE": {1, 1000}, "EPISODE_BATCH_TOLERANCE": {0, 1000}, "EPISODE_BATCH_PREFETCH": {0, 1000},
	"NOTIFY_OFFLINE_MINUTES": {1, 1440},
}

var fieldLabels = map[string]string{
	"CHECK_INTERVAL": "Check every", "CACHE_MAX_USAGE": "Pool limit", "COPY_DELAY": "Copy after",
	"MOVIE_DELETE_DELAY": "Movies after", "EPISODE_KEEP_PREVIOUS": "Keep previous episodes", "CACHE_MAX_DAYS": "Days",
	"EPISODE_BATCH_SIZE": "Batch", "EPISODE_BATCH_TOLERANCE": "Tolerance", "EPISODE_BATCH_PREFETCH": "Ahead",
	"NOTIFY_OFFLINE_MINUTES": "Notify after", "EXCLUDE_DIRS": "Skip folders", "MEDIA_FILETYPES": "File types",
}

func label(k string) string {
	if l, ok := fieldLabels[k]; ok {
		return l
	}
	return k
}

type Settings map[string]string

func isSwitch(k string) bool {
	return strings.HasPrefix(k, "ENABLE_") || strings.HasSuffix(k, "_VERIFY_TLS")
}

func truthy(v string) bool {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "yes", "true", "1", "on":
		return true
	}
	return false
}

var reKey = regexp.MustCompile(`^[A-Z0-9_]+$`)

func parseSettings(data string) Settings {
	s := Settings{}
	for _, line := range strings.Split(data, "\n") {
		i := strings.IndexByte(line, '=')
		if i <= 0 {
			continue
		}
		k := strings.TrimSpace(line[:i])
		v := strings.TrimSpace(strings.TrimRight(line[i+1:], "\r"))
		if len(v) >= 2 && v[0] == '"' && v[len(v)-1] == '"' {
			v = v[1 : len(v)-1]
		}
		if reKey.MatchString(k) {
			s[k] = v
		}
	}
	return s.normalized()
}

func (s Settings) normalized() Settings {
	out := Settings{}
	for _, k := range settingKeys {
		v, ok := s[k]
		if !ok {
			v = settingDefaults[k]
		}
		if isSwitch(k) {
			v = map[bool]string{true: "yes", false: "no"}[truthy(v)]
		}
		out[k] = v
	}
	return out
}

func loadSettings() Settings {
	b, _ := os.ReadFile(SettingsCfg)
	return parseSettings(string(b))
}

func (s Settings) Render() string {
	var b strings.Builder
	for _, k := range settingKeys {
		b.WriteString(k + "=\"" + s[k] + "\"\n")
	}
	return b.String()
}

func saveSettings(s Settings) error {
	return fileWrite(SettingsCfg, []byte(s.Render()), 0o600)
}

func (s Settings) On(k string) bool { return s[k] == "yes" }

func (s Settings) Int(k string) int {
	n, err := strconv.Atoi(s[k])
	r, ok := numberRanges[k]
	if err != nil && ok {
		n, _ = strconv.Atoi(settingDefaults[k])
	}
	if ok {
		n = min(max(n, r[0]), r[1])
	}
	return n
}

func cleanPath(p string) string {
	p = strings.TrimSpace(p)
	if p == "" {
		return ""
	}
	return path.Clean(p)
}

var reDisk = regexp.MustCompile(`^disk\d+$`)

func storageProblem(cacheRoot, arrayRoot string) string {
	cache, array := cleanPath(cacheRoot), cleanPath(arrayRoot)
	for _, x := range [][2]string{{"Cache Root", cache}, {"Array Root", array}} {
		if !strings.HasPrefix(x[1], "/") || x[1] == "/" {
			return x[0] + " must be a full path below /, such as /mnt/cache or /mnt/user."
		}
	}
	if under(cache, array) || under(array, cache) {
		return fmt.Sprintf("Cache Root %s and Array Root %s overlap - they must be two different places.", cache, array)
	}
	if onArray(cache) {
		return fmt.Sprintf("Cache Root %s is on the array - choose the cache pool itself, such as %s/cache.", cache, MntDir)
	}
	return ""
}

func onArray(p string) bool {
	rel, ok := relativeTo(p, MntDir)
	if !ok || rel == "" {
		return false
	}
	first := strings.SplitN(rel, "/", 2)[0]
	return first == "user" || first == "user0" || reDisk.MatchString(first)
}

var poolSkip = map[string]bool{"user": true, "user0": true, "disks": true, "remotes": true, "rootshare": true, "addons": true}

func listPools(arrayRoot string) []string {
	out := []string{}
	seen := map[string]bool{}
	for _, m := range readMounts() {
		rel, ok := relativeTo(m.Dir, MntDir)
		if !ok || rel == "" {
			continue
		}
		parts := strings.Split(rel, "/")
		good := len(parts) == 1 && !poolSkip[parts[0]] && !reDisk.MatchString(parts[0])
		good = good || (len(parts) == 2 && parts[0] == "disks")
		if good && m.Dir != cleanPath(arrayRoot) && !seen[m.Dir] {
			seen[m.Dir] = true
			out = append(out, m.Dir)
		}
	}
	return out
}

func validURL(v string) bool {
	u, err := url.Parse(v)
	return err == nil && (u.Scheme == "http" || u.Scheme == "https") && u.Host != ""
}

var reMapping = regexp.MustCompile(`^([^:;]+):([^;]+)$`)

func parseMappings(v string) ([][2]string, error) {
	out := [][2]string{}
	for _, pair := range strings.Split(v, ";") {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		m := reMapping.FindStringSubmatch(pair)
		if m == nil {
			return nil, fmt.Errorf("Docker mapping %q must look like /container/path:/host/path", pair)
		}
		d, h := cleanPath(m[1]), cleanPath(m[2])
		if !strings.HasPrefix(d, "/") || d == "/" {
			return nil, fmt.Errorf("Docker mapping %q: the container path must be a full path such as /media", pair)
		}
		out = append(out, [2]string{d, h})
	}
	return out, nil
}

type fieldError struct{ Field, Msg string }

func (e fieldError) Error() string { return e.Msg }

func validateSettings(s Settings) error {
	for _, k := range settingKeys {
		if strings.ContainsAny(s[k], "\"\r\n\x00") {
			return fieldError{k, label(k) + " must not contain quotes or line breaks."}
		}
	}
	for k, r := range numberRanges {
		n, err := strconv.Atoi(s[k])
		if err != nil || n < r[0] || n > r[1] {
			return fieldError{k, fmt.Sprintf("%s: enter a whole number from %d to %d.", label(k), r[0], r[1])}
		}
	}
	for _, sv := range servers {
		if !validURL(s[sv.Key+"_URL"]) && (s.On("ENABLE_"+sv.Key) || s[sv.Key+"_URL"] != "") {
			return fieldError{sv.Key + "_URL", sv.Label + " URL must start with http:// or https://, such as http://192.168.1.10:32400."}
		}
		if p := s[sv.Key+"_CERT_SHA256"]; p != "" && !reSHA.MatchString(p) {
			return fieldError{sv.Key + "_CERT_SHA256", sv.Label + " certificate fingerprint is not a SHA-256 value."}
		}
	}
	if !inList(s["CLEANUP_MODE"], "none", "smart", "days") {
		return fieldError{"CLEANUP_MODE", "Cleanup mode must be none, smart or days."}
	}
	if _, err := parseMappings(s["DOCKER_MAPPINGS"]); err != nil {
		return fieldError{"DOCKER_MAPPINGS", err.Error()}
	}
	if p := storageProblem(s["CACHE_ROOT"], s["ARRAY_ROOT"]); p != "" {
		return fieldError{"CACHE_ROOT", p}
	}
	return nil
}

var reSHA = regexp.MustCompile(`^[0-9a-f]{64}$`)

func inList(v string, l ...string) bool {
	for _, x := range l {
		if x == v {
			return true
		}
	}
	return false
}

func cacheRootState(s Settings) M {
	pools := listPools(s["ARRAY_ROOT"])
	cur := cleanPath(s["CACHE_ROOT"])
	problem := storageProblem(cur, s["ARRAY_ROOT"])
	listed := inList(cur, pools...)
	if problem == "" && !listed {
		if len(pools) == 0 {
			problem = ""
		} else {
			problem = cur + " is not a mounted pool. Choose one from the list."
		}
	}
	arrays := []string{MntDir + "/user"}
	for _, p := range listPools("") {
		if p != cur {
			arrays = append(arrays, p)
		}
	}
	return M{"pools": pools, "current": cur, "listed": listed, "problem": problem, "arrays": arrays}
}

func init() {
	registerAction("settings_get", func(p M) (M, error) {
		s := loadSettings()
		out := M{}
		stored := M{}
		for _, k := range settingKeys {
			if isToken(k) {
				stored[k] = s[k] != ""
				continue
			}
			out[k] = s[k]
		}
		return M{"settings": out, "stored": stored, "cache_root": cacheRootState(s)}, nil
	})
	registerAction("pools", func(p M) (M, error) {
		return cacheRootState(loadSettings()), nil
	})
	registerAction("settings_save", func(p M) (M, error) {
		old := loadSettings()
		ch, _ := p["settings"].(M)
		next, err := mergeSettings(old, ch)
		if err != nil {
			return nil, err
		}
		st := cacheRootState(next)
		if len(st["pools"].([]string)) > 0 && !st["listed"].(bool) {
			return nil, apiField("CACHE_ROOT", st["problem"].(string))
		}
		if err := saveSettings(next); err != nil {
			return nil, apiFail("Cannot write " + SettingsCfg + ": " + err.Error())
		}
		logInfo("Settings saved")
		spawnDetached("service", "condrestart")
		return M{}, nil
	})
}

func isToken(k string) bool {
	return strings.HasSuffix(k, "_TOKEN") || strings.HasSuffix(k, "_API_KEY")
}

func mergeSettings(old Settings, ch M) (Settings, error) {
	next := Settings{}
	for _, k := range settingKeys {
		next[k] = old[k]
		if isToken(k) && ch[k+"_CLEAR"] == true {
			next[k] = ""
			continue
		}
		v, ok := ch[k]
		if !ok || v == nil {
			continue
		}
		val := strings.TrimSpace(fmt.Sprint(v))
		if b, isBool := v.(bool); isBool {
			val = map[bool]string{true: "yes", false: "no"}[b]
		}
		if isToken(k) && val == "" {
			continue
		}
		if k == "CACHE_ROOT" || k == "ARRAY_ROOT" {
			val = cleanPath(val)
		}
		next[k] = val
	}
	next = next.normalizedKeep()
	if err := validateSettings(next); err != nil {
		if fe, ok := err.(fieldError); ok {
			return nil, apiField(fe.Field, fe.Msg)
		}
		return nil, err
	}
	return next, nil
}

func (s Settings) normalizedKeep() Settings {
	for k, v := range s {
		if isSwitch(k) {
			s[k] = map[bool]string{true: "yes", false: "no"}[truthy(v)]
		}
	}
	return s
}

func migrateSettings() {
	b, err := os.ReadFile(SettingsCfg)
	if err != nil {
		return
	}
	s := parseSettings(string(b))
	if s.Render() != string(b) {
		if err := saveSettings(s); err != nil {
			logError("Cannot rewrite %s: %v", SettingsCfg, err)
			return
		}
		logInfo("Settings converted to the current format")
	}
}
