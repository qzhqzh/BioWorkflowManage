# Reference Connector curl 流程

准备只存在当前 shell 的调用变量：

```bash
export CONNECTOR_URL=http://127.0.0.1:8090
export CONNECTOR_TOKEN="$REFERENCE_CONNECTOR_INBOUND_TOKEN"
export EXTERNAL_RUN_ID=MES-20260830-001
```

产品发现：

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer $CONNECTOR_TOKEN" \
  "$CONNECTOR_URL/v1/products"
```

提交订单。网络超时后可以原样重发；不要生成新的业务执行 ID，也不要修改订单内容：

```bash
curl --fail-with-body -sS -X POST \
  -H "Authorization: Bearer $CONNECTOR_TOKEN" \
  -H 'Content-Type: application/json' \
  "$CONNECTOR_URL/v1/orders" \
  --data-binary @mes-order.example.json
```

本地状态与主动对账：

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer $CONNECTOR_TOKEN" \
  "$CONNECTOR_URL/v1/orders/$EXTERNAL_RUN_ID"

curl --fail-with-body -sS -X POST \
  -H "Authorization: Bearer $CONNECTOR_TOKEN" \
  -H 'Content-Type: application/json' \
  "$CONNECTOR_URL/v1/orders/$EXTERNAL_RUN_ID/reconcile" \
  --data '{}'
```

小结果由 Connector 下载到受限结果目录，并逐文件校验大小和 SHA-256。响应中的 file result 带有
`download_url` 和 `sha256`：

```bash
RESULTS=$(curl --fail-with-body -sS -X POST \
  -H "Authorization: Bearer $CONNECTOR_TOKEN" \
  -H 'Content-Type: application/json' \
  "$CONNECTOR_URL/v1/orders/$EXTERNAL_RUN_ID/results" \
  --data '{}')

DOWNLOAD_URL=$(jq -r '.outputs[0].download_url' <<< "$RESULTS")
EXPECTED_SHA256=$(jq -r '.outputs[0].sha256 | sub("^sha256:"; "")' <<< "$RESULTS")
curl --fail-with-body -sS \
  -H "Authorization: Bearer $CONNECTOR_TOKEN" \
  "$CONNECTOR_URL$DOWNLOAD_URL" \
  --output result.bin
printf '%s  %s\n' "$EXPECTED_SHA256" result.bin | sha256sum --check -
```

大结果优先使用部署侧预配置的 Artifact Export profile。业务请求不能携带 endpoint 或凭据：

```bash
curl --fail-with-body -sS -X POST \
  -H "Authorization: Bearer $CONNECTOR_TOKEN" \
  -H 'Content-Type: application/json' \
  "$CONNECTOR_URL/v1/orders/$EXTERNAL_RUN_ID/exports" \
  --data '{"profile":"mes-results","requires_ack":true}'
```

收到 `analysis.artifact_export.completed` Webhook 后，或轮询确认 export 成功后，校验固化清单并
生成一次性业务回执：

```bash
curl --fail-with-body -sS -X POST \
  -H "Authorization: Bearer $CONNECTOR_TOKEN" \
  -H 'Content-Type: application/json' \
  "$CONNECTOR_URL/v1/orders/$EXTERNAL_RUN_ID/complete-export" \
  --data '{}'
```

Webhook 地址为：

```text
https://<connector-public-host>/v1/webhooks/bioworkflow
```

该路由不用 MES Bearer Token，而是验证 Analysis Node 的 delivery/event/timestamp/secret-version
headers 和 HMAC-SHA256 签名。不要让外层代理改写原始 body。
