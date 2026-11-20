//go:build windows

package localauth

import (
	"errors"
	"unsafe"

	"golang.org/x/sys/windows"
)

// entropy binds the protected blob to this application and API version.
var tokenEntropy = []byte("CRANE local API token v1")

func protectToken(raw []byte) ([]byte, error) {
	if len(raw) == 0 {
		return nil, errors.New("cannot protect empty token")
	}
	in := windows.DataBlob{Data: &raw[0], Size: uint32(len(raw))}
	entropy := windows.DataBlob{Data: &tokenEntropy[0], Size: uint32(len(tokenEntropy))}
	var out windows.DataBlob
	if err := windows.CryptProtectData(
		&in, nil, &entropy, 0, nil, windows.CRYPTPROTECT_UI_FORBIDDEN, &out,
	); err != nil {
		return nil, err
	}
	defer windows.LocalFree(windows.Handle(unsafe.Pointer(out.Data)))
	if out.Data == nil || out.Size == 0 {
		return nil, errors.New("protected token blob is empty")
	}
	result := make([]byte, out.Size)
	copy(result, unsafe.Slice((*byte)(unsafe.Pointer(out.Data)), out.Size))
	return result, nil
}

func unprotectToken(blob []byte) ([]byte, error) {
	if len(blob) == 0 {
		return nil, errors.New("cannot unprotect empty token blob")
	}
	in := windows.DataBlob{Data: &blob[0], Size: uint32(len(blob))}
	entropy := windows.DataBlob{Data: &tokenEntropy[0], Size: uint32(len(tokenEntropy))}
	var out windows.DataBlob
	if err := windows.CryptUnprotectData(
		&in, nil, &entropy, 0, nil, windows.CRYPTPROTECT_UI_FORBIDDEN, &out,
	); err != nil {
		return nil, err
	}
	defer windows.LocalFree(windows.Handle(unsafe.Pointer(out.Data)))
	if out.Data == nil || out.Size == 0 {
		return nil, errors.New("unprotected token is empty")
	}
	result := make([]byte, out.Size)
	copy(result, unsafe.Slice((*byte)(unsafe.Pointer(out.Data)), out.Size))
	return result, nil
}
