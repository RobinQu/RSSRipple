# 错误处理规范

### 响应示例

```json
// 成功
{
  "success": true,
  "data": { "id": "...", "name": "..." },
  "error": null,
  "meta": {}
}

// 失败
{
  "success": false,
  "data": null,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "downloader_id is required",
    "details": { "field": "downloader_id" }
  },
  "meta": {}
}

// 500 内部错误（dev_mode=true 时带 stack trace）
{
  "success": false,
  "data": null,
  "error": {
    "code": "INTERNAL_SERVER_ERROR",
    "message": "Unexpected error",
    "stack": "Traceback (most recent call last): ..."
  },
  "meta": {}
}
```

### 错误码清单

| 错误码 | HTTP 状态码 | 说明 |
|--------|-------------|------|
| `NOT_FOUND` | 404 | 请求的资源不存在 |
| `VALIDATION_ERROR` | 422 | 请求参数验证失败（字段缺失/格式错误/枚举非法） |
| `INVALID_FEED` | 422 | RSS URL 无效、不可达或解析失败 |
| `DUPLICATE_SUBMISSION` | 409 | 表单 Token 已被使用或重复提交 |
| `ALREADY_RUNNING` | 409 | 该 Channel/Agent 的后台任务已在执行中 |
| `TRANSMISSION_ERROR` | 502 | Transmission RPC 连接失败或操作失败（含认证失败、磁盘不足等） |
| `LLM_ERROR` | 502 | LLM 调用失败（未配置 Key、超时、响应解析失败） |
| `INTERNAL_SERVER_ERROR` | 500 | 未预期错误；dev_mode 下附 stack trace，生产环境隐藏 |

### 全局异常处理

- 所有 HTTP 异常（RequestValidationError、HTTPException）由全局 exception handler 转换为统一响应格式。
- 未捕获异常统一转换为 `INTERNAL_SERVER_ERROR`，并记录日志（含 request_id、用户、URL、堆栈）。
- SSE 流式端点（`analyze-stream`、`analyze-url-stream`）发生错误时发送 `event: error` 事件：`data: {"code": "...", "message": "..."}`。
- Task queue 中的任务异常被捕获并记录到对应 Channel/Agent 的 `last_fetch_error`/`last_run_status` 字段，不抛出到全局。

---

