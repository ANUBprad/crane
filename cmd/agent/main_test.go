package main

import (
	"bytes"
	"encoding/base64"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestRunAPITokenLifecycleAndTaskCreate(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("protected installation tokens are validated on Windows")
	}
	root := t.TempDir()
	t.Setenv("LOCALAPPDATA", root)

	var versionOut bytes.Buffer
	if err := run([]string{"--version"}, &versionOut, &bytes.Buffer{}); err != nil {
		t.Fatalf("--version error = %v", err)
	}
	if !strings.Contains(versionOut.String(), "CRANE version:") {
		t.Fatalf("unexpected version output: %q", versionOut.String())
	}

	var out, errOut bytes.Buffer
	if err := run([]string{"api-token"}, &out, &errOut); err != nil {
		t.Fatalf("api-token error = %v", err)
	}
	original := strings.TrimSpace(out.String())
	decoded, err := base64.RawURLEncoding.DecodeString(original)
	if err != nil || len(decoded) != 32 {
		t.Fatalf("token = %q, decoded length = %d, error = %v", original, len(decoded), err)
	}

	out.Reset()
	if err := run([]string{"api-token"}, &out, &errOut); err != nil {
		t.Fatalf("second api-token error = %v", err)
	}
	if strings.TrimSpace(out.String()) != original {
		t.Fatal("token changed between retrievals")
	}

	out.Reset()
	if err := run([]string{"api-token", "--rotate"}, &out, &errOut); err != nil {
		t.Fatalf("api-token --rotate error = %v", err)
	}
	rotated := strings.TrimSpace(out.String())
	if rotated == original || rotated == "" {
		t.Fatal("rotation did not produce a new token")
	}

	if err := run([]string{"api-token", "--revoke"}, &out, &errOut); err != nil {
		t.Fatalf("api-token --revoke error = %v", err)
	}
	out.Reset()
	errOut.Reset()
	if err := run([]string{"api-token"}, &out, &errOut); err == nil {
		t.Fatal("token retrieval succeeded after revocation")
	}
	if strings.Contains(errOut.String(), original) || strings.Contains(errOut.String(), rotated) {
		t.Fatal("error output leaked a token")
	}
	if out.Len() != 0 {
		t.Fatalf("token retrieval emitted stdout after revocation: %q", out.String())
	}

	out.Reset()
	if err := run([]string{"task", "create", "--id", "e2e-task", "--input", "private input"}, &out, &errOut); err != nil {
		t.Fatalf("task create error = %v", err)
	}
	if !strings.Contains(out.String(), "created task e2e-task") {
		t.Fatalf("unexpected task output: %q", out.String())
	}
	if strings.Contains(out.String(), "private input") {
		t.Fatal("task output exposed private input")
	}
	if _, err := filepath.Abs(root); err != nil {
		t.Fatal(err)
	}
}
