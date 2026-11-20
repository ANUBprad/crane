//go:build !windows

package main

import (
	"errors"
	"os/exec"
)

func startDetachedCommand(*exec.Cmd) error {
	return errors.New("detached processes are only supported on Windows")
}

func startDetachedServe(string) error {
	return errors.New("detached supervisor startup is only supported on Windows")
}
