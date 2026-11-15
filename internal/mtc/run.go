package mtc

import (
	"bytes"
	"context"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

type result struct {
	Code           int
	Stdout, Stderr string
}

func (r result) Message() string {
	msg := strings.TrimSpace(r.Stderr)
	if msg == "" {
		msg = strings.TrimSpace(r.Stdout)
	}
	if msg == "" {
		return "no error output"
	}
	return msg
}

func runCmd(timeout time.Duration, name string, args ...string) result {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	code := 0
	if err := cmd.Run(); err != nil {
		code = -1
		if ee, ok := err.(*exec.ExitError); ok && ee.ExitCode() >= 0 {
			code = ee.ExitCode()
		}
		if errb.Len() == 0 {
			errb.WriteString(err.Error())
		}
	}
	return result{code, out.String(), errb.String()}
}
