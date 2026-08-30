#!/usr/bin/env python3
"""BioWorkflowManage MCP stdio bridge backed by the scoped Integration API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "bioworkflow-manage", "version": "0.1.0"}


def object_schema(properties: dict[str, Any], required: list[str] | None = None):
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


STRING = {"type": "string"}
OBJECT = {"type": "object", "additionalProperties": True}
TOOLS = [
    {
        "name": "list_analysis_products",
        "description": "列出外部系统可使用的稳定分析产品代码、契约版本和输入输出契约。",
        "inputSchema": object_schema({}),
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "get_analysis_product",
        "description": "按 analysis_code 与 contract_version 读取固定分析产品契约。",
        "inputSchema": object_schema(
            {"analysis_code": STRING, "contract_version": STRING},
            ["analysis_code", "contract_version"],
        ),
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "list_workflow_versions",
        "description": "列出可供外部系统固定引用的已发布 WorkflowVersion 及输入输出契约。",
        "inputSchema": object_schema({}),
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "get_workflow_version",
        "description": "读取一个固定 WorkflowVersion 的 digest、接口和运行就绪状态。",
        "inputSchema": object_schema({"version_id": {"type": "integer", "minimum": 1}}, ["version_id"]),
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "list_tools",
        "description": "查询已发布工具版本、输入输出、资源建议和 digest。",
        "inputSchema": object_schema({"tool_id": STRING}),
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "list_software",
        "description": "查询软件知识、注意事项、软件版本和容器镜像。",
        "inputSchema": object_schema({}),
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "preflight_workflow",
        "description": "用受管输入检查固定分析产品或 WorkflowVersion、资源和语义输出契约，不创建任务。",
        "inputSchema": object_schema(
            {
                "workflow": OBJECT,
                "analysis_product": OBJECT,
                "inputs": OBJECT,
                "database": OBJECT,
                "metadata": OBJECT,
            },
            ["inputs"],
        ),
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "submit_workflow",
        "description": "幂等投递固定分析产品或 WorkflowVersion；必须提供唯一 external_run_id 和 idempotency_key。",
        "inputSchema": object_schema(
            {
                "external_ref": OBJECT,
                "idempotency_key": STRING,
                "workflow": OBJECT,
                "analysis_product": OBJECT,
                "subject": OBJECT,
                "inputs": OBJECT,
                "database": OBJECT,
                "metadata": OBJECT,
            },
            ["external_ref", "idempotency_key", "subject", "inputs"],
        ),
        "annotations": {"readOnlyHint": False, "idempotentHint": True},
    },
    {
        "name": "preflight_task_test",
        "description": "检查一个不可变 ToolVersion 是否能用给定小数据独立测试，不创建运行。",
        "inputSchema": object_schema({"tool": OBJECT, "inputs": OBJECT}, ["tool", "inputs"]),
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "submit_task_test",
        "description": "幂等提交不可变 ToolVersion 的独立小数据测试。",
        "inputSchema": object_schema(
            {
                "external_ref": OBJECT,
                "idempotency_key": STRING,
                "tool": OBJECT,
                "inputs": OBJECT,
                "label": STRING,
            },
            ["external_ref", "idempotency_key", "tool", "inputs"],
        ),
        "annotations": {"readOnlyHint": False, "idempotentHint": True},
    },
    {
        "name": "get_run",
        "description": "查询本 Service Account 的运行状态、进度、错误、耗时与输出。",
        "inputSchema": object_schema({"run_id": STRING}, ["run_id"]),
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "find_run_by_external_ref",
        "description": "在投递响应丢失后，通过 external_run_id 找回运行。",
        "inputSchema": object_schema({"external_run_id": STRING}, ["external_run_id"]),
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "list_run_events",
        "description": "按事件 ID 增量获取运行日志和状态事件。",
        "inputSchema": object_schema(
            {"run_id": STRING, "after_id": {"type": "integer", "minimum": 0}},
            ["run_id"],
        ),
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "list_run_outputs",
        "description": "读取语义化输出清单、大小、摘要及受保护下载地址。",
        "inputSchema": object_schema({"run_id": STRING}, ["run_id"]),
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "cancel_run",
        "description": "安全取消排队或执行中的运行；重复调用保持幂等。",
        "inputSchema": object_schema({"run_id": STRING}, ["run_id"]),
        "annotations": {"readOnlyHint": False, "idempotentHint": True, "destructiveHint": True},
    },
    {
        "name": "retry_run",
        "description": "基于失败/取消运行的固定快照创建新运行，不覆盖原证据。",
        "inputSchema": object_schema(
            {
                "run_id": STRING,
                "external_ref": OBJECT,
                "idempotency_key": STRING,
                "metadata": OBJECT,
            },
            ["run_id", "external_ref", "idempotency_key"],
        ),
        "annotations": {"readOnlyHint": False, "idempotentHint": True},
    },
]


class APIClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        payload = None
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            **(headers or {}),
        }
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=payload,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                details = json.load(error)
            except (json.JSONDecodeError, UnicodeDecodeError):
                details = {"error": {"code": "HTTP_ERROR", "message": str(error)}}
            raise RuntimeError(json.dumps(details, ensure_ascii=False)) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"BioWorkflowManage unavailable: {error.reason}") from error


def tool_call(client: APIClient, name: str, arguments: dict[str, Any]) -> Any:
    if name == "list_analysis_products":
        return client.request("GET", "/api/v1/integration/analysis-products")
    if name == "get_analysis_product":
        analysis_code = urllib.parse.quote(arguments["analysis_code"], safe="")
        contract_version = urllib.parse.quote(arguments["contract_version"], safe="")
        return client.request(
            "GET",
            f"/api/v1/integration/analysis-products/{analysis_code}/versions/{contract_version}",
        )
    if name == "list_workflow_versions":
        return client.request("GET", "/api/v1/integration/workflow-versions")
    if name == "get_workflow_version":
        return client.request("GET", f"/api/v1/integration/workflow-versions/{arguments['version_id']}")
    if name == "list_tools":
        query = urllib.parse.urlencode({"tool_id": arguments.get("tool_id", "")})
        return client.request("GET", f"/api/v1/integration/tools?{query}")
    if name == "list_software":
        return client.request("GET", "/api/v1/integration/software")
    if name == "preflight_workflow":
        return client.request("POST", "/api/v1/integration/analysis-runs/preflight", body=arguments)
    if name == "submit_workflow":
        body = {key: value for key, value in arguments.items() if key != "idempotency_key"}
        return client.request(
            "POST",
            "/api/v1/integration/analysis-runs",
            body=body,
            headers={"Idempotency-Key": arguments["idempotency_key"]},
        )
    if name == "preflight_task_test":
        return client.request("POST", "/api/v1/integration/tool-test-runs/preflight", body=arguments)
    if name == "submit_task_test":
        body = {key: value for key, value in arguments.items() if key != "idempotency_key"}
        return client.request(
            "POST",
            "/api/v1/integration/tool-test-runs",
            body=body,
            headers={"Idempotency-Key": arguments["idempotency_key"]},
        )
    if name == "get_run":
        return client.request("GET", f"/api/v1/integration/analysis-runs/{arguments['run_id']}")
    if name == "find_run_by_external_ref":
        query = urllib.parse.urlencode({"external_run_id": arguments["external_run_id"]})
        return client.request("GET", f"/api/v1/integration/analysis-runs/by-external-ref?{query}")
    if name == "list_run_events":
        query = urllib.parse.urlencode({"after_id": arguments.get("after_id", 0)})
        return client.request(
            "GET",
            f"/api/v1/integration/analysis-runs/{arguments['run_id']}/events?{query}",
        )
    if name == "list_run_outputs":
        return client.request("GET", f"/api/v1/integration/analysis-runs/{arguments['run_id']}/outputs")
    if name == "cancel_run":
        return client.request("POST", f"/api/v1/integration/analysis-runs/{arguments['run_id']}/cancel", body={})
    if name == "retry_run":
        body = {
            key: value
            for key, value in arguments.items()
            if key not in {"run_id", "idempotency_key"}
        }
        return client.request(
            "POST",
            f"/api/v1/integration/analysis-runs/{arguments['run_id']}/retry",
            body=body,
            headers={"Idempotency-Key": arguments["idempotency_key"]},
        )
    raise ValueError(f"Unknown tool: {name}")


def validate_tool_arguments(name: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object")
    tool = next((item for item in TOOLS if item["name"] == name), None)
    if tool is None:
        raise KeyError(name)
    schema = tool["inputSchema"]
    allowed = set(schema.get("properties", {}))
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ValueError(f"Unknown tool arguments: {', '.join(unknown)}")
    missing = sorted(set(schema.get("required", [])) - set(arguments))
    if missing:
        raise ValueError(f"Missing required tool arguments: {', '.join(missing)}")
    return arguments


def response(request_id: Any, result: Any = None, error: Any = None) -> dict[str, Any]:
    value = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        value["error"] = error
    else:
        value["result"] = result
    return value


def handle(client: APIClient, message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")
    if message_id is None:
        return None
    if method == "initialize":
        return response(
            message_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "只使用受管相对路径；写操作需要稳定 external_run_id 和 idempotency_key。"
                    "优先先 preflight，再 submit；不要在输入 metadata 中发送患者、医院或医生身份信息。"
                ),
            },
        )
    if method == "ping":
        return response(message_id, {})
    if method == "tools/list":
        return response(message_id, {"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        try:
            arguments = validate_tool_arguments(name, arguments)
            value = tool_call(client, name, arguments)
            return response(
                message_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(value, ensure_ascii=False, indent=2, default=str),
                        }
                    ],
                    "structuredContent": value,
                    "isError": False,
                },
            )
        except KeyError:
            return response(
                message_id,
                error={"code": -32602, "message": f"Unknown tool: {name}"},
            )
        except Exception as error:
            return response(
                message_id,
                {
                    "content": [{"type": "text", "text": str(error)}],
                    "isError": True,
                },
            )
    return response(message_id, error={"code": -32601, "message": "Method not found"})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BIOWORKFLOW_URL", "http://127.0.0.1:8082"),
    )
    args = parser.parse_args()
    token = os.environ.get("BIOWORKFLOW_TOKEN", "").strip()
    if not token:
        print("BIOWORKFLOW_TOKEN is required", file=sys.stderr)
        return 2
    client = APIClient(args.base_url, token)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            result = handle(client, message)
        except Exception as error:
            result = response(None, error={"code": -32700, "message": str(error)})
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
