package mtc

import "syscall"

func childAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Pdeathsig: syscall.SIGTERM}
}

func ctimeOf(st *syscall.Stat_t) int64 { return st.Ctim.Sec }
