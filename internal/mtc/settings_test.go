package mtc

import (
	"os"
	"strings"
	"testing"
)

const pythonCfg = `ENABLE_PLEX="True"
PLEX_URL="https://192.168.20.3:32400"
PLEX_TOKEN="secret-token-123"
ENABLE_EMBY="False"
EMBY_URL="http://localhost:8096"
EMBY_API_KEY=""
ENABLE_JELLYFIN="False"
JELLYFIN_URL="http://localhost:8096"
JELLYFIN_API_KEY=""
CHECK_INTERVAL="10"
CACHE_MAX_USAGE="85"
COPY_DELAY="30"
CLEANUP_MODE="smart"
MOVIE_DELETE_DELAY="1800"
EPISODE_KEEP_PREVIOUS="2"
CACHE_MAX_DAYS="7"
EXCLUDE_DIRS=""
MEDIA_FILETYPES=".mkv .mp4 .avi"
ARRAY_ROOT="/mnt/user"
CACHE_ROOT="/mnt/cache"
DOCKER_MAPPINGS="/data:/mnt/user/Media"
ENABLE_EPISODE_BATCHING="True"
EPISODE_BATCH_SIZE="30"
EPISODE_BATCH_TOLERANCE="10"
EPISODE_BATCH_PREFETCH="4"
ENABLE_CACHE_EVICTION="True"
ENABLE_NEXT_SEASON_PREFETCH="False"
FLUSH_LAST="123"
`

func TestMigratePythonSettings(t *testing.T) {
	newSandbox(t)
	os.WriteFile(SettingsCfg, []byte(pythonCfg), 0o644)
	migrateSettings()
	s := loadSettings()
	b, _ := os.ReadFile(SettingsCfg)
	out := string(b)
	for k, v := range map[string]string{"ENABLE_PLEX": "yes", "PLEX_TOKEN": "secret-token-123", "CACHE_MAX_USAGE": "85",
		"CLEANUP_MODE": "smart", "PLEX_VERIFY_TLS": "yes", "ENABLE_EMBY": "no", "DOCKER_MAPPINGS": "/data:/mnt/user/Media",
		"ENABLE_EPISODE_BATCHING": "yes", "NOTIFY_OFFLINE_MINUTES": "10"} {
		if s[k] != v || !strings.Contains(out, k+"=\""+v+"\"\n") {
			t.Errorf("%s = %q, want %q", k, s[k], v)
		}
	}
	if strings.Contains(out, "FLUSH_LAST") || strings.Contains(out, "True") {
		t.Errorf("old keys or values left:\n%s", out)
	}
}

func TestSettingsGetHidesTokens(t *testing.T) {
	newSandbox(t)
	s := loadSettings()
	s["PLEX_TOKEN"] = "secret-token-123"
	saveSettings(s)
	out := apiDispatch("settings_get", M{})
	if strings.Contains(string(jsonEncode(out)), "secret-token-123") {
		t.Fatal("token sent to the browser")
	}
	if out["stored"].(M)["PLEX_TOKEN"] != true {
		t.Fatal("stored flag missing")
	}
}

func TestSettingsSaveTokens(t *testing.T) {
	sb := newSandbox(t)
	sb.settings(map[string]string{"PLEX_TOKEN": "old"})
	os.WriteFile(sb.Root+"/mtc", []byte("#!/bin/sh\n"), 0o755)
	SelfBin = sb.Root + "/mtc"
	save := func(ch M) M { return apiDispatch("settings_save", M{"settings": ch}) }
	if r := save(M{"PLEX_TOKEN": ""}); r["ok"] != true || loadSettings()["PLEX_TOKEN"] != "old" {
		t.Fatalf("empty field changed the token: %v", r)
	}
	save(M{"PLEX_TOKEN": "new"})
	if loadSettings()["PLEX_TOKEN"] != "new" {
		t.Fatal("token not replaced")
	}
	save(M{"PLEX_TOKEN_CLEAR": true})
	if loadSettings()["PLEX_TOKEN"] != "" {
		t.Fatal("token not cleared")
	}
}

func TestStorageProblem(t *testing.T) {
	newSandbox(t)
	m := MntDir
	bad := [][2]string{{m + "/user", m + "/user"}, {m + "/user/Media", "/srv"}, {m + "/user0", "/srv"}, {m + "/disk3", "/srv"},
		{m + "/disk3/x", "/srv"}, {m + "/cache", m + "/cache"}, {m + "/cache", m + "/cache/x"}, {"cache", m + "/user"}, {"/", m + "/user"}, {m + "/cache", ""}}
	for _, c := range bad {
		if storageProblem(c[0], c[1]) == "" {
			t.Errorf("accepted cache %q array %q", c[0], c[1])
		}
	}
	if p := storageProblem(m+"/cache", m+"/user"); p != "" {
		t.Errorf("refused a good pair: %s", p)
	}
	if storageProblem(m+"/diskette", m+"/user") != "" {
		t.Error("diskette is not a disk")
	}
}

func TestListPools(t *testing.T) {
	sb := newSandbox(t)
	sb.mounts("cache zfs", "fast xfs", "user fuse.shfs", "user0 fuse.shfs", "disk1 xfs", "disks/ssd1 xfs", "remotes/nas cifs", "cache/Media zfs")
	got := strings.Join(listPools(MntDir+"/user"), " ")
	want := strings.Join([]string{MntDir + "/cache", MntDir + "/fast", MntDir + "/disks/ssd1"}, " ")
	if got != want {
		t.Fatalf("pools %q, want %q", got, want)
	}
}

func TestInvalidCacheRootShownNotReplaced(t *testing.T) {
	sb := newSandbox(t)
	sb.settings(map[string]string{"CACHE_ROOT": MntDir + "/oldpool"})
	st := apiDispatch("settings_get", M{})["cache_root"].(M)
	if st["current"] != MntDir+"/oldpool" || st["listed"] != false || st["problem"] == "" {
		t.Fatalf("state %v", st)
	}
	r := apiDispatch("settings_save", M{"settings": M{"COPY_DELAY": "40"}})
	if r["ok"] != false || r["field"] != "CACHE_ROOT" {
		t.Fatalf("saved with an unlisted cache root: %v", r)
	}
	if loadSettings()["CACHE_ROOT"] != MntDir+"/oldpool" || loadSettings()["COPY_DELAY"] != "30" {
		t.Fatal("settings changed")
	}
}

func TestSettingsValidation(t *testing.T) {
	newSandbox(t)
	before, _ := os.ReadFile(SettingsCfg)
	cases := map[string]M{
		"EXCLUDE_DIRS":    {"EXCLUDE_DIRS": "a\nCACHE_ROOT=/"},
		"MEDIA_FILETYPES": {"MEDIA_FILETYPES": `.mkv"`},
		"CHECK_INTERVAL":  {"CHECK_INTERVAL": "0"},
		"CACHE_MAX_USAGE": {"CACHE_MAX_USAGE": "abc"},
		"PLEX_URL":        {"ENABLE_PLEX": true, "PLEX_URL": "file:///etc/passwd"},
		"EMBY_URL":        {"EMBY_URL": "ftp://x"},
		"DOCKER_MAPPINGS": {"DOCKER_MAPPINGS": "media:/mnt/user/Media"},
		"CLEANUP_MODE":    {"CLEANUP_MODE": "all"},
	}
	for field, ch := range cases {
		r := apiDispatch("settings_save", M{"settings": ch})
		if r["ok"] != false || r["field"] != field {
			t.Errorf("%s: %v", field, r)
		}
	}
	if r := apiDispatch("settings_save", M{"settings": M{"DOCKER_MAPPINGS": "/media"}}); r["ok"] != false {
		t.Error("mapping without colon accepted")
	}
	after, _ := os.ReadFile(SettingsCfg)
	if string(after) != string(before) {
		t.Error("settings written despite errors")
	}
}
