package mtc

import (
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

func fakePlex(t *testing.T, token string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Plex-Token") != token {
			w.WriteHeader(401)
			return
		}
		switch r.URL.Path {
		case "/":
			w.Write([]byte(`{"MediaContainer":{"friendlyName":"Skynet"}}`))
		case "/status/sessions":
			w.Write([]byte(`{"MediaContainer":{"Metadata":[
				{"ratingKey":"1","title":"Film","viewOffset":500,"duration":1000,"Media":[{"Part":[{"file":"/media/Movies/Film.mkv"}]}]},
				{"ratingKey":"2","title":"Ep","grandparentTitle":"Show","viewOffset":10,"duration":100}]}}`))
		case "/library/metadata/2":
			w.Write([]byte(`{"MediaContainer":{"Metadata":[{"viewCount":1,"Media":[{"Part":[{"file":"/tv/Show/S01/Show S01E01.mkv"}]}]}]}}`))
		case "/library/sections":
			w.Write([]byte(`{"MediaContainer":{"Directory":[{"Location":[{"path":"/media/Movies"}]},{"Location":[{"path":"/tv"}]}]}}`))
		default:
			w.WriteHeader(404)
		}
	})
}

func fakeEmby(path string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Emby-Token") != "key" {
			w.WriteHeader(401)
			return
		}
		switch r.URL.Path {
		case "/Sessions":
			w.Write([]byte(`[{"UserId":"u1","NowPlayingItem":{"Id":"9","Name":"E1","SeriesName":"S","Path":"` + path + `","RunTimeTicks":1000},"PlayState":{"PositionTicks":950}},{"UserId":"u2"}]`))
		case "/System/Info":
			w.Write([]byte(`{"ServerName":"Jelly"}`))
		case "/Library/VirtualFolders":
			w.Write([]byte(`[{"Locations":["/media/Movies"]}]`))
		}
	})
}

func conn(key, u, token string) serverConn {
	d, _ := serverByKey(key)
	return serverConn{d, u, token, true, ""}
}

func TestPlexStreams(t *testing.T) {
	newSandbox(t)
	ts := httptest.NewServer(fakePlex(t, "tok"))
	defer ts.Close()
	list, err := conn("PLEX", ts.URL, "tok").streams()
	if err != nil || len(list) != 2 {
		t.Fatalf("%v %v", list, err)
	}
	if list[0].Path != "/media/Movies/Film.mkv" || *list[0].Progress != 0.5 {
		t.Errorf("first %+v", list[0])
	}
	if list[1].Path != "/tv/Show/S01/Show S01E01.mkv" || list[1].Title != "Show - Ep" {
		t.Errorf("second %+v", list[1])
	}
}

func TestEmbyJellyfinStreams(t *testing.T) {
	newSandbox(t)
	ts := httptest.NewServer(fakeEmby("/media/S/S01E01.mkv"))
	defer ts.Close()
	for _, k := range []string{"EMBY", "JELLYFIN"} {
		list, err := conn(k, ts.URL, "key").streams()
		if err != nil || len(list) != 1 || list[0].Path != "/media/S/S01E01.mkv" || *list[0].Progress != 0.95 || list[0].User != "u1" {
			t.Fatalf("%s: %+v %v", k, list, err)
		}
	}
}

func TestFetchErrorsAreNotEmpty(t *testing.T) {
	newSandbox(t)
	apiTimeout = 300 * time.Millisecond
	defer func() { apiTimeout = 5 * time.Second }()
	html := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("<html>")) }))
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { time.Sleep(time.Second) }))
	plex := httptest.NewServer(fakePlex(t, "tok"))
	defer html.Close()
	defer slow.Close()
	defer plex.Close()
	dead := httptest.NewServer(http.NotFoundHandler())
	dead.Close()
	for want, u := range map[string]string{"response": html.URL, "timeout": slow.URL, "token": plex.URL, "unreachable": dead.URL} {
		list, err := conn("PLEX", u, "wrong").streams()
		if err == nil || err.Kind != want || list != nil {
			t.Errorf("%s: %v %v", want, list, err)
		}
	}
}

func TestTLSVerifyAndPin(t *testing.T) {
	newSandbox(t)
	ts := httptest.NewTLSServer(fakePlex(t, "tok"))
	defer ts.Close()
	c := conn("PLEX", ts.URL, "tok")
	if _, err := c.streams(); err == nil || err.Kind != "certificate" {
		t.Fatalf("self-signed accepted: %v", err)
	}
	c.Pin = certFingerprint(ts.Certificate())
	if _, err := c.streams(); err != nil {
		t.Fatalf("pinned: %v", err)
	}
	c.Pin = strings.Repeat("0", 64)
	if _, err := c.streams(); err == nil || err.Kind != "certificate" {
		t.Fatalf("wrong pin accepted: %v", err)
	}
	c.Pin, c.Verify = "", false
	if _, err := c.streams(); err != nil {
		t.Fatalf("verify off: %v", err)
	}
}

func TestConnectionTestMessages(t *testing.T) {
	sb := newSandbox(t)
	sb.settings(map[string]string{"PLEX_TOKEN": "tok", "DOCKER_MAPPINGS": "/media:" + MntDir + "/user/Media"})
	sb.file(MntDir+"/user0/Media/Movies/Film.mkv", 1, past)
	ts := httptest.NewServer(fakePlex(t, "tok"))
	defer ts.Close()
	r := apiDispatch("test", M{"service": "plex", "url": ts.URL, "token": "bad"})
	if r["success"] != false || r["kind"] != "token" || !strings.Contains(r["message"].(string), "refuses the token (HTTP 401)") || r["advice"] == "" {
		t.Fatalf("wrong token: %v", r)
	}
	r = apiDispatch("test", M{"service": "plex", "url": ts.URL, "token": ""})
	if r["success"] != true || !strings.Contains(r["message"].(string), "Skynet") {
		t.Fatalf("saved token: %v", r)
	}
	streams := r["streams"].([]M)
	if streams[0]["state"] != "ok" || streams[0]["host"] != MntDir+"/user/Media/Movies/Film.mkv" {
		t.Errorf("stream preview %v", streams[0])
	}
	if streams[1]["state"] != "unmapped" || !strings.Contains(streams[1]["text"].(string), "/tv") {
		t.Errorf("unmapped preview %v", streams[1])
	}
	if strings.Contains(string(jsonEncode(r)), "tok\"") {
		t.Error("token in the answer")
	}
}

func TestTrustCertificate(t *testing.T) {
	sb := newSandbox(t)
	ts := httptest.NewTLSServer(fakePlex(t, "tok"))
	defer ts.Close()
	sb.settings(map[string]string{"PLEX_TOKEN": "tok", "PLEX_URL": ts.URL, "ENABLE_PLEX": "yes"})
	os.WriteFile(sb.Root+"/mtc", []byte("#!/bin/sh\n"), 0o755)
	SelfBin = sb.Root + "/mtc"
	r := apiDispatch("test", M{"service": "plex", "url": ts.URL})
	cert, _ := r["certificate"].(M)
	if r["kind"] != "certificate" || cert == nil || cert["sha256"] != certFingerprint(ts.Certificate()) {
		t.Fatalf("no fingerprint: %v", r)
	}
	if r = apiDispatch("trust_cert", M{"service": "plex", "sha256": cert["sha256"]}); r["ok"] != true {
		t.Fatal(r)
	}
	loadConfig()
	if C.S["PLEX_CERT_SHA256"] != cert["sha256"] {
		t.Fatal("pin not saved")
	}
	if _, err := C.conn(servers[0]).streams(); err != nil {
		t.Fatalf("pinned server refused: %v", err)
	}
}

func TestURLSchemes(t *testing.T) {
	newSandbox(t)
	for _, u := range []string{"file:///etc/passwd", "ftp://x", "gopher://x", "http://"} {
		if r := apiDispatch("test", M{"service": "plex", "url": u, "token": "x"}); r["ok"] != false {
			t.Errorf("%s accepted: %v", u, r)
		}
	}
}

func TestTokenNeverLogged(t *testing.T) {
	sb := newSandbox(t)
	dead := httptest.NewServer(http.NotFoundHandler())
	dead.Close()
	sb.settings(map[string]string{"ENABLE_PLEX": "yes", "PLEX_URL": dead.URL, "PLEX_TOKEN": "supersecret"})
	d := newDaemon()
	d.pass()
	d.writeStatus()
	st, _ := os.ReadFile(StatusFile)
	ov := jsonEncode(apiDispatch("overview", M{}))
	for what, s := range map[string]string{"log": sb.log(), "status": string(st), "overview": string(ov)} {
		if strings.Contains(s, "supersecret") {
			t.Errorf("token in %s", what)
		}
	}
}
