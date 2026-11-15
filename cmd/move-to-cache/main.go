package main

import (
	"os"

	"github.com/MajorPain007/unraid-move-to-cache/internal/mtc"
)

func main() {
	os.Exit(mtc.Main(os.Args[1:]))
}
