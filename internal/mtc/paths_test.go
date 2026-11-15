package mtc

import "testing"

func TestTranslateLongestFirst(t *testing.T) {
	sb := newSandbox(t)
	sb.settings(map[string]string{"DOCKER_MAPPINGS": "/media:" + MntDir + "/user/Media;/media/movies:" + MntDir + "/user/Filme"})
	if p, _ := C.translate("/media/movies/a.mkv"); p != MntDir+"/user/Filme/a.mkv" {
		t.Errorf("got %s", p)
	}
	if p, ok := C.translate("/mediax/a.mkv"); ok || p != "/mediax/a.mkv" {
		t.Errorf("boundary ignored: %s", p)
	}
	if p, _ := C.translate(`\media\b.mkv`); p != MntDir+"/user/Media/b.mkv" {
		t.Errorf("backslashes: %s", p)
	}
}

func TestStreamPathMustStayInside(t *testing.T) {
	sb := newSandbox(t)
	sb.settings(map[string]string{"DOCKER_MAPPINGS": "/data:" + MntDir + "/user/Media"})
	for _, p := range []string{"/data/../../disk1/Movies/a.mkv", "/data/./a.mkv", "/data//a.mkv", "/data/a.txt", "/other/a.mkv"} {
		if got, ok := C.streamArrayPath(p); ok {
			t.Errorf("%s accepted as %s", p, got)
		}
	}
	if _, ok := C.streamArrayPath("/data/Movies/a.mkv"); !ok {
		t.Error("good path refused")
	}
}

func TestCacheToArray(t *testing.T) {
	sb := newSandbox(t)
	for root, want := range map[string]string{MntDir + "/user": MntDir + "/user0/Media/a.mkv", MntDir + "/user/Media": MntDir + "/user0/Media/Media/a.mkv", MntDir + "/disks/x": MntDir + "/disks/x/Media/a.mkv"} {
		sb.settings(map[string]string{"ARRAY_ROOT": root})
		if got, _ := C.cacheToArray(MntDir + "/cache/Media/a.mkv"); got != want {
			t.Errorf("%s: %s, want %s", root, got, want)
		}
	}
	if _, ok := C.cacheToArray(MntDir + "/cachex/a.mkv"); ok {
		t.Error("path outside the cache accepted")
	}
}

func TestMediaTypes(t *testing.T) {
	sb := newSandbox(t)
	sb.settings(map[string]string{"MEDIA_FILETYPES": ""})
	if C.isMedia("a.mkv") || C.isMedia("passwd") {
		t.Error("empty list must mean nothing")
	}
	sb.settings(nil)
	if !C.isMedia("A.MKV") || C.isMedia("a.mkv.part") {
		t.Error("extension check")
	}
}

func TestParseEpisode(t *testing.T) {
	for name, want := range map[string]int{"Show S01E02.mkv": 2, "show.s2e113.mkv": 113, "Show 1x05.mkv": 5, "Show 2x105 x.mkv": 105} {
		if n, ok := parseEpisode(name); !ok || n != want {
			t.Errorf("%s: %d %v", name, n, ok)
		}
	}
	for _, name := range []string{"Movie 1080p.mkv", "Film 123x45.mkv"} {
		if _, ok := parseEpisode(name); ok {
			t.Errorf("%s parsed as episode", name)
		}
	}
}
