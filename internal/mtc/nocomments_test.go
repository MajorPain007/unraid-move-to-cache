package mtc

import (
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNoComments(t *testing.T) {
	files, _ := filepath.Glob("*.go")
	more, _ := filepath.Glob("../../cmd/move-to-cache/*.go")
	for _, f := range append(files, more...) {
		src, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		parsed, err := parser.ParseFile(token.NewFileSet(), f, src, parser.ParseComments)
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range parsed.Comments {
			t.Errorf("%s has a comment: %q", f, strings.TrimSpace(c.List[0].Text))
		}
	}
}
