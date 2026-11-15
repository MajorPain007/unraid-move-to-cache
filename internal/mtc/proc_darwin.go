package mtc

import "syscall"

func childAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{}
}

func ctimeOf(st *syscall.Stat_t) int64 { return st.Ctimespec.Sec }
