package python_protocol

import (
	"strings"
	"testing"
)

func TestMCPProtocolDecoders(t *testing.T) {
	command := `{"version":"0.1.0","type":"mcp_command","command_id":"cmd-1","operation":"invoke_tool","payload":{"server_id":"demo","tool_name":"get_test_echo","arguments":{"value":"x"}}}`
	decodedCommand, err := DecodeMCPCommand([]byte(command))
	if err != nil || decodedCommand.CommandID != "cmd-1" {
		t.Fatalf("DecodeMCPCommand() = %#v, %v", decodedCommand, err)
	}
	badCommand := strings.Replace(command, `"operation":"invoke_tool"`, `"operation":"shell"`, 1)
	if _, err := DecodeMCPCommand([]byte(badCommand)); err == nil {
		t.Fatal("unsupported MCP operation was accepted")
	}

	response := `{"version":"0.1.0","type":"mcp_response","command_id":"cmd-1","success":true,"result":{"ok":true}}`
	decodedResponse, err := DecodeMCPResponse([]byte(response))
	if err != nil || !decodedResponse.Success {
		t.Fatalf("DecodeMCPResponse() = %#v, %v", decodedResponse, err)
	}
	failed := `{"version":"0.1.0","type":"mcp_response","command_id":"cmd-1","success":false,"error":{"code":"mcp_failed","message":"safe detail"}}`
	if _, err := DecodeMCPResponse([]byte(failed)); err != nil {
		t.Fatalf("valid failed response rejected: %v", err)
	}

	toolResult := `{"version":"0.1.0","type":"tool_result","task_id":"task-1","invocation_id":"inv-1","server_id":"demo","server_name":"Demo","tool_name":"get_test_echo","arguments":{"value":"x"},"result":{"text":"x"},"policy_decision":"allowed","duration_ms":4,"status":"succeeded"}`
	decodedResult, err := DecodeToolResult([]byte(toolResult))
	if err != nil || decodedResult.Status != "succeeded" || decodedResult.InvocationID != "inv-1" {
		t.Fatalf("DecodeToolResult() = %#v, %v", decodedResult, err)
	}
	invalidStatus := strings.Replace(toolResult, `"status":"succeeded"`, `"status":"invented"`, 1)
	if _, err := DecodeToolResult([]byte(invalidStatus)); err == nil {
		t.Fatal("unsupported tool_result status was accepted")
	}

	submission := `{"version":"0.1.0","type":"task_submission","task_id":"task-1","input":"echo","mcp_servers":[{"server_id":"demo","name":"Demo","transport":"stdio","command":"python","args":["server.py"],"enabled":true}],"granted_approvals":["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]}`
	decodedSubmission, err := DecodeTaskSubmission([]byte(submission))
	if err != nil || len(decodedSubmission.MCPServers) != 1 || len(decodedSubmission.GrantedApprovals) != 1 {
		t.Fatalf("DecodeTaskSubmission() = %#v, %v", decodedSubmission, err)
	}
	badGrant := strings.Replace(submission, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "not-a-fingerprint", 1)
	if _, err := DecodeTaskSubmission([]byte(badGrant)); err == nil {
		t.Fatal("invalid approval fingerprint was accepted")
	}
	if !ValidateType(TypeMCPCommand) || !ValidateType(TypeMCPResponse) || !ValidateType(TypeToolResult) {
		t.Fatal("MCP protocol types are not recognized")
	}
}
