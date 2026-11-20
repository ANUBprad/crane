//go:build !windows

package main

import "errors"

func startDetachedServe(string) error {
	return errors.New("detached supervisor startup is only supported on Windows")
}
