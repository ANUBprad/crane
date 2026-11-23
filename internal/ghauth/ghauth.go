package ghauth

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	defaultDeviceCodeURL = "https://github.com/login/device/code"
	defaultTokenURL      = "https://github.com/login/oauth/access_token"
	defaultAPIBase       = "https://api.github.com"
	defaultScope         = "read:user"
	tokenFileName        = "github_auth.dpapi"
)

// Config controls GitHub device flow authentication.
type Config struct {
	ClientID      string
	Scope         string
	DeviceCodeURL string
	TokenURL      string
	APIBase       string
	HTTPClient    *http.Client
	DataDir       string
}

// Token is the persisted GitHub auth state. Access token is never printed.
type Token struct {
	AccessToken string    `json:"access_token"`
	TokenType   string    `json:"token_type,omitempty"`
	Scope       string    `json:"scope,omitempty"`
	ObtainedAt  time.Time `json:"obtained_at"`
}

// DeviceCode is the user-facing device flow prompt.
type DeviceCode struct {
	DeviceCode      string `json:"device_code"`
	UserCode        string `json:"user_code"`
	VerificationURI string `json:"verification_uri"`
	ExpiresIn       int    `json:"expires_in"`
	Interval        int    `json:"interval"`
}

type tokenResponse struct {
	AccessToken string `json:"access_token"`
	TokenType   string `json:"token_type"`
	Scope       string `json:"scope"`
	Error       string `json:"error"`
	ErrorDesc   string `json:"error_description"`
}

func (c Config) withDefaults() Config {
	if c.Scope == "" {
		c.Scope = defaultScope
	}
	if c.DeviceCodeURL == "" {
		c.DeviceCodeURL = defaultDeviceCodeURL
	}
	if c.TokenURL == "" {
		c.TokenURL = defaultTokenURL
	}
	if c.APIBase == "" {
		c.APIBase = defaultAPIBase
	}
	if c.HTTPClient == nil {
		c.HTTPClient = &http.Client{Timeout: 15 * time.Second}
	}
	return c
}

func (c Config) tokenPath() (string, error) {
	if c.DataDir == "" {
		return "", errors.New("GitHub auth data directory is not configured")
	}
	return filepath.Join(c.DataDir, tokenFileName), nil
}

// ClientIDFromEnv returns the explicitly configured GitHub client ID.
func ClientIDFromEnv() string {
	return strings.TrimSpace(os.Getenv("CRANE_GITHUB_CLIENT_ID"))
}

// StartDeviceFlow requests a device code from GitHub.
func StartDeviceFlow(ctx context.Context, cfg Config) (DeviceCode, error) {
	cfg = cfg.withDefaults()
	if cfg.ClientID == "" {
		return DeviceCode{}, errors.New("GitHub client ID is required; set CRANE_GITHUB_CLIENT_ID or pass --client-id")
	}
	form := url.Values{}
	form.Set("client_id", cfg.ClientID)
	form.Set("scope", cfg.Scope)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, cfg.DeviceCodeURL, strings.NewReader(form.Encode()))
	if err != nil {
		return DeviceCode{}, err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := cfg.HTTPClient.Do(req)
	if err != nil {
		return DeviceCode{}, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode >= 400 {
		return DeviceCode{}, fmt.Errorf("GitHub device flow request failed: %s", resp.Status)
	}
	var dc DeviceCode
	if err := json.Unmarshal(body, &dc); err != nil {
		return DeviceCode{}, fmt.Errorf("invalid device flow response: %w", err)
	}
	if dc.DeviceCode == "" || dc.UserCode == "" || dc.VerificationURI == "" {
		return DeviceCode{}, errors.New("incomplete device flow response from GitHub")
	}
	if dc.Interval <= 0 {
		dc.Interval = 5
	}
	return dc, nil
}

// PollForToken polls GitHub until the device flow completes, fails, or times out.
func PollForToken(ctx context.Context, cfg Config, dc DeviceCode) (Token, error) {
	cfg = cfg.withDefaults()
	if cfg.ClientID == "" {
		return Token{}, errors.New("GitHub client ID is required")
	}
	interval := time.Duration(dc.Interval) * time.Second
	if interval <= 0 {
		interval = 5 * time.Second
	}
	deadline := time.Now().Add(15 * time.Minute)
	if dc.ExpiresIn > 0 {
		d := time.Now().Add(time.Duration(dc.ExpiresIn) * time.Second)
		if d.Before(deadline) {
			deadline = d
		}
	}
	for {
		if time.Now().After(deadline) {
			return Token{}, errors.New("GitHub device flow timed out; run agent auth login again")
		}
		select {
		case <-ctx.Done():
			return Token{}, ctx.Err()
		case <-time.After(interval):
		}
		form := url.Values{}
		form.Set("client_id", cfg.ClientID)
		form.Set("device_code", dc.DeviceCode)
		form.Set("grant_type", "urn:ietf:params:oauth:grant-type:device_code")
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, cfg.TokenURL, strings.NewReader(form.Encode()))
		if err != nil {
			return Token{}, err
		}
		req.Header.Set("Accept", "application/json")
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		resp, err := cfg.HTTPClient.Do(req)
		if err != nil {
			return Token{}, err
		}
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		resp.Body.Close()
		if resp.StatusCode >= 400 {
			return Token{}, fmt.Errorf("GitHub token request failed: %s", resp.Status)
		}
		var tr tokenResponse
		if err := json.Unmarshal(body, &tr); err != nil {
			return Token{}, fmt.Errorf("invalid token response: %w", err)
		}
		switch tr.Error {
		case "":
			if tr.AccessToken == "" {
				return Token{}, errors.New("GitHub token response missing access_token")
			}
			return Token{AccessToken: tr.AccessToken, TokenType: tr.TokenType, Scope: tr.Scope, ObtainedAt: time.Now().UTC()}, nil
		case "authorization_pending":
			continue
		case "slow_down":
			interval += 5 * time.Second
			continue
		case "access_denied":
			return Token{}, errors.New("GitHub authorization was denied")
		case "expired_token":
			return Token{}, errors.New("GitHub device code expired; run agent auth login again")
		default:
			if tr.ErrorDesc != "" {
				return Token{}, fmt.Errorf("GitHub device flow error: %s", tr.ErrorDesc)
			}
			return Token{}, fmt.Errorf("GitHub device flow error: %s", tr.Error)
		}
	}
}

// SaveToken persists the token securely. The token value is never logged.
func SaveToken(cfg Config, token Token) error {
	cfg = cfg.withDefaults()
	path, err := cfg.tokenPath()
	if err != nil {
		return err
	}
	data, err := json.Marshal(token)
	if err != nil {
		return err
	}
	return saveToken(path, string(data))
}

// LoadToken reads the persisted token, if present.
func LoadToken(cfg Config) (Token, bool, error) {
	cfg = cfg.withDefaults()
	path, err := cfg.tokenPath()
	if err != nil {
		return Token{}, false, err
	}
	raw, err := loadToken(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return Token{}, false, nil
		}
		return Token{}, false, err
	}
	var token Token
	if err := json.Unmarshal([]byte(raw), &token); err != nil {
		return Token{}, false, fmt.Errorf("stored GitHub auth token is invalid: %w", err)
	}
	if token.AccessToken == "" {
		return Token{}, false, nil
	}
	return token, true, nil
}

// DeleteToken removes the persisted token.
func DeleteToken(cfg Config) error {
	cfg = cfg.withDefaults()
	path, err := cfg.tokenPath()
	if err != nil {
		return err
	}
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// Status describes the current GitHub auth state without exposing the token.
type Status struct {
	Authenticated bool   `json:"authenticated"`
	Scope         string `json:"scope,omitempty"`
	TokenType     string `json:"token_type,omitempty"`
	ObtainedAt    string `json:"obtained_at,omitempty"`
	Login         string `json:"login,omitempty"`
	Validated     bool   `json:"validated"`
	ValidationErr string `json:"validation_error,omitempty"`
}

// GetStatus reports stored auth state. If validate is true, it makes an explicit
// GitHub API request to confirm the token works.
func GetStatus(ctx context.Context, cfg Config, validate bool) (Status, error) {
	cfg = cfg.withDefaults()
	token, ok, err := LoadToken(cfg)
	if err != nil {
		return Status{}, err
	}
	if !ok {
		return Status{Authenticated: false}, nil
	}
	st := Status{
		Authenticated: true,
		Scope:         token.Scope,
		TokenType:     token.TokenType,
	}
	if !token.ObtainedAt.IsZero() {
		st.ObtainedAt = token.ObtainedAt.UTC().Format(time.RFC3339)
	}
	if !validate {
		return st, nil
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(cfg.APIBase, "/")+"/user", nil)
	if err != nil {
		st.ValidationErr = "validation request could not be created"
		return st, nil
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("Authorization", "Bearer "+token.AccessToken)
	resp, err := cfg.HTTPClient.Do(req)
	if err != nil {
		st.ValidationErr = "GitHub API validation failed"
		return st, nil
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != 200 {
		st.ValidationErr = fmt.Sprintf("GitHub API returned %s", resp.Status)
		return st, nil
	}
	var user struct {
		Login string `json:"login"`
	}
	if json.Unmarshal(body, &user) == nil {
		st.Login = user.Login
	}
	st.Validated = true
	return st, nil
}
