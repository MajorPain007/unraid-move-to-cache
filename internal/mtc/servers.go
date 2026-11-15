package mtc

import (
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type serverConn struct {
	Def    servDef
	URL    string
	Token  string
	Verify bool
	Pin    string
}

func (c *Config) conn(d servDef) serverConn {
	return serverConn{d, strings.TrimRight(c.S[d.Key+"_URL"], "/"), c.S[d.TokenKey], c.S.On(d.Key + "_VERIFY_TLS"), c.S[d.Key+"_CERT_SHA256"]}
}

type stream struct {
	Path     string   `json:"path"`
	Service  string   `json:"service"`
	ID       string   `json:"id"`
	User     string   `json:"user"`
	Title    string   `json:"title"`
	Progress *float64 `json:"progress"`
}

type fetchError struct {
	Kind string
	Msg  string
}

func (e *fetchError) Error() string { return e.Msg }

var apiTimeout = 5 * time.Second

func certFingerprint(c *x509.Certificate) string {
	s := sha256.Sum256(c.Raw)
	return hex.EncodeToString(s[:])
}

func (s serverConn) client() *http.Client {
	tc := &tls.Config{}
	if !s.Verify {
		tc.InsecureSkipVerify = true
	} else if s.Pin != "" {
		pin := s.Pin
		tc.InsecureSkipVerify = true
		tc.VerifyPeerCertificate = func(raw [][]byte, _ [][]*x509.Certificate) error {
			if len(raw) == 0 {
				return errors.New("no certificate")
			}
			c, err := x509.ParseCertificate(raw[0])
			if err != nil {
				return err
			}
			if certFingerprint(c) != pin {
				return fmt.Errorf("pinned certificate mismatch: the server now shows %s", certFingerprint(c))
			}
			return nil
		}
	}
	tr := &http.Transport{TLSClientConfig: tc, Proxy: nil, DialContext: (&net.Dialer{Timeout: apiTimeout}).DialContext}
	return &http.Client{Timeout: apiTimeout, Transport: tr, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}

func (s serverConn) get(path string, out any) *fetchError {
	if !validURL(s.URL) {
		return &fetchError{"config", s.Def.Label + " URL must start with http:// or https://"}
	}
	req, err := http.NewRequest("GET", s.URL+path, nil)
	if err != nil {
		return &fetchError{"config", err.Error()}
	}
	req.Header.Set("Accept", "application/json")
	if s.Def.Key == "PLEX" {
		req.Header.Set("X-Plex-Token", s.Token)
	} else {
		req.Header.Set("X-Emby-Token", s.Token)
	}
	resp, err := s.client().Do(req)
	if err != nil {
		return classify(s, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		return &fetchError{"token", fmt.Sprintf("%s refuses the token (HTTP %d)", s.Def.Label, resp.StatusCode)}
	}
	if resp.StatusCode != 200 {
		return &fetchError{"response", fmt.Sprintf("%s answered HTTP %d", s.Def.Label, resp.StatusCode)}
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		return classify(s, err)
	}
	if json.Unmarshal(b, out) != nil {
		return &fetchError{"response", s.Def.Label + " did not answer with JSON - is the URL the media server itself?"}
	}
	return nil
}

func classify(s serverConn, err error) *fetchError {
	msg := err.Error()
	if s.Token != "" {
		msg = strings.ReplaceAll(msg, s.Token, "***")
	}
	var ue *url.Error
	if errors.As(err, &ue) {
		msg = ue.Err.Error()
	}
	var cv *tls.CertificateVerificationError
	var ua x509.UnknownAuthorityError
	var hn x509.HostnameError
	var ci x509.CertificateInvalidError
	if errors.As(err, &cv) || errors.As(err, &ua) || errors.As(err, &hn) || errors.As(err, &ci) || strings.Contains(msg, "pinned certificate") {
		return &fetchError{"certificate", fmt.Sprintf("The certificate of %s is not trusted: %s", s.Def.Label, msg)}
	}
	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		return &fetchError{"timeout", fmt.Sprintf("%s did not answer within %s", s.Def.Label, apiTimeout)}
	}
	return &fetchError{"unreachable", fmt.Sprintf("%s cannot be reached: %s", s.Def.Label, msg)}
}

type plexPart struct {
	File string `json:"file"`
}

type plexMeta struct {
	RatingKey        string  `json:"ratingKey"`
	Title            string  `json:"title"`
	GrandparentTitle string  `json:"grandparentTitle"`
	ViewOffset       float64 `json:"viewOffset"`
	Duration         float64 `json:"duration"`
	ViewCount        int     `json:"viewCount"`
	User             struct {
		Title string `json:"title"`
	} `json:"User"`
	Media []struct {
		Part []plexPart `json:"Part"`
	} `json:"Media"`
}

type plexContainer struct {
	MediaContainer *struct {
		FriendlyName string     `json:"friendlyName"`
		Metadata     []plexMeta `json:"Metadata"`
		Directory    []struct {
			Location []struct {
				Path string `json:"path"`
			} `json:"Location"`
		} `json:"Directory"`
	} `json:"MediaContainer"`
}

func (m plexMeta) file() string {
	for _, med := range m.Media {
		for _, p := range med.Part {
			if p.File != "" {
				return p.File
			}
		}
	}
	return ""
}

func progressOf(pos, dur float64) *float64 {
	if dur <= 0 {
		return nil
	}
	p := min(max(pos/dur, 0), 1)
	return &p
}

var plexPaths = map[string]string{}

func (s serverConn) plexStreams() ([]stream, *fetchError) {
	var c plexContainer
	if err := s.get("/status/sessions", &c); err != nil {
		return nil, err
	}
	if c.MediaContainer == nil {
		return nil, &fetchError{"response", "Plex sent an unexpected answer (no MediaContainer)"}
	}
	out := []stream{}
	for _, m := range c.MediaContainer.Metadata {
		p := m.file()
		if p == "" {
			p = plexPaths[m.RatingKey]
		}
		if p == "" && m.RatingKey != "" {
			var md plexContainer
			if s.get("/library/metadata/"+url.PathEscape(m.RatingKey), &md) == nil && md.MediaContainer != nil {
				for _, x := range md.MediaContainer.Metadata {
					if p = x.file(); p != "" {
						break
					}
				}
			}
		}
		if p == "" {
			continue
		}
		if len(plexPaths) >= 500 {
			plexPaths = map[string]string{}
		}
		plexPaths[m.RatingKey] = p
		title := m.Title
		if m.GrandparentTitle != "" {
			title = m.GrandparentTitle + " - " + m.Title
		}
		out = append(out, stream{p, "plex", m.RatingKey, m.User.Title, title, progressOf(m.ViewOffset, m.Duration)})
	}
	return out, nil
}

type embySession struct {
	UserID         string `json:"UserId"`
	UserName       string `json:"UserName"`
	NowPlayingItem *struct {
		ID           string  `json:"Id"`
		Name         string  `json:"Name"`
		SeriesName   string  `json:"SeriesName"`
		Path         string  `json:"Path"`
		RunTimeTicks float64 `json:"RunTimeTicks"`
	} `json:"NowPlayingItem"`
	PlayState *struct {
		PositionTicks float64 `json:"PositionTicks"`
	} `json:"PlayState"`
}

func (s serverConn) embyStreams() ([]stream, *fetchError) {
	var list []embySession
	if err := s.get("/Sessions", &list); err != nil {
		return nil, err
	}
	out := []stream{}
	for _, x := range list {
		it := x.NowPlayingItem
		if it == nil || it.Path == "" {
			continue
		}
		var pos float64
		if x.PlayState != nil {
			pos = x.PlayState.PositionTicks
		}
		title := it.Name
		if it.SeriesName != "" {
			title = it.SeriesName + " - " + it.Name
		}
		out = append(out, stream{it.Path, strings.ToLower(s.Def.Label), it.ID, x.UserID, title, progressOf(pos, it.RunTimeTicks)})
	}
	return out, nil
}

func (s serverConn) streams() ([]stream, *fetchError) {
	if s.Def.Key == "PLEX" {
		return s.plexStreams()
	}
	return s.embyStreams()
}

func (s serverConn) playedFlag(st stream) bool {
	if s.Def.Key == "PLEX" {
		var c plexContainer
		if s.get("/library/metadata/"+url.PathEscape(st.ID), &c) == nil && c.MediaContainer != nil && len(c.MediaContainer.Metadata) > 0 {
			return c.MediaContainer.Metadata[0].ViewCount > 0
		}
		return false
	}
	var item struct {
		UserData struct {
			Played bool `json:"Played"`
		} `json:"UserData"`
	}
	if s.get("/Users/"+url.PathEscape(st.User)+"/Items/"+url.PathEscape(st.ID), &item) == nil {
		return item.UserData.Played
	}
	return false
}

func watched(st stream) bool {
	if st.Progress != nil {
		return *st.Progress >= 0.9
	}
	for _, d := range servers {
		if strings.EqualFold(d.Label, st.Service) {
			return C.conn(d).playedFlag(st)
		}
	}
	return false
}

func probeCert(raw string) M {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" {
		return nil
	}
	host := u.Host
	if u.Port() == "" {
		host += ":443"
	}
	conn, err := tls.DialWithDialer(&net.Dialer{Timeout: apiTimeout}, "tcp", host, &tls.Config{InsecureSkipVerify: true})
	if err != nil {
		return nil
	}
	defer conn.Close()
	certs := conn.ConnectionState().PeerCertificates
	if len(certs) == 0 {
		return nil
	}
	c := certs[0]
	return M{"sha256": certFingerprint(c), "subject": c.Subject.String(), "issuer": c.Issuer.String(),
		"names": c.DNSNames, "not_after": c.NotAfter.Format("2006-01-02")}
}

var advice = map[string]string{
	"token":       "Copy the token again: Plex: Settings, open any item, Get Info, View XML, X-Plex-Token in the address. Emby/Jellyfin: Dashboard, API Keys.",
	"unreachable": "Check the address and port, and that the container is running. Inside Docker, localhost is the container itself; use the server's IP.",
	"timeout":     "The server is reachable but slow or blocked by a firewall. Check address and port.",
	"certificate": "Trust this certificate below (recommended for a self-signed one or https://<IP> with Plex), or switch off \"Verify certificate\".",
	"response":    "The address does not lead to the media server's API. Use the base address, e.g. http://192.168.1.10:32400.",
	"config":      "Enter the address with http:// or https://.",
}

func pathPreview(c *Config, docker string) M {
	host, mapped := c.translate(docker)
	row := M{"container": docker, "host": host}
	switch {
	case !mapped && !under(host, c.Array):
		first := "/" + strings.SplitN(strings.TrimLeft(docker, "/"), "/", 2)[0]
		row["state"] = "unmapped"
		row["text"] = "No Docker mapping for " + first + " - add one under Docker path mappings."
	case !under(host, c.Array):
		row["state"] = "outside"
		row["text"] = "Lies outside the Array Root " + c.Array + " - nothing would be cached."
	case exists(host):
		row["state"] = "ok"
		row["text"] = "Found on the host. A stream from here would be cached to " + c.arrayToCache(host) + "."
	default:
		row["state"] = "missing"
		row["text"] = "Not found on the host - check the mapping."
	}
	return row
}

func (s serverConn) libraryPaths() []string {
	var out []string
	if s.Def.Key == "PLEX" {
		var c plexContainer
		if s.get("/library/sections", &c) == nil && c.MediaContainer != nil {
			for _, d := range c.MediaContainer.Directory {
				for _, l := range d.Location {
					out = append(out, l.Path)
				}
			}
		}
		return out
	}
	var vf []struct {
		Locations []string `json:"Locations"`
	}
	if s.get("/Library/VirtualFolders", &vf) == nil {
		for _, v := range vf {
			out = append(out, v.Locations...)
		}
	}
	return out
}

func serverByKey(k string) (servDef, bool) {
	for _, d := range servers {
		if strings.EqualFold(d.Key, k) {
			return d, true
		}
	}
	return servDef{}, false
}

func boolParam(p M, k string, def bool) bool {
	switch v := p[k].(type) {
	case bool:
		return v
	case string:
		return truthy(v)
	}
	return def
}

func connectionTest(p M) (M, error) {
	d, ok := serverByKey(pstr(p, "service"))
	if !ok {
		return nil, apiFail("Unknown media server")
	}
	saved := loadSettings()
	cfg := newConfig(saved)
	if m, ok := p["mappings"].(string); ok {
		if _, err := parseMappings(m); err != nil {
			return nil, apiField("DOCKER_MAPPINGS", err.Error())
		}
		s2 := Settings{}
		for k, v := range saved {
			s2[k] = v
		}
		s2["DOCKER_MAPPINGS"] = m
		cfg = newConfig(s2)
	}
	sc := serverConn{d, strings.TrimRight(strings.TrimSpace(pstr(p, "url")), "/"), strings.TrimSpace(pstr(p, "token")), boolParam(p, "verify", true), pstr(p, "pin")}
	if sc.Token == "" {
		sc.Token = saved[d.TokenKey]
	}
	if !validURL(sc.URL) {
		return nil, apiField(d.Key+"_URL", d.Label+" URL must start with http:// or https://")
	}
	if sc.Token == "" {
		return M{"success": false, "kind": "token", "message": "No token entered and none saved.", "advice": advice["token"]}, nil
	}
	name := ""
	var ferr *fetchError
	if d.Key == "PLEX" {
		var c plexContainer
		if ferr = sc.get("/", &c); ferr == nil {
			if c.MediaContainer == nil {
				ferr = &fetchError{"response", "Plex sent an unexpected answer (no MediaContainer)"}
			} else {
				name = c.MediaContainer.FriendlyName
			}
		}
	} else {
		var info struct {
			ServerName string `json:"ServerName"`
		}
		if ferr = sc.get("/System/Info", &info); ferr == nil {
			name = info.ServerName
		}
	}
	if ferr != nil {
		out := M{"success": false, "kind": ferr.Kind, "message": ferr.Msg, "advice": advice[ferr.Kind]}
		if ferr.Kind == "certificate" {
			out["certificate"] = probeCert(sc.URL)
		}
		return out, nil
	}
	preview := []M{}
	if list, err := sc.streams(); err == nil {
		for _, st := range list {
			row := pathPreview(cfg, st.Path)
			row["title"] = st.Title
			preview = append(preview, row)
		}
	}
	libs := []M{}
	for _, l := range sc.libraryPaths() {
		libs = append(libs, pathPreview(cfg, l))
	}
	return M{"success": true, "message": fmt.Sprintf("Connected to %s %q.", d.Label, name), "streams": preview, "libraries": libs}, nil
}

func init() {
	registerAction("test", connectionTest)
	registerAction("trust_cert", func(p M) (M, error) {
		d, ok := serverByKey(pstr(p, "service"))
		fp := strings.ToLower(pstr(p, "sha256"))
		if !ok || !reSHA.MatchString(fp) {
			return nil, apiFail("Unknown media server or malformed fingerprint")
		}
		l := lockFile(TmpDir+"/settings.lock", true)
		defer l.Unlock()
		s := loadSettings()
		s[d.Key+"_CERT_SHA256"] = fp
		s[d.Key+"_VERIFY_TLS"] = "yes"
		if err := saveSettings(s); err != nil {
			return nil, apiFail("Cannot write " + SettingsCfg + ": " + err.Error())
		}
		logInfo("%s: certificate %s is trusted from now on", d.Label, fp)
		spawnDetached("service", "condrestart")
		return M{"message": "Trusted. Only this certificate is accepted from now on."}, nil
	})
}
