package mtc

import (
	"bufio"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

type Config struct {
	S         Settings
	Cache     string
	Array     string
	Mappings  [][2]string
	Excludes  []string
	MediaExts []string
}

var C = newConfig(Settings{}.normalized())

func newConfig(s Settings) *Config {
	c := &Config{S: s, Cache: cleanPath(s["CACHE_ROOT"]), Array: cleanPath(s["ARRAY_ROOT"])}
	c.Mappings, _ = parseMappings(s["DOCKER_MAPPINGS"])
	sort.SliceStable(c.Mappings, func(i, j int) bool { return len(c.Mappings[i][0]) > len(c.Mappings[j][0]) })
	for _, x := range strings.Split(s["EXCLUDE_DIRS"], ",") {
		if x = strings.TrimSpace(x); x != "" {
			c.Excludes = append(c.Excludes, x)
		}
	}
	for _, x := range strings.FieldsFunc(s["MEDIA_FILETYPES"], func(r rune) bool { return r == ' ' || r == ',' || r == '\t' }) {
		c.MediaExts = append(c.MediaExts, strings.ToLower(x))
	}
	return c
}

func loadConfig() { C = newConfig(loadSettings()) }

func under(p, dir string) bool {
	dir = strings.TrimRight(dir, "/")
	return p == dir || strings.HasPrefix(p, dir+"/")
}

func relativeTo(p, dir string) (string, bool) {
	dir = strings.TrimRight(dir, "/")
	if p == dir {
		return "", true
	}
	if strings.HasPrefix(p, dir+"/") {
		return p[len(dir)+1:], true
	}
	return "", false
}

func join(dir, rel string) string {
	if rel == "" {
		return dir
	}
	return strings.TrimRight(dir, "/") + "/" + rel
}

func (c *Config) physicalArray() string {
	if rel, ok := relativeTo(c.Array, MntDir+"/user"); ok {
		return join(MntDir+"/user0", rel)
	}
	return c.Array
}

func (c *Config) cacheToArray(p string) (string, bool) {
	rel, ok := relativeTo(p, c.Cache)
	if !ok {
		return "", false
	}
	return join(c.physicalArray(), rel), true
}

func (c *Config) arrayToCache(p string) string {
	if rel, ok := relativeTo(p, c.Array); ok {
		return join(c.Cache, rel)
	}
	return p
}

func (c *Config) translate(docker string) (string, bool) {
	p := strings.ReplaceAll(docker, "\\", "/")
	for _, m := range c.Mappings {
		if rel, ok := relativeTo(p, m[0]); ok {
			base := m[1]
			if !strings.HasPrefix(base, "/") {
				base = join(c.Array, base)
			}
			return join(base, rel), true
		}
	}
	return p, false
}

func (c *Config) mappedCacheDirs() []string {
	set := map[string]bool{}
	for _, m := range c.Mappings {
		h := strings.TrimRight(m[1], "/")
		if h == "" {
			continue
		}
		if rel, ok := relativeTo(h, c.Array); ok {
			set[join(c.Cache, rel)] = true
		} else if under(h, c.Cache) {
			set[h] = true
		} else if strings.HasPrefix(h, "/") {
			set[join(c.Cache, strings.TrimLeft(h, "/"))] = true
		} else {
			set[join(c.Cache, h)] = true
		}
	}
	delete(set, c.Cache)
	out := make([]string, 0, len(set))
	for d := range set {
		out = append(out, d)
	}
	sort.Strings(out)
	return out
}

func (c *Config) inMapped(p string) bool {
	for _, d := range c.mappedCacheDirs() {
		if under(p, d) {
			return true
		}
	}
	return false
}

func (c *Config) excluded(p string) bool {
	for _, part := range strings.Split(p, "/") {
		for _, e := range c.Excludes {
			if part == e {
				return true
			}
		}
	}
	return false
}

func (c *Config) isMedia(name string) bool {
	low := strings.ToLower(name)
	for _, e := range c.MediaExts {
		if strings.HasSuffix(low, e) {
			return true
		}
	}
	return false
}

func (c *Config) streamArrayPath(docker string) (string, bool) {
	p, _ := c.translate(docker)
	if p != path.Clean(p) || !strings.HasPrefix(p, "/") || !under(p, c.Array) || p == c.Array {
		return "", false
	}
	if c.excluded(p) || !c.isMedia(filepath.Base(p)) {
		return "", false
	}
	return p, true
}

var epPatterns = []*regexp.Regexp{
	regexp.MustCompile(`[sS]\d{1,4}[eE](\d{1,4})`),
	regexp.MustCompile(`(?:^|\D)\d{1,2}x(\d{2,3})(?:\D|$)`),
}

func parseEpisode(name string) (int, bool) {
	for _, re := range epPatterns {
		if m := re.FindStringSubmatch(name); m != nil {
			n, _ := strconv.Atoi(m[1])
			return n, true
		}
	}
	return 0, false
}

var reNum = regexp.MustCompile(`\d+`)

func seasonNumber(name string) (int, bool) {
	m := reNum.FindString(name)
	if m == "" {
		return 0, false
	}
	n, _ := strconv.Atoi(m)
	return n, true
}

type mount struct{ Source, Dir, Type string }

var reOctal = regexp.MustCompile(`\\([0-7]{3})`)

func unescapeMount(s string) string {
	return reOctal.ReplaceAllStringFunc(s, func(m string) string {
		n, _ := strconv.ParseUint(m[1:], 8, 8)
		return string(rune(n))
	})
}

func readMounts() []mount {
	f, err := os.Open(ProcMounts)
	if err != nil {
		return nil
	}
	defer f.Close()
	var out []mount
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fs := strings.Fields(sc.Text())
		if len(fs) >= 3 {
			out = append(out, mount{unescapeMount(fs[0]), unescapeMount(fs[1]), fs[2]})
		}
	}
	return out
}

func mountOf(p string, ms []mount) (mount, bool) {
	var best mount
	found := false
	for _, m := range ms {
		if under(p, m.Dir) && (!found || len(m.Dir) >= len(best.Dir)) {
			best, found = m, true
		}
	}
	return best, found
}

func fsIdentity(p string, ms []mount) string {
	m, ok := mountOf(p, ms)
	if !ok {
		return ""
	}
	if m.Type == "fuse.shfs" {
		return "array"
	}
	return m.Type + "\x00" + m.Source
}

func separateFilesystems(a, b string) bool {
	ms := readMounts()
	x, y := fsIdentity(a, ms), fsIdentity(b, ms)
	return x != "" && y != "" && x != y
}
