# 模型凭据配置

本项目**不需要把 Key 写进代码**。凭据统一由 `credentials.py` 解析，
所有生图、视觉分析、自查调用都走这一个入口。

## 读取顺序

| 顺序 | 来源 | 写法 |
| --- | --- | --- |
| 1 | 环境变量 | `HOMAI_DASHSCOPE_KEY` / `HOMAI_DASHSCOPE_BASE_URL` |
| 2 | 本地文件 | `credentials.json`（项目根目录，权限 `0600`） |

`api_key` 取**第一个非空**来源，`base_url` 同样按顺序取第一个非空值，
都没有就用默认网关 `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。
所以只填 Key 不填 Base URL 也能跑。

环境变量和本地文件都不存在时，接口返回 500，消息是：

```
未找到模型凭据：请在网页右上角「设置」填写模型 Key，或设置环境变量 HOMAI_DASHSCOPE_KEY / HOMAI_DASHSCOPE_BASE_URL
```

## 三种填法

### 1. 界面（推荐）

导航栏右上「设置」→ 粘贴 Key → 保存。保存后**下一次调用立刻生效**，不用重启。

- 尚未配置任何 Key 时，图库顶部会显示提示条「还没配置模型 Key，点右上「设置」填一个」，
  点提示条直接打开设置；本地 Key 或环境变量任一可用后，提示条自动消失。
- 弹窗里显示当前来源徽章（`本地 Key` / `环境变量` / `未配置`）、掩码 `sk-…末4位`、Base URL。
- 「清除本地凭据」= 删除 `credentials.json`；如果存在环境变量，会自动回落到环境变量。

### 2. 直接编辑文件

在项目根目录执行：

```bash
cat > credentials.json <<'JSON'
{
  "dashscope_api_key": "<在这里填你的 Key>",
  "dashscope_base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
}
JSON
chmod 600 credentials.json
```

字段名必须是 `dashscope_api_key` / `dashscope_base_url`。

### 3. 环境变量

```bash
export HOMAI_DASHSCOPE_KEY='<你的 Key>'
export HOMAI_DASHSCOPE_BASE_URL='https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
```

systemd 托管时写进 unit 的 `Environment=` 或 `EnvironmentFile=`；
注意 systemd 不会继承你交互 shell 里的 export，改完要 `systemctl --user daemon-reload`。

## 安全约定

- `credentials.json` 权限固定 `0600`（目录 `0700`），写入用临时文件 + `fsync` + 原子 `os.replace`。
- 该文件已进 `.gitignore`，**不会**被提交；`*.tmp` 中间文件也排除。
- Key 只在进程内存里使用；接口与界面只返回 `mask_api_key()` 的掩码（`sk-…abcd`）。
- 服务日志不打印 Key：`logs/gui-service.log` 里匹配 `sk-[A-Za-z0-9]{20,}` 应为 0 条。
- 不要在 README、issue、聊天记录里贴真实 Key；本文档只写占位符。

## 轮换与排查

```bash
# 看当前来源与掩码（不返回明文）
curl -s http://127.0.0.1:8765/api/credentials

# 写入 / 覆盖
curl -s -X POST http://127.0.0.1:8765/api/credentials \
  -H 'Content-Type: application/json' \
  -d '{"api_key":"<你的 Key>"}'

# 删除本地凭据；有环境变量时自动回落到环境变量
curl -s -X DELETE http://127.0.0.1:8765/api/credentials
```

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `未找到模型凭据` | 环境变量和本地文件都空 | 在右上「设置」填写，或设置环境变量 |
| 保存报「API Key 长度必须大于 20 个字符」 | Key 不完整 | 重新复制，别带空格 |
| 保存后仍报 401 | Key 无效或 Base URL 不对 | 弹窗里核对 Base URL |
| 改了环境变量没生效 | systemd 没 reload | `systemctl --user daemon-reload && systemctl --user restart homai-gallery` |