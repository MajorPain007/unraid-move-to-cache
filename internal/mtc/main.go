package mtc

import (
	"fmt"
	"os"
	"sort"
)

var buildName = "plex_to_cache"

var (
	Name       = envOr("MTC_PLUGIN_NAME", buildName)
	ConfigDir  = envOr("MTC_CONFIG_DIR", "/boot/config/plugins/"+Name)
	TmpDir     = envOr("MTC_TMP_DIR", "/tmp/"+Name)
	LogFile    = envOr("MTC_LOG_FILE", "/var/log/"+Name+".log")
	MntDir     = envOr("MTC_MNT", "/mnt")
	ProcMounts = envOr("MTC_MOUNTS", "/proc/self/mounts")
	RsyncBin   = envOr("MTC_RSYNC", "rsync")
	ZfsBin     = envOr("MTC_ZFS", "zfs")
	NotifyBin  = envOr("MTC_NOTIFY", "/usr/local/emhttp/webGui/scripts/notify")
	SelfBin    = envOr("MTC_BIN", selfPath())
)

var (
	SettingsCfg, TrackedFile, LegacyList, DisabledFlag, StatusFile, DaemonLock, DaemonPid, TrackedLock, MoveQueue, MoveLock, MovePid, MoveStatus string
)

func init() { setPaths() }

func setPaths() {
	SettingsCfg = ConfigDir + "/settings.cfg"
	TrackedFile = ConfigDir + "/tracked.json"
	LegacyList = ConfigDir + "/cached_files.list"
	DisabledFlag = ConfigDir + "/service_disabled"
	StatusFile = TmpDir + "/status.json"
	DaemonLock = TmpDir + "/daemon.lock"
	DaemonPid = TmpDir + "/daemon.pid"
	TrackedLock = TmpDir + "/tracked.lock"
	MoveQueue = TmpDir + "/move.queue"
	MoveLock = TmpDir + "/move.lock"
	MovePid = TmpDir + "/move.pid"
	MoveStatus = TmpDir + "/move.json"
}

type command struct {
	run  func(args []string) int
	help string
}

var commands = map[string]command{}

func register(name, help string, run func(args []string) int) {
	commands[name] = command{run: run, help: help}
}

func Main(args []string) int {
	if len(args) == 0 {
		names := make([]string, 0, len(commands))
		for n := range commands {
			names = append(names, n)
		}
		sort.Strings(names)
		fmt.Fprintln(os.Stderr, "usage: move-to-cache <command> [args]")
		for _, n := range names {
			fmt.Fprintf(os.Stderr, "  %-10s %s\n", n, commands[n].help)
		}
		return 2
	}
	c, ok := commands[args[0]]
	if !ok {
		fmt.Fprintf(os.Stderr, "unknown command %q\n", args[0])
		return 2
	}
	return c.run(args[1:])
}

func selfPath() string {
	p, err := os.Executable()
	if err != nil {
		return "/usr/local/emhttp/plugins/" + Name + "/bin/move-to-cache"
	}
	return p
}
