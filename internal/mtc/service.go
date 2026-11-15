package mtc

import (
	"fmt"
	"os"
	"syscall"
	"time"
)

func daemonRunning() bool { return lockHeld(DaemonLock) }

func daemonPidLive() int {
	if p := readPid(DaemonPid); pidRuns(p, "daemon") {
		return p
	}
	return 0
}

func moverPidLive() int {
	if p := readPid(MovePid); pidRuns(p, "move") {
		return p
	}
	return 0
}

func termAndWait(pid int, wait time.Duration) bool {
	syscall.Kill(pid, syscall.SIGTERM)
	for end := time.Now().Add(wait); time.Now().Before(end); time.Sleep(200 * time.Millisecond) {
		if !pidAlive(pid) {
			return true
		}
	}
	return false
}

func serviceStart() int {
	if daemonRunning() {
		fmt.Println("Move to Cache is already running.")
		return 0
	}
	if err := spawnDetached("daemon"); err != nil {
		fmt.Println("Service failed to start:", err)
		return 1
	}
	for i := 0; i < 20; i++ {
		time.Sleep(100 * time.Millisecond)
		if daemonRunning() {
			fmt.Println("Service started.")
			return 0
		}
	}
	fmt.Println("Service failed to start - see " + LogFile)
	return 1
}

func serviceStop() {
	if p := daemonPidLive(); p > 0 {
		if !termAndWait(p, 15*time.Second) {
			syscall.Kill(p, syscall.SIGKILL)
		}
	}
	fmt.Println("Service stopped.")
	if moverRunning() {
		fmt.Println("A move to the array is still running - it carries on until it is done or stopped.")
	}
}

func stopMove() {
	p := moverPidLive()
	if p == 0 {
		fmt.Println("No move to the array is running.")
		return
	}
	if termAndWait(p, 10*time.Second) {
		fmt.Println("Move to the array stopped. What was not moved yet stays on the cache.")
		return
	}
	fmt.Printf("Asked the move to the array to stop (PID %d); it is still finishing.\n", p)
}

func setDisabled(on bool) {
	if on {
		if !exists(DisabledFlag) {
			fileWrite(DisabledFlag, nil, 0o644)
		}
		return
	}
	os.Remove(DisabledFlag)
}

func runService(args []string) int {
	if len(args) != 1 {
		fmt.Fprintln(os.Stderr, "usage: move-to-cache service start|stop|restart|condrestart|boot|status|stop_move|array_stopping")
		return 2
	}
	switch args[0] {
	case "start":
		setDisabled(false)
		return serviceStart()
	case "stop":
		setDisabled(true)
		serviceStop()
		fmt.Println("Autostart disabled - the service stays stopped until you start it again.")
	case "restart":
		setDisabled(false)
		serviceStop()
		return serviceStart()
	case "condrestart":
		if exists(DisabledFlag) {
			fmt.Println("Service is disabled by user - not restarting.")
			return 0
		}
		serviceStop()
		return serviceStart()
	case "boot":
		if exists(DisabledFlag) {
			fmt.Println("Service is disabled by user - skipping autostart.")
			return 0
		}
		return serviceStart()
	case "status":
		if moverRunning() {
			fmt.Println("A move to the array is running.")
		}
		if daemonRunning() {
			fmt.Println("Move to Cache is running.")
			return 0
		}
		fmt.Println("Move to Cache is stopped.")
		return 3
	case "stop_move":
		stopMove()
	case "array_stopping":
		stopMove()
		serviceStop()
	default:
		fmt.Fprintln(os.Stderr, "unknown service command", args[0])
		return 2
	}
	return 0
}

func init() {
	register("service", "control the service: start|stop|restart|condrestart|boot|status|stop_move|array_stopping", runService)
	register("migrate", "take over settings and the cached-file list of an older version", func(args []string) int {
		migrateSettings()
		migrateTracked()
		return 0
	})
}
