//go:build !windows

package localauth

import "errors"

func protectToken([]byte) ([]byte, error) {
	return nil, errors.New("protected token storage is only supported on Windows")
}

func unprotectToken([]byte) ([]byte, error) {
	return nil, errors.New("protected token storage is only supported on Windows")
}
