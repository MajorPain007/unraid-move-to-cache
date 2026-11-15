package mtc

import (
	"os/exec"
	"strconv"
	"strings"
	"syscall"
)

func childAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{}
}

func ctimeOf(st *syscall.Stat_t) int64 { return st.Ctimespec.Sec }

func cmdlineOf(pid int) []string {
	out, err := exec.Command("ps", "-o", "command=", "-p", strconv.Itoa(pid)).Output()
	if err != nil {
		return nil
	}
	return strings.Fields(string(out))
}
