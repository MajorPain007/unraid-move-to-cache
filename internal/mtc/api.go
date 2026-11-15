package mtc

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"regexp"
)

type apiError struct {
	msg   string
	field string
}

func (e *apiError) Error() string { return e.msg }

func apiFail(msg string) error { return &apiError{msg: msg} }

func apiField(field, msg string) error { return &apiError{msg: msg, field: field} }

type apiAction func(p M) (M, error)

var apiActions = map[string]apiAction{}

func registerAction(name string, fn apiAction) { apiActions[name] = fn }

var reAction = regexp.MustCompile(`^[a-z_]{2,40}$`)

func apiDispatch(action string, p M) (out M) {
	fn, ok := apiActions[action]
	if !reAction.MatchString(action) || !ok {
		return M{"ok": false, "error": fmt.Sprintf("Unknown action %q - reload the page.", action)}
	}
	defer func() {
		if r := recover(); r != nil {
			out = M{"ok": false, "error": fmt.Sprint(r)}
		}
	}()
	res, err := fn(p)
	if err != nil {
		out = M{"ok": false, "error": err.Error()}
		if ae, ok := err.(*apiError); ok && ae.field != "" {
			out["field"] = ae.field
		}
		return out
	}
	if res == nil {
		res = M{}
	}
	res["ok"] = true
	return res
}

func readParams(r io.Reader) M {
	b, _ := io.ReadAll(r)
	var p M
	if json.Unmarshal(b, &p) != nil || p == nil {
		p = M{}
	}
	for k, v := range p {
		if s, ok := v.(string); ok && len(s) > 0 && (s[0] == '{' || s[0] == '[') {
			var d any
			if json.Unmarshal([]byte(s), &d) == nil {
				p[k] = d
			}
		}
	}
	return p
}

func pstr(p M, key string) string {
	if v, ok := p[key].(string); ok {
		return v
	}
	return ""
}

func init() {
	register("api", "answer one action of the web interface: api <action>, parameters as JSON on stdin", func(args []string) int {
		action := ""
		if len(args) > 0 {
			action = args[0]
		}
		os.Stdout.Write(jsonEncode(apiDispatch(action, readParams(os.Stdin))))
		return 0
	})
}
