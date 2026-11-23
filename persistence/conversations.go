package persistence

import (
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"
)

const maxConversationIDLength = 200

// ConversationRecord is the persisted metadata for one chat conversation.
type ConversationRecord struct {
	ID            string `json:"id"`
	Title         string `json:"title"`
	CreatedAt     string `json:"created_at"`
	UpdatedAt     string `json:"updated_at"`
	LastMessageAt string `json:"last_message_at,omitempty"`
	MessageCount  int    `json:"message_count"`
}

// ConversationMessage is one persisted chat message.
type ConversationMessage struct {
	ID             int64  `json:"id"`
	ConversationID string `json:"conversation_id"`
	Role           string `json:"role"`
	Content        string `json:"content"`
	CreatedAt      string `json:"created_at"`
	Metadata       string `json:"metadata,omitempty"`
}

// ValidConversationID restricts conversation IDs to the same safe portable set as task IDs.
func ValidConversationID(id string) bool {
	if id == "" || len(id) > maxConversationIDLength {
		return false
	}
	for _, r := range id {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
		case r == '-', r == '_', r == '.', r == ':':
		default:
			return false
		}
	}
	return true
}

// CreateConversation persists a new conversation record.
func (s *Store) CreateConversation(id, title string) (ConversationRecord, error) {
	if !ValidConversationID(id) {
		return ConversationRecord{}, errors.New("invalid conversation identifier")
	}
	title = strings.TrimSpace(title)
	if len(title) > 200 {
		title = title[:200]
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	_, err := s.db.Exec(
		"INSERT INTO conversations (id, title, created_at, updated_at, last_message_at, message_count) VALUES (?, ?, ?, ?, ?, 0)",
		id, title, now, now, "",
	)
	if err != nil {
		if strings.Contains(strings.ToLower(err.Error()), "unique") {
			return ConversationRecord{}, fmt.Errorf("conversation already exists: %s", id)
		}
		return ConversationRecord{}, fmt.Errorf("create conversation: %w", err)
	}
	return s.GetConversation(id)
}

// GetConversation returns one conversation record.
func (s *Store) GetConversation(id string) (ConversationRecord, error) {
	if !ValidConversationID(id) {
		return ConversationRecord{}, errors.New("invalid conversation identifier")
	}
	var record ConversationRecord
	var lastMessageAt sql.NullString
	err := s.db.QueryRow(
		"SELECT id, title, created_at, updated_at, last_message_at, message_count FROM conversations WHERE id = ?",
		id,
	).Scan(&record.ID, &record.Title, &record.CreatedAt, &record.UpdatedAt, &lastMessageAt, &record.MessageCount)
	if errors.Is(err, sql.ErrNoRows) {
		return ConversationRecord{}, fmt.Errorf("conversation not found: %s", id)
	}
	if err != nil {
		return ConversationRecord{}, fmt.Errorf("get conversation: %w", err)
	}
	if lastMessageAt.Valid {
		record.LastMessageAt = lastMessageAt.String
	}
	return record, nil
}

// ListConversations returns conversations ordered by most recent activity.
func (s *Store) ListConversations(limit int) ([]ConversationRecord, error) {
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	rows, err := s.db.Query(
		"SELECT id, title, created_at, updated_at, last_message_at, message_count FROM conversations ORDER BY updated_at DESC, id DESC LIMIT ?",
		limit,
	)
	if err != nil {
		return nil, fmt.Errorf("list conversations: %w", err)
	}
	defer rows.Close()
	records := make([]ConversationRecord, 0)
	for rows.Next() {
		var record ConversationRecord
		var lastMessageAt sql.NullString
		if err := rows.Scan(&record.ID, &record.Title, &record.CreatedAt, &record.UpdatedAt, &lastMessageAt, &record.MessageCount); err != nil {
			return nil, fmt.Errorf("scan conversation: %w", err)
		}
		if lastMessageAt.Valid {
			record.LastMessageAt = lastMessageAt.String
		}
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate conversations: %w", err)
	}
	return records, nil
}

// AppendConversationMessage persists a message and updates conversation metadata.
func (s *Store) AppendConversationMessage(conversationID, role, content, metadata string) (ConversationMessage, error) {
	if !ValidConversationID(conversationID) {
		return ConversationMessage{}, errors.New("invalid conversation identifier")
	}
	role = strings.TrimSpace(strings.ToLower(role))
	switch role {
	case "user", "assistant", "system":
	default:
		return ConversationMessage{}, fmt.Errorf("unsupported conversation role %q", role)
	}
	if strings.TrimSpace(content) == "" {
		return ConversationMessage{}, errors.New("message content must not be empty")
	}
	if metadata == "" {
		metadata = "{}"
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	tx, err := s.db.Begin()
	if err != nil {
		return ConversationMessage{}, fmt.Errorf("begin conversation message transaction: %w", err)
	}
	defer tx.Rollback()

	var exists int
	if err := tx.QueryRow("SELECT 1 FROM conversations WHERE id = ?", conversationID).Scan(&exists); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return ConversationMessage{}, fmt.Errorf("conversation not found: %s", conversationID)
		}
		return ConversationMessage{}, fmt.Errorf("check conversation: %w", err)
	}
	result, err := tx.Exec(
		"INSERT INTO conversation_messages (conversation_id, role, content, created_at, metadata) VALUES (?, ?, ?, ?, ?)",
		conversationID, role, content, now, metadata,
	)
	if err != nil {
		return ConversationMessage{}, fmt.Errorf("append conversation message: %w", err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		return ConversationMessage{}, fmt.Errorf("read conversation message id: %w", err)
	}
	if _, err := tx.Exec(
		"UPDATE conversations SET updated_at = ?, last_message_at = ?, message_count = message_count + 1, title = CASE WHEN title = '' AND ? = 'user' THEN ? ELSE title END WHERE id = ?",
		now, now, role, truncateConversationTitle(content), conversationID,
	); err != nil {
		return ConversationMessage{}, fmt.Errorf("update conversation metadata: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return ConversationMessage{}, fmt.Errorf("commit conversation message: %w", err)
	}
	return ConversationMessage{ID: id, ConversationID: conversationID, Role: role, Content: content, CreatedAt: now, Metadata: metadata}, nil
}

func truncateConversationTitle(content string) string {
	title := strings.TrimSpace(strings.Join(strings.Fields(content), " "))
	if len(title) > 80 {
		return title[:80]
	}
	return title
}

// GetConversationMessages returns messages in chronological order.
func (s *Store) GetConversationMessages(conversationID string, limit int) ([]ConversationMessage, error) {
	if !ValidConversationID(conversationID) {
		return nil, errors.New("invalid conversation identifier")
	}
	if limit <= 0 || limit > 500 {
		limit = 200
	}
	rows, err := s.db.Query(
		"SELECT id, conversation_id, role, content, created_at, metadata FROM conversation_messages WHERE conversation_id = ? ORDER BY id ASC LIMIT ?",
		conversationID, limit,
	)
	if err != nil {
		return nil, fmt.Errorf("list conversation messages: %w", err)
	}
	defer rows.Close()
	messages := make([]ConversationMessage, 0)
	for rows.Next() {
		var msg ConversationMessage
		var metadata sql.NullString
		if err := rows.Scan(&msg.ID, &msg.ConversationID, &msg.Role, &msg.Content, &msg.CreatedAt, &metadata); err != nil {
			return nil, fmt.Errorf("scan conversation message: %w", err)
		}
		if metadata.Valid {
			msg.Metadata = metadata.String
		}
		messages = append(messages, msg)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate conversation messages: %w", err)
	}
	return messages, nil
}

// GetRecentConversationMessages returns the most recent messages in chronological order.
func (s *Store) GetRecentConversationMessages(conversationID string, limit int) ([]ConversationMessage, error) {
	if !ValidConversationID(conversationID) {
		return nil, errors.New("invalid conversation identifier")
	}
	if limit <= 0 || limit > 100 {
		limit = 20
	}
	rows, err := s.db.Query(
		"SELECT id, conversation_id, role, content, created_at, metadata FROM (SELECT id, conversation_id, role, content, created_at, metadata FROM conversation_messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?) sub ORDER BY id ASC",
		conversationID, limit,
	)
	if err != nil {
		return nil, fmt.Errorf("list recent conversation messages: %w", err)
	}
	defer rows.Close()
	messages := make([]ConversationMessage, 0)
	for rows.Next() {
		var msg ConversationMessage
		var metadata sql.NullString
		if err := rows.Scan(&msg.ID, &msg.ConversationID, &msg.Role, &msg.Content, &msg.CreatedAt, &metadata); err != nil {
			return nil, fmt.Errorf("scan conversation message: %w", err)
		}
		if metadata.Valid {
			msg.Metadata = metadata.String
		}
		messages = append(messages, msg)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate conversation messages: %w", err)
	}
	return messages, nil
}
