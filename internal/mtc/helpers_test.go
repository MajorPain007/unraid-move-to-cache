package mtc

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type sandbox struct {
	t    *testing.T
	Root string
	Mnt  string
}

const fakeRsync = `#!/bin/sh
rm=0
for a in "$@"; do [ "$a" = --remove-source-files ] && rm=1; done
eval src=\${$(($#-1))}
eval dst=\${$#}
[ -n "$FAKE_RSYNC_LOG" ] && echo "$src -> $dst" >> "$FAKE_RSYNC_LOG"
[ -n "$FAKE_RSYNC_SLEEP" ] && sleep "$FAKE_RSYNC_SLEEP"
if [ -n "$FAKE_RSYNC_EXIT" ]; then exit "$FAKE_RSYNC_EXIT"; fi
cp -p "$src" "$dst" || exit 11
[ $rm = 1 ] && rm -f "$src"
exit 0
`

func newSandbox(t *testing.T) *sandbox {
	root, _ := filepath.EvalSymlinks(t.TempDir())
	s := &sandbox{t: t, Root: root, Mnt: root + "/mnt"}
	ConfigDir, TmpDir, LogFile, MntDir = root+"/cfg", root+"/tmp", root+"/log", s.Mnt
	ProcMounts = root + "/mounts"
	RsyncBin = root + "/rsync"
	NotifyBin = root + "/notify"
	ZfsBin = root + "/zfs"
	setPaths()
	for _, d := range []string{ConfigDir, TmpDir, s.Mnt + "/cache", s.Mnt + "/user0"} {
		os.MkdirAll(d, 0o755)
	}
	os.Symlink(s.Mnt+"/user0", s.Mnt+"/user")
	os.WriteFile(s.Mnt+"/cache/.keep", nil, 0o644)
	os.WriteFile(RsyncBin, []byte(fakeRsync), 0o755)
	os.WriteFile(NotifyBin, []byte("#!/bin/sh\necho \"$*\" >> \""+root+"/notified\"\n"), 0o755)
	s.mounts("cache btrfs", "user fuse.shfs", "user0 fuse.shfs")
	trackedCache.data = nil
	notified = map[string]bool{}
	stopCtx, stopAll = context.WithCancel(context.Background())
	rsyncRetryDelay = 10 * time.Millisecond
	poolSpace = func() (space, bool) { return space{0, 1 << 50, 1 << 49}, true }
	s.settings(nil)
	return s
}

func (s *sandbox) mounts(entries ...string) {
	var b strings.Builder
	b.WriteString("rootfs / rootfs rw 0 0\n")
	for _, e := range entries {
		f := strings.Fields(e)
		b.WriteString(f[0] + "dev " + s.Mnt + "/" + f[0] + " " + f[1] + " rw 0 0\n")
	}
	os.WriteFile(ProcMounts, []byte(b.String()), 0o644)
}

func (s *sandbox) settings(extra map[string]string) {
	st := Settings{}.normalized()
	st["ARRAY_ROOT"] = s.Mnt + "/user"
	st["CACHE_ROOT"] = s.Mnt + "/cache"
	st["DOCKER_MAPPINGS"] = "/media:" + s.Mnt + "/user/Media"
	for k, v := range extra {
		st[k] = v
	}
	saveSettings(st)
	C = newConfig(st)
}

func (s *sandbox) file(p string, size int, mtime time.Time) string {
	os.MkdirAll(filepath.Dir(p), 0o755)
	os.WriteFile(p, make([]byte, size), 0o644)
	os.Chtimes(p, mtime, mtime)
	return p
}

func (s *sandbox) log() string {
	b, _ := os.ReadFile(LogFile)
	return string(b)
}

func (s *sandbox) notified() string {
	b, _ := os.ReadFile(s.Root + "/notified")
	return string(b)
}

var past = time.Now().Add(-2 * time.Hour).Truncate(time.Second)
