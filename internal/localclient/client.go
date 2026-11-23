// Package localclient is the trusted CLI client for the authenticated loopback
// API. It never prints or persists bearer tokens.
package localclient

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"crane/localapi"
	"crane/localauth"
	"crane/persistence"
)

// Client reads the installation token on every call so rotation takes effect
// immediately without caching a secret in a long-lived CLI process.
type Client struct {
	baseURL string
	auth    *localauth.Manager
	http    *http.Client
}

// New prepares a client for the loopback API.
func New() (*Client, error) {
	auth, err := localauth.NewManager(localauth.DefaultPath())
	if err != nil {
		return nil, err
	}
	return &Client{
		baseURL: fmt.Sprintf("http://127.0.0.1:%d", localapi.DefaultPort()),
		auth:    auth,
		http:    &http.Client{Timeout: 15 * time.Second},
	}, nil
}

// Health reports the supervisor's own readiness probe.
func (c *Client) Health(ctx context.Context) (localapi.HealthResponse, error) {
	var response localapi.HealthResponse
	data, err := c.do(ctx, http.MethodGet, "/v1/health", nil)
	if err != nil {
		return response, err
	}
	if err := unmarshalEnvelope(data, &response); err != nil {
		return response, err
	}
	return response, nil
}

// CreateTask submits one task to the running supervisor.
func (c *Client) CreateTask(ctx context.Context, id, input string) (persistence.TaskRecord, error) {
	return c.CreateTaskWithContext(ctx, id, input, "", nil, "")
}

// ConversationMessage mirrors the API conversation message payload.
type ConversationMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// CreateTaskWithContext submits one task with optional conversation context and workspace.
func (c *Client) CreateTaskWithContext(ctx context.Context, id, input, conversationID string, messages []ConversationMessage, workspace string) (persistence.TaskRecord, error) {
	var response struct {
		Task persistence.TaskRecord `json:"task"`
	}
	payload := map[string]any{"id": id, "input": input}
	if conversationID != "" {
		payload["conversation_id"] = conversationID
	}
	if len(messages) > 0 {
		payload["conversation_messages"] = messages
	}
	if strings.TrimSpace(workspace) != "" {
		payload["workspace"] = workspace
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return response.Task, err
	}
	data, err := c.do(ctx, http.MethodPost, "/v1/tasks", body)
	if err != nil {
		return response.Task, err
	}
	if err := unmarshalEnvelope(data, &response); err != nil {
		return response.Task, err
	}
	return response.Task, nil
}

// GetTask reads one persisted task through the API.
func (c *Client) GetTask(ctx context.Context, id string) (persistence.TaskRecord, error) {
	var response struct {
		Task persistence.TaskRecord `json:"task"`
	}
	if !persistence.ValidTaskID(id) {
		return response.Task, persistence.ErrTaskNotFound
	}
	data, err := c.do(ctx, http.MethodGet, "/v1/tasks/"+id, nil)
	if err != nil {
		return response.Task, err
	}
	if err := unmarshalEnvelope(data, &response); err != nil {
		return response.Task, err
	}
	return response.Task, nil
}

// CancelTask requests supervisor-owned cancellation.
func (c *Client) CancelTask(ctx context.Context, id string) (persistence.TaskRecord, error) {
	var response struct {
		Task persistence.TaskRecord `json:"task"`
	}
	if !persistence.ValidTaskID(id) {
		return response.Task, persistence.ErrTaskNotFound
	}
	data, err := c.do(ctx, http.MethodPost, "/v1/tasks/"+id+"/cancel", nil)
	if err != nil {
		return response.Task, err
	}
	if err := unmarshalEnvelope(data, &response); err != nil {
		return response.Task, err
	}
	return response.Task, nil
}

// ApproveTask approves or rejects a waiting approval and resumes if approved.
func (c *Client) ApproveTask(ctx context.Context, id string, approved bool, scope string) (persistence.TaskRecord, error) {
	var response struct {
		Task persistence.TaskRecord `json:"task"`
	}
	if !persistence.ValidTaskID(id) {
		return response.Task, persistence.ErrTaskNotFound
	}
	payload := map[string]any{"approved": approved, "scope": scope}
	body, err := json.Marshal(payload)
	if err != nil {
		return response.Task, err
	}
	data, err := c.do(ctx, http.MethodPost, "/v1/tasks/"+id+"/approve", body)
	if err != nil {
		return response.Task, err
	}
	if err := unmarshalEnvelope(data, &response); err != nil {
		return response.Task, err
	}
	return response.Task, nil
}

// TaskEvent is a streamed task event from the local API SSE endpoint.
type TaskEvent struct {
	ID        int64           `json:"id"`
	TaskID    string          `json:"task_id"`
	Timestamp string          `json:"timestamp"`
	Type      string          `json:"type"`
	Payload   json.RawMessage `json:"payload"`
}

// StreamTaskEvents subscribes to task SSE events and calls onEvent for each event.
// It returns when context is cancelled, the stream ends, or an error occurs.
func (c *Client) StreamTaskEvents(ctx context.Context, taskID string, after int64, onEvent func(TaskEvent) error) error {
	if !persistence.ValidTaskID(taskID) {
		return persistence.ErrTaskNotFound
	}
	if err := c.auth.Load(); err != nil {
		return errors.New("installation token is unavailable; start the CRANE supervisor first")
	}
	token, err := c.auth.CurrentToken()
	if err != nil {
		return errors.New("installation token is unavailable; start the CRANE supervisor first")
	}
	path := "/v1/tasks/" + taskID + "/events"
	if after > 0 {
		path += "?cursor=" + strconv.FormatInt(after, 10)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "text/event-stream")
	if after > 0 {
		req.Header.Set("Last-Event-ID", strconv.FormatInt(after, 10))
	}
	streamClient := &http.Client{Timeout: 0}
	resp, err := streamClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		code := apiErrorCode(data)
		if code == "" {
			code = http.StatusText(resp.StatusCode)
		}
		return fmt.Errorf("local API: %s", code)
	}
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 64*1024), 1<<20)
	var eventType string
	var dataLines []string
	var eventID int64
	dispatch := func() error {
		if eventType == "" && len(dataLines) == 0 {
			return nil
		}
		data := strings.Join(dataLines, "\n")
		ev := TaskEvent{ID: eventID, TaskID: taskID, Type: eventType, Payload: json.RawMessage(data)}
		eventType = ""
		dataLines = nil
		eventID = 0
		if ev.Type == "" {
			return nil
		}
		return onEvent(ev)
	}
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if err := dispatch(); err != nil {
				return err
			}
			continue
		}
		if strings.HasPrefix(line, ":") {
			continue
		}
		if strings.HasPrefix(line, "event:") {
			eventType = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
			continue
		}
		if strings.HasPrefix(line, "id:") {
			if v, err := strconv.ParseInt(strings.TrimSpace(strings.TrimPrefix(line, "id:")), 10, 64); err == nil {
				eventID = v
			}
			continue
		}
		if strings.HasPrefix(line, "data:") {
			dataLines = append(dataLines, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
		}
	}
	if err := scanner.Err(); err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return err
	}
	if err := dispatch(); err != nil {
		return err
	}
	return nil
}

// MCPStatus reports live MCP controller state.
func (c *Client) MCPStatus(ctx context.Context) (MCPStatusResponse, error) {
	var response MCPStatusResponse
	data, err := c.do(ctx, http.MethodGet, "/v1/mcp/status", nil)
	if err != nil {
		return response, err
	}
	if err := unmarshalEnvelope(data, &response); err != nil {
		return response, err
	}
	return response, nil
}

// MCPStatusResponse mirrors the API-safe MCP status envelope.
type MCPStatusResponse struct {
	Servers     []MCPServerStatus `json:"servers"`
	Invocations []json.RawMessage `json:"invocations"`
	Approvals   []json.RawMessage `json:"approvals"`
}

// MCPServerStatus is the subset needed by diagnostics.
type MCPServerStatus struct {
	ID        string `json:"id"`
	Connected bool   `json:"connected"`
	ToolCount int    `json:"tool_count"`
}

func (c *Client) do(ctx context.Context, method, path string, body []byte) ([]byte, error) {
	if err := c.auth.Load(); err != nil {
		return nil, errors.New("installation token is unavailable; start the CRANE supervisor first")
	}
	token, err := c.auth.CurrentToken()
	if err != nil {
		return nil, errors.New("installation token is unavailable; start the CRANE supervisor first")
	}
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	request, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reader)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Authorization", "Bearer "+token)
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := c.http.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	data, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	if response.StatusCode >= 400 {
		code := apiErrorCode(data)
		if code == "" {
			code = http.StatusText(response.StatusCode)
		}
		return nil, fmt.Errorf("local API: %s", code)
	}
	return data, nil
}

func unmarshalEnvelope(data []byte, target any) error {
	var envelope struct {
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(data, &envelope); err != nil {
		return errors.New("local API returned an invalid response")
	}
	if len(envelope.Data) == 0 {
		return errors.New("local API returned an empty response")
	}
	if err := json.Unmarshal(envelope.Data, target); err != nil {
		return errors.New("local API returned an invalid response")
	}
	return nil
}

func apiErrorCode(data []byte) string {
	var body struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if json.Unmarshal(data, &body) != nil {
		return ""
	}
	return strings.TrimSpace(body.Error.Code)
}

func (c *Client) CreateConversation(ctx context.Context, id, title string) (persistence.ConversationRecord, error) {
	var response struct {
		Conversation persistence.ConversationRecord `json:"conversation"`
	}
	body, err := json.Marshal(map[string]string{"id": id, "title": title})
	if err != nil {
		return response.Conversation, err
	}
	data, err := c.do(ctx, http.MethodPost, "/v1/conversations", body)
	if err != nil {
		return response.Conversation, err
	}
	if err := unmarshalEnvelope(data, &response); err != nil {
		return response.Conversation, err
	}
	return response.Conversation, nil
}

func (c *Client) GetConversation(ctx context.Context, id string) (persistence.ConversationRecord, error) {
	var response struct {
		Conversation persistence.ConversationRecord `json:"conversation"`
	}
	if !persistence.ValidConversationID(id) {
		return response.Conversation, fmt.Errorf("conversation not found: %s", id)
	}
	data, err := c.do(ctx, http.MethodGet, "/v1/conversations/"+id, nil)
	if err != nil {
		return response.Conversation, err
	}
	if err := unmarshalEnvelope(data, &response); err != nil {
		return response.Conversation, err
	}
	return response.Conversation, nil
}

func (c *Client) ListConversations(ctx context.Context, limit int) ([]persistence.ConversationRecord, error) {
	var response struct {
		Conversations []persistence.ConversationRecord `json:"conversations"`
	}
	path := "/v1/conversations"
	if limit > 0 {
		path = fmt.Sprintf("/v1/conversations?limit=%d", limit)
	}
	data, err := c.do(ctx, http.MethodGet, path, nil)
	if err != nil {
		return nil, err
	}
	if err := unmarshalEnvelope(data, &response); err != nil {
		return nil, err
	}
	return response.Conversations, nil
}

func (c *Client) GetConversationMessages(ctx context.Context, id string, limit int) ([]persistence.ConversationMessage, error) {
	var response struct {
		Messages []persistence.ConversationMessage `json:"messages"`
	}
	if !persistence.ValidConversationID(id) {
		return nil, fmt.Errorf("conversation not found: %s", id)
	}
	path := "/v1/conversations/" + id + "/messages"
	if limit > 0 {
		path = fmt.Sprintf("%s?limit=%d", path, limit)
	}
	data, err := c.do(ctx, http.MethodGet, path, nil)
	if err != nil {
		return nil, err
	}
	if err := unmarshalEnvelope(data, &response); err != nil {
		return nil, err
	}
	return response.Messages, nil
}

func (c *Client) AppendConversationMessage(ctx context.Context, id, role, content, metadata string) (persistence.ConversationMessage, error) {
	var response struct {
		Message persistence.ConversationMessage `json:"message"`
	}
	payload := map[string]string{"role": role, "content": content}
	if metadata != "" {
		payload["metadata"] = metadata
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return response.Message, err
	}
	data, err := c.do(ctx, http.MethodPost, "/v1/conversations/"+id+"/messages", body)
	if err != nil {
		return response.Message, err
	}
	if err := unmarshalEnvelope(data, &response); err != nil {
		return response.Message, err
	}
	return response.Message, nil
}
