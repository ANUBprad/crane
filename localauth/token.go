// Package localauth owns the per-install secret used by the loopback API.
//
// The token is generated once, stored as an OS-protected blob, and never
// exposed through the browser/renderer. Rotation revokes the previous token
// before persisting the replacement; revocation removes the active token.
package localauth

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

const (
	stateVersion = 1
	tokenBytes   = 32
	maxStateSize = 64 << 10
)

var (
	ErrNoActiveToken = errors.New("no active installation token")
	ErrInvalidToken  = errors.New("invalid installation token record")
)

type tokenFile struct {
	Version     int             `json:"version"`
	ActiveID    string          `json:"active_id,omitempty"`
	ActiveToken string          `json:"active_token,omitempty"`
	Revoked     []revokedRecord `json:"revoked,omitempty"`
	CreatedAt   string          `json:"created_at"`
	UpdatedAt   string          `json:"updated_at"`
}

type revokedRecord struct {
	ID        string `json:"id"`
	Hash      string `json:"hash"`
	RevokedAt string `json:"revoked_at"`
}

// Manager keeps the active token in memory and persists protected state on
// disk. It is safe for concurrent API requests.
type Manager struct {
	mu          sync.RWMutex
	path        string
	activeID    string
	activeToken []byte
	revokedHash [][32]byte
	revokedIDs  []string
	createdAt   string
}

// DefaultPath returns the per-user installation token store path.
func DefaultPath() string {
	if p := os.Getenv("LOCALAPPDATA"); p != "" {
		return filepath.Join(p, "CRANE", "auth", "token.json")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return filepath.Join(".crane", "auth", "token.json")
	}
	return filepath.Join(home, ".crane", "auth", "token.json")
}

// NewManager prepares a manager for path without reading or creating state.
func NewManager(path string) (*Manager, error) {
	if path == "" {
		return nil, errors.New("token store path must not be empty")
	}
	return &Manager{path: path}, nil
}

// Path returns the token store path.
func (m *Manager) Path() string { return m.path }

// LoadOrCreate loads existing state, or creates it when the file is absent.
// Corrupt or inaccessible state is never silently replaced.
func (m *Manager) LoadOrCreate() error {
	err := m.Load()
	if err == nil {
		return nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return m.create()
}

// Load reads protected token state from disk.
func (m *Manager) Load() error {
	data, err := os.ReadFile(m.path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return err
		}
		return fmt.Errorf("read installation token state: %w", err)
	}
	if len(data) > maxStateSize {
		return errors.New("installation token state is too large")
	}
	var file tokenFile
	if err := json.Unmarshal(data, &file); err != nil {
		return fmt.Errorf("decode installation token state: %w", err)
	}
	if file.Version != stateVersion {
		return fmt.Errorf("unsupported installation token state version %d", file.Version)
	}

	var active []byte
	if file.ActiveToken != "" {
		blob, err := base64.RawStdEncoding.DecodeString(file.ActiveToken)
		if err != nil {
			return fmt.Errorf("decode protected token blob: %w", err)
		}
		active, err = unprotectToken(blob)
		if err != nil {
			return fmt.Errorf("unprotect installation token: %w", err)
		}
		if len(active) != tokenBytes {
			return ErrInvalidToken
		}
	}

	hashes := make([][32]byte, 0, len(file.Revoked))
	ids := make([]string, 0, len(file.Revoked))
	for _, item := range file.Revoked {
		hash, err := hex.DecodeString(item.Hash)
		if err != nil || len(hash) != sha256.Size {
			return ErrInvalidToken
		}
		var h [32]byte
		copy(h[:], hash)
		hashes = append(hashes, h)
		ids = append(ids, item.ID)
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	m.activeID = file.ActiveID
	m.activeToken = active
	m.revokedHash = hashes
	m.revokedIDs = ids
	m.createdAt = file.CreatedAt
	return nil
}

// CurrentToken returns the active token for trusted local integrations only.
func (m *Manager) CurrentToken() (string, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if len(m.activeToken) == 0 {
		return "", ErrNoActiveToken
	}
	return encodeToken(m.activeToken), nil
}

// Verify checks a presented bearer token using constant-time comparison.
// Revoked tokens are rejected even when a new active token exists.
func (m *Manager) Verify(token string) bool {
	raw, err := decodeToken(token)
	if err != nil || len(raw) != tokenBytes {
		return false
	}
	hash := sha256.Sum256(raw)

	m.mu.RLock()
	defer m.mu.RUnlock()
	for _, revoked := range m.revokedHash {
		if subtle.ConstantTimeCompare(hash[:], revoked[:]) == 1 {
			return false
		}
	}
	if len(m.activeToken) == 0 {
		return false
	}
	return subtle.ConstantTimeCompare(raw, m.activeToken) == 1
}

// Rotate creates a new active token and revokes the previous one.
func (m *Manager) Rotate() error {
	m.mu.Lock()
	defer m.mu.Unlock()

	newToken, err := randomToken()
	if err != nil {
		return err
	}
	newID, err := randomID()
	if err != nil {
		return err
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)

	revoked := append([][32]byte(nil), m.revokedHash...)
	revokedIDs := append([]string(nil), m.revokedIDs...)
	revokedRecords := m.recordsFromLocked(revoked, revokedIDs)
	if len(m.activeToken) > 0 {
		hash := sha256.Sum256(m.activeToken)
		revoked = append(revoked, hash)
		revokedIDs = append(revokedIDs, m.activeID)
		revokedRecords = append(revokedRecords, revokedRecord{
			ID:        m.activeID,
			Hash:      hex.EncodeToString(hash[:]),
			RevokedAt: now,
		})
	}
	protected, err := protectToken(newToken)
	if err != nil {
		return fmt.Errorf("protect installation token: %w", err)
	}

	if err := m.persistLocked(tokenFile{
		Version:     stateVersion,
		ActiveID:    newID,
		ActiveToken: base64.RawStdEncoding.EncodeToString(protected),
		Revoked:     revokedRecords,
		CreatedAt:   m.createdAt,
		UpdatedAt:   now,
	}); err != nil {
		return err
	}

	m.activeID = newID
	m.activeToken = newToken
	m.revokedHash = revoked
	m.revokedIDs = revokedIDs
	return nil
}

// Revoke permanently invalidates the active token. A new token must be
// created with Rotate; this method never silently generates a replacement.
func (m *Manager) Revoke() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if len(m.activeToken) == 0 {
		return ErrNoActiveToken
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	hash := sha256.Sum256(m.activeToken)
	revoked := append([][32]byte(nil), m.revokedHash...)
	revokedIDs := append([]string(nil), m.revokedIDs...)
	revoked = append(revoked, hash)
	revokedIDs = append(revokedIDs, m.activeID)
	records := m.recordsFromLocked(revoked, revokedIDs)
	records[len(records)-1].RevokedAt = now
	if err := m.persistLocked(tokenFile{
		Version:   stateVersion,
		Revoked:   records,
		CreatedAt: m.createdAt,
		UpdatedAt: now,
	}); err != nil {
		return err
	}
	m.activeID = ""
	m.activeToken = nil
	m.revokedHash = revoked
	m.revokedIDs = revokedIDs
	return nil
}

func (m *Manager) create() error {
	token, err := randomToken()
	if err != nil {
		return err
	}
	id, err := randomID()
	if err != nil {
		return err
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	protected, err := protectToken(token)
	if err != nil {
		return fmt.Errorf("protect installation token: %w", err)
	}
	state := tokenFile{
		Version:     stateVersion,
		ActiveID:    id,
		ActiveToken: base64.RawStdEncoding.EncodeToString(protected),
		CreatedAt:   now,
		UpdatedAt:   now,
	}
	if err := m.persistLocked(state); err != nil {
		return err
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	m.activeID = id
	m.activeToken = token
	m.revokedHash = nil
	m.revokedIDs = nil
	m.createdAt = now
	return nil
}

func (m *Manager) persistLocked(state tokenFile) error {
	// persistLocked is called with m.mu held. It preserves revoked IDs from
	// the in-memory record list; Load rebuilds that list on restart.
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return fmt.Errorf("encode installation token state: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(m.path), 0700); err != nil {
		return fmt.Errorf("create token store directory: %w", err)
	}
	tmp := m.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0600); err != nil {
		return fmt.Errorf("write installation token state: %w", err)
	}
	if err := os.Rename(tmp, m.path); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("install token state: %w", err)
	}
	return nil
}

func (m *Manager) recordsFromLocked(hashes [][32]byte, ids []string) []revokedRecord {
	records := make([]revokedRecord, 0, len(hashes))
	for i := range hashes {
		id := ""
		if i < len(ids) {
			id = ids[i]
		}
		records = append(records, revokedRecord{ID: id, Hash: hex.EncodeToString(hashes[i][:])})
	}
	return records
}

func randomToken() ([]byte, error) {
	buf := make([]byte, tokenBytes)
	if _, err := rand.Read(buf); err != nil {
		return nil, fmt.Errorf("generate installation token: %w", err)
	}
	return buf, nil
}

func randomID() (string, error) {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return "", fmt.Errorf("generate token identifier: %w", err)
	}
	return hex.EncodeToString(buf), nil
}

func encodeToken(raw []byte) string {
	return base64.RawURLEncoding.EncodeToString(raw)
}

func decodeToken(value string) ([]byte, error) {
	return base64.RawURLEncoding.DecodeString(value)
}
