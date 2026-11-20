// Package localclient is the trusted CLI client for the authenticated loopback
// API. It never prints or persists bearer tokens.
package localclient

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
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
	var response struct {
		Task persistence.TaskRecord `json:"task"`
	}
	body, err := json.Marshal(map[string]string{"id": id, "input": input})
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
