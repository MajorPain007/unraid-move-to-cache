package mtc

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

type M = map[string]any

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func jsonEncode(v any) []byte {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return []byte("null\n")
	}
	return buf.Bytes()
}

func jsonReadInto(path string, v any) bool {
	b, err := os.ReadFile(path)
	return err == nil && json.Unmarshal(b, v) == nil
}

func fileWrite(path string, data []byte, mode os.FileMode) error {
	if old, err := os.ReadFile(path); err == nil && bytes.Equal(old, data) {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := fmt.Sprintf("%s.tmp%d", path, os.Getpid())
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	_, err = f.Write(data)
	if err == nil {
		err = f.Sync()
	}
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err == nil {
		os.Chmod(tmp, mode)
		err = os.Rename(tmp, path)
	}
	if err != nil {
		os.Remove(tmp)
	}
	return err
}

type fileLock struct{ f *os.File }

func (l *fileLock) Unlock() {
	if l != nil && l.f != nil {
		syscall.Flock(int(l.f.Fd()), syscall.LOCK_UN)
		l.f.Close()
		l.f = nil
	}
}

func lockFile(path string, block bool) *fileLock {
	os.MkdirAll(filepath.Dir(path), 0o755)
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o644)
	if err != nil {
		return nil
	}
	how := syscall.LOCK_EX
	if !block {
		how |= syscall.LOCK_NB
	}
	if syscall.Flock(int(f.Fd()), how) != nil {
		f.Close()
		return nil
	}
	return &fileLock{f: f}
}

func lockHeld(path string) bool {
	l := lockFile(path, false)
	if l == nil {
		return exists(path)
	}
	l.Unlock()
	return false
}

func exists(p string) bool {
	_, err := os.Lstat(p)
	return err == nil
}

func isDir(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.IsDir()
}

func pidAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	if b, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid)); err == nil {
		if i := bytes.LastIndexByte(b, ')'); i > 0 && i+2 < len(b) && b[i+2] == 'Z' {
			return false
		}
	}
	err := syscall.Kill(pid, 0)
	return err == nil || err == syscall.EPERM
}

func readPid(path string) int {
	b, _ := os.ReadFile(path)
	n, _ := strconv.Atoi(strings.TrimSpace(string(b)))
	return n
}

func pidRuns(pid int, args ...string) bool {
	if !pidAlive(pid) {
		return false
	}
	parts := cmdlineOf(pid)
	if len(parts) < len(args)+1 || filepath.Base(parts[0]) != "move-to-cache" {
		return false
	}
	for i, a := range args {
		if parts[i+1] != a {
			return false
		}
	}
	return true
}

func humanBytes(n int64) string {
	f := float64(n)
	for _, u := range []string{"B", "KB", "MB", "GB"} {
		if f < 1024 {
			if u == "B" {
				return fmt.Sprintf("%d B", n)
			}
			return fmt.Sprintf("%.1f %s", f, u)
		}
		f /= 1024
	}
	return fmt.Sprintf("%.1f TB", f)
}

var (
	logMu    sync.Mutex
	logMax   int64 = 5 << 20
	logTag         = ""
	logSinks       = []func(string){}
)

func logLine(level, format string, a ...any) {
	msg := fmt.Sprintf(format, a...)
	line := time.Now().Format("2006-01-02 15:04:05") + " " + level + " " + logTag + msg + "\n"
	logMu.Lock()
	defer logMu.Unlock()
	if st, err := os.Stat(LogFile); err == nil && st.Size() > logMax {
		os.Rename(LogFile, LogFile+".1")
	}
	f, err := os.OpenFile(LogFile, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o644)
	if err == nil {
		f.WriteString(line)
		f.Close()
	}
	for _, s := range logSinks {
		s(line)
	}
}

func logInfo(format string, a ...any)  { logLine("INFO ", format, a...) }
func logWarn(format string, a ...any)  { logLine("WARN ", format, a...) }
func logError(format string, a ...any) { logLine("ERROR", format, a...) }

func tail(path string, lines int) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	st, _ := f.Stat()
	start := max(st.Size()-512*1024, 0)
	buf := make([]byte, st.Size()-start)
	n, _ := f.ReadAt(buf, start)
	data := strings.ToValidUTF8(string(buf[:n]), "�")
	if start > 0 {
		if i := strings.IndexByte(data, '\n'); i >= 0 {
			data = data[i+1:]
		}
	}
	all := strings.Split(strings.TrimRight(data, "\n"), "\n")
	if len(all) > lines {
		all = all[len(all)-lines:]
	}
	return strings.Join(all, "\n")
}

var notified = map[string]bool{}
var notifyMu sync.Mutex

func notifyOnce(cause, subject, description string) {
	notifyMu.Lock()
	defer notifyMu.Unlock()
	if notified[cause] {
		return
	}
	notified[cause] = true
	if st, err := os.Stat(NotifyBin); err != nil || st.Mode()&0o111 == 0 {
		return
	}
	cmd := exec.Command(NotifyBin, "-e", "Move to Cache", "-s", subject, "-d", description, "-i", "alert")
	cmd.Dir = "/"
	cmd.Run()
}

func notifyResolved(cause string) {
	notifyMu.Lock()
	delete(notified, cause)
	notifyMu.Unlock()
}

func spawnDetached(args ...string) error {
	out, err := os.OpenFile(LogFile, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o644)
	if err != nil {
		out = nil
	}
	cmd := exec.Command(SelfBin, args...)
	cmd.Dir = "/"
	cmd.Stdout = out
	cmd.Stderr = out
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	err = cmd.Start()
	if out != nil {
		out.Close()
	}
	if err == nil {
		go cmd.Wait()
	}
	return err
}
