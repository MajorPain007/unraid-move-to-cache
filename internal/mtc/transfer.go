package mtc

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

var (
	stopCtx, stopAll = context.WithCancel(context.Background())
	errStopped       = errors.New("stopped")
	rsyncRetries     = 3
	rsyncRetryDelay  = 5 * time.Second
	rsyncTick        = time.Second
)

func stopped() bool { return stopCtx.Err() != nil }

func rsyncTimeout(src string) time.Duration {
	var size int64
	if st, err := os.Stat(src); err == nil {
		size = st.Size()
	}
	return 600*time.Second + time.Duration(size/(10<<20))*time.Second
}

func runRsync(args []string, timeout time.Duration, tick func()) (int, string) {
	cmd := exec.Command(RsyncBin, args...)
	cmd.SysProcAttr = childAttr()
	var errb strings.Builder
	cmd.Stderr = &errb
	cmd.WaitDelay = 2 * time.Second
	if err := cmd.Start(); err != nil {
		return -2, err.Error()
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	ticker := time.NewTicker(rsyncTick)
	defer ticker.Stop()
	for {
		select {
		case err := <-done:
			if err == nil {
				return 0, errb.String()
			}
			if ee, ok := err.(*exec.ExitError); ok && ee.ExitCode() >= 0 {
				return ee.ExitCode(), errb.String()
			}
			return -2, errb.String() + err.Error()
		case <-stopCtx.Done():
			cmd.Process.Signal(syscall.SIGTERM)
			select {
			case <-done:
			case <-time.After(30 * time.Second):
				cmd.Process.Kill()
				<-done
			}
			return -3, "stopped"
		case <-deadline.C:
			cmd.Process.Kill()
			<-done
			return -1, fmt.Sprintf("rsync timed out after %s", timeout)
		case <-ticker.C:
			if tick != nil {
				tick()
			}
		}
	}
}

func sameCopy(a, b os.FileInfo) bool {
	d := a.ModTime().Sub(b.ModTime())
	return a.Size() == b.Size() && d > -2*time.Second && d < 2*time.Second
}

func removeLeftovers(dst string) {
	m, _ := filepath.Glob(filepath.Join(filepath.Dir(dst), "."+escapeGlob(filepath.Base(dst))+".??????"))
	for _, p := range m {
		os.Remove(p)
	}
}

func escapeGlob(s string) string {
	return strings.NewReplacer(`\`, `\\`, `*`, `\*`, `?`, `\?`, `[`, `\[`).Replace(s)
}

func tempOf(dst string) string {
	m, _ := filepath.Glob(filepath.Join(filepath.Dir(dst), "."+escapeGlob(filepath.Base(dst))+".??????"))
	if len(m) == 0 {
		return ""
	}
	return m[0]
}

func transfer(src, dst string, removeSource bool, tick func()) error {
	args := []string{"-a", "--fsync"}
	if removeSource {
		args = append(args, "--remove-source-files")
	}
	args = append(args, src, dst)
	timeout := rsyncTimeout(src)
	last := ""
	for attempt := 1; attempt <= rsyncRetries; attempt++ {
		if stopped() {
			return errStopped
		}
		code, stderr := runRsync(args, timeout, tick)
		if code == 0 {
			return nil
		}
		if code == -3 || stopped() {
			removeLeftovers(dst)
			return errStopped
		}
		if code == -1 {
			removeLeftovers(dst)
		}
		lines := []string{}
		for _, l := range strings.Split(strings.TrimSpace(stderr), "\n") {
			if strings.TrimSpace(l) != "" {
				lines = append(lines, strings.TrimSpace(l))
			}
		}
		last = fmt.Sprintf("exit status %d", code)
		if len(lines) > 0 {
			last = strings.Join(lines[max(len(lines)-3, 0):], "; ")
		}
		if code == 23 || code == 24 {
			s, serr := os.Stat(src)
			d, derr := os.Stat(dst)
			if derr == nil && (os.IsNotExist(serr) || (serr == nil && sameCopy(s, d))) {
				logWarn("rsync finished with warnings (exit %d) but %s is complete: %s", code, filepath.Base(dst), last)
				if removeSource && serr == nil {
					if err := os.Remove(src); err != nil {
						logWarn("Could not remove the source after the transfer: %v", err)
					}
				}
				return nil
			}
		}
		if attempt < rsyncRetries {
			logWarn("rsync attempt %d/%d failed (exit %d) for %s: %s - retrying in %s", attempt, rsyncRetries, code, filepath.Base(src), last, rsyncRetryDelay)
			select {
			case <-stopCtx.Done():
				return errStopped
			case <-time.After(rsyncRetryDelay):
			}
		}
	}
	return fmt.Errorf("rsync failed: %s", last)
}

type twinState int

const (
	twinNone twinState = iota
	twinSame
	twinCacheNewer
	twinArrayNewer
	twinUnclear
)

func twin(cachePath string) (twinState, error) {
	arrayPath, ok := C.cacheToArray(cachePath)
	if !ok {
		return 0, fmt.Errorf("%s is not under %s", cachePath, C.Cache)
	}
	a, err := os.Stat(arrayPath)
	if os.IsNotExist(err) {
		return twinNone, nil
	}
	if err != nil {
		return 0, err
	}
	c, err := os.Stat(cachePath)
	if err != nil {
		return 0, err
	}
	if !separateFilesystems(cachePath, arrayPath) {
		return 0, fmt.Errorf("%s and %s are not on two separate filesystems - they may be one and the same file, so neither is touched", cachePath, arrayPath)
	}
	d := c.ModTime().Sub(a.ModTime())
	if d > -2*time.Second && d < 2*time.Second {
		if c.Size() == a.Size() {
			return twinSame, nil
		}
		return twinUnclear, nil
	}
	if d > 0 {
		return twinCacheNewer, nil
	}
	return twinArrayNewer, nil
}

func setOwner(dst string, st os.FileInfo, file bool) {
	sys, ok := st.Sys().(*syscall.Stat_t)
	if !ok {
		return
	}
	if l, err := os.Lstat(dst); err != nil || l.Mode()&os.ModeSymlink != 0 {
		return
	}
	if err := os.Lchown(dst, int(sys.Uid), int(sys.Gid)); err != nil {
		logWarn("Could not give %s the owner of its original: %v", dst, err)
	}
	mode := st.Mode().Perm() | st.Mode()&(os.ModeSetgid|os.ModeSetuid|os.ModeSticky)
	if file {
		mode = st.Mode().Perm()
	}
	os.Chmod(dst, mode)
}

func mirrorParents(cachePath string) ([]string, error) {
	rel, ok := relativeTo(filepath.Dir(cachePath), C.Cache)
	if !ok {
		return nil, fmt.Errorf("%s is not under %s", cachePath, C.Cache)
	}
	dst, src := C.physicalArray(), C.Cache
	if !isDir(dst) {
		return nil, fmt.Errorf("%s does not exist - is the array started?", dst)
	}
	var created []string
	for _, part := range strings.Split(rel, "/") {
		if part == "" {
			continue
		}
		dst, src = dst+"/"+part, src+"/"+part
		if isDir(dst) {
			continue
		}
		if err := os.Mkdir(dst, 0o755); err != nil {
			removeDirs(created)
			return nil, err
		}
		created = append(created, dst)
		if st, err := os.Stat(src); err == nil {
			setOwner(dst, st, false)
		}
	}
	return created, nil
}

func removeDirs(dirs []string) {
	for i := len(dirs) - 1; i >= 0; i-- {
		if os.Remove(dirs[i]) != nil {
			return
		}
	}
}

func cleanupEmptyDirs(p string) {
	keep := map[string]bool{C.Cache: true}
	for _, d := range C.mappedCacheDirs() {
		keep[d] = true
	}
	for d := filepath.Dir(p); under(d, C.Cache) && !keep[d]; d = filepath.Dir(d) {
		if os.Remove(d) != nil {
			return
		}
	}
}

type moveResult string

const (
	resMoved   moveResult = "moved"
	resDropped moveResult = "dropped"
	resGone    moveResult = "gone"
	resKept    moveResult = "kept"
	resStopped moveResult = "stopped"
	resFailed  moveResult = "failed"
)

func moveFileToArray(cachePath string, tick func()) (moveResult, int64) {
	name := filepath.Base(cachePath)
	arrayPath, ok := C.cacheToArray(cachePath)
	if !ok {
		logWarn("%s is not under %s - left where it is", cachePath, C.Cache)
		return resKept, 0
	}
	st, err := os.Stat(cachePath)
	if os.IsNotExist(err) {
		return resGone, 0
	}
	if err != nil {
		logError("Move failed for %s: %v", name, err)
		return resFailed, 0
	}
	if !separateFilesystems(cachePath, C.physicalArray()) {
		logError("Move refused for %s: %s and %s are not on two separate filesystems", name, C.Cache, C.physicalArray())
		return resFailed, 0
	}
	tw, err := twin(cachePath)
	if err != nil {
		logError("Move failed for %s: %v", name, err)
		return resFailed, 0
	}
	switch tw {
	case twinSame, twinArrayNewer:
		if tw == twinArrayNewer {
			logWarn("%s: the copy on the array is newer - removing the one on the cache", name)
		}
		if err := os.Remove(cachePath); err != nil {
			logError("Move failed for %s: %v", name, err)
			return resFailed, 0
		}
		cleanupEmptyDirs(cachePath)
		return resDropped, st.Size()
	case twinUnclear:
		logWarn("%s is also on the array, just as old but a different size. Left on the cache: compare the two and delete the one that is wrong.", name)
		return resKept, 0
	}
	var created []string
	if tw == twinCacheNewer {
		logInfo("%s was changed on the cache after it was cached - it replaces the older copy on the array", name)
	} else if created, err = mirrorParents(cachePath); err != nil {
		logError("Move failed for %s: %v", name, err)
		return resFailed, 0
	}
	if err := transfer(cachePath, arrayPath, true, tick); err != nil {
		removeDirs(created)
		if err == errStopped {
			return resStopped, 0
		}
		logError("Move failed for %s: %v", name, err)
		return resFailed, 0
	}
	cleanupEmptyDirs(cachePath)
	return resMoved, st.Size()
}

func copyToCache(arrayPath string) error {
	cachePath := C.arrayToCache(arrayPath)
	if cachePath == arrayPath {
		return fmt.Errorf("%s is not under %s", arrayPath, C.Array)
	}
	rel, _ := relativeTo(filepath.Dir(cachePath), C.Cache)
	cur, orig := C.Cache, C.physicalArray()
	for _, part := range strings.Split(rel, "/") {
		if part == "" {
			continue
		}
		cur, orig = cur+"/"+part, orig+"/"+part
		if isDir(cur) {
			continue
		}
		if err := os.Mkdir(cur, 0o755); err != nil {
			return err
		}
		if st, err := os.Stat(orig); err == nil {
			setOwner(cur, st, false)
		}
	}
	if err := transfer(arrayPath, cachePath, false, nil); err != nil {
		return err
	}
	if orig, ok := C.cacheToArray(cachePath); ok {
		if st, err := os.Stat(orig); err == nil {
			setOwner(cachePath, st, true)
		}
	}
	return nil
}

const legacyPartial = ".plex_to_cache-partial"

func reconcile() {
	t := loadTracked()
	var stale []string
	for p := range t {
		if !exists(p) {
			stale = append(stale, p)
		}
	}
	adopt := map[string]float64{}
	cleared := 0
	for _, dir := range C.mappedCacheDirs() {
		if !isDir(dir) {
			continue
		}
		filepath.WalkDir(dir, func(p string, d os.DirEntry, err error) error {
			if err != nil {
				return nil
			}
			if d.IsDir() {
				if d.Name() == legacyPartial {
					os.RemoveAll(p)
					cleared++
					return filepath.SkipDir
				}
				if p != dir && strings.HasPrefix(d.Name(), ".") {
					return filepath.SkipDir
				}
				return nil
			}
			if strings.HasPrefix(d.Name(), ".") || !C.isMedia(d.Name()) || !d.Type().IsRegular() {
				return nil
			}
			if _, ok := t[p]; ok {
				return nil
			}
			if tw, err := twin(p); err == nil && tw == twinSame {
				if st, err := os.Stat(p); err == nil {
					if sys, ok := st.Sys().(*syscall.Stat_t); ok {
						adopt[p] = float64(ctimeOf(sys))
					}
				}
			}
			return nil
		})
	}
	updateTracked(adopt, stale)
	if len(stale)+len(adopt)+cleared > 0 {
		logInfo("[Reconcile] Tracked list: %d stale entries removed, %d orphaned cache copies adopted, %d folder(s) of interrupted copies removed", len(stale), len(adopt), cleared)
	}
}
