package mtc

import (
	"fmt"
	"os"
	"strings"
	"syscall"
)

func childAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Pdeathsig: syscall.SIGTERM}
}

func ctimeOf(st *syscall.Stat_t) int64 { return st.Ctim.Sec }

func cmdlineOf(pid int) []string {
	b, err := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", pid))
	if err != nil {
		return nil
	}
	return strings.Split(strings.TrimRight(string(b), "\x00"), "\x00")
}
