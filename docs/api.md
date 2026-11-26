# HTTP API

服务默认监听 `0.0.0.0:8765`，无鉴权（同一局域网可访问，会消耗生图额度）。
所有接口返回 JSON（`Content-Type: application/json; charset=utf-8`），
导出接口除外。错误统一为 `{"error": "<code>", "message": "<中文说明>"}`。

## 图库（灵感库）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/bootstrap` | 首屏数据：条目、分类、风格列表、设置 |
| GET | `/api/items?category=<key>&limit=<n>` | 条目列表，可按分类过滤 |
| GET | `/api/items/{id}` | 单条条目详情（含 palette、拆解、提示词） |
| GET | `/api/items/{id}/prompt` | 只取提示词 |
| GET | `/api/export.csv` | 导出 metadata CSV（`text/csv`，UTF-8 BOM） |
| POST | `/api/items/{id}/generate` | 提交生成任务，返回 `202 {"job_id": "..."}` |
| POST | `/api/items/{id}/restore` | 把条目恢复为原始记录（撤销本地覆盖） |
| GET | `/api/jobs/{job_id}` | 轮询生成任务 |

`POST /api/items/{id}/generate` 请求体：

```json
{"prompt": "<提示词>", "size": "1080*1440", "negative": "<负面词>"}
```

`GET /api/jobs/{job_id}` 响应关键字段：`status`（`queued`/`running`/`done`/`error`）、
`stage`、`attempts`、`final_score`、`error`、`samples[]`（生成图 URL、尺寸、耗时）。

## 老房改造：项目（CRUD）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/remodel/items?limit=<n>&offset=<n>` | 项目列表，含封面缩略图、版本数、状态 |
| POST | `/api/remodel/items` | 新建项目（`multipart/form-data`：`file`（或 `photo`）、`name?`、`default_style?`、`default_mode?`）**不自动分析** |
| GET | `/api/remodel/items/{item_id}` | 项目详情（`item` + `gens[]` + `source[]`） |
| PATCH | `/api/remodel/items/{item_id}` | 改名 / 改封面：`{"name": "..."}` 或 `{"cover_gen": "<gen_id>"}` |
| DELETE | `/api/remodel/items/{item_id}` | 软删除到 `remodel/trash/`；加 `?purge=1` 永久删除 |
| POST | `/api/remodel/items/{item_id}/source` | 替换原图（`multipart/form-data`，`file`），旧原图保留在 `source/` |
| POST | `/api/remodel/items/{item_id}/analyze` | 分析原图结构，`{"force": true}` 强制重算 |
| POST | `/api/remodel/items/{item_id}/generate` | 提交生成任务，`202 {"job_id", "gen_id"}` |
| POST | `/api/remodel/items/{item_id}/detail-optimize` | 以最新结果图为底图做细节优化，追加新版本，`202 {"job_id", "gen_id"}` |
| GET | `/api/remodel/items/{item_id}/gens` | 版本列表（最新在前） |
| PATCH | `/api/remodel/items/{item_id}/gens/{gen_id}` | 改备注：`{"note": "..."}` |
| DELETE | `/api/remodel/items/{item_id}/gens/{gen_id}` | 删除单版（只删该版图片与 json） |
| GET | `/api/remodel/jobs/{job_id}` | 轮询改造任务（`stage`/`attempts`/`final_score`/`error`） |
| GET | `/api/remodel/packs` | 风格包（`style/style_packs.json`） |
| GET | `/api/remodel/history` | 旧版任务历史（兼容保留） |
| POST | `/api/remodel/upload` | 旧版上传入口（返回 `upload_id`） |
| POST | `/api/remodel/analyze` | 旧版分析入口（按 `upload_id`） |
| POST | `/api/remodel/generate` | 旧版生成入口（按 `upload_id`） |
| POST | `/api/remodel/recheck/{job_id}` | 旧版重新自查 |
| POST | `/api/remodel/thumbnails/backfill` | 为缺缩略图的项目补生成 |

`POST /api/remodel/items/{item_id}/generate` 请求体：

```json
{
  "mode": "img2img",
  "style": "北欧",
  "note": "第二次尝试",
  "detail_enabled": true,
  "detail_zh": "几盆绿植、两幅挂画"
}
```

- `mode`：`img2img`（默认，图生图）或 `text`（纯文本）
- `style`：风格包名称，必须是 `/api/remodel/packs` 里的 `styles[].name_zh`
- `note`：可选备注，写进该版本的 `gen json`
- `detail_enabled`：可选，布尔，默认 `false`。开启后让模型在现有台面/地面上补充小型装饰
  （绿植、挂画、织物、摆件等），不改变建筑结构。只接受 `true`/`false`/`"true"`/`"false"`/`1`/`0`，
  其他值返回 `400`
- `detail_zh`：可选，字符串，默认 `""`。中文（或英文）细节要求，`strip()` 后最长 300 字符，
  超长返回 `400`；开启时会先翻译成英文再拼进提示词。关闭时该字段被忽略

图生图**不要求**先有分析结果；纯文本模式若没有分析，任务会先跑 `analyzing` 阶段再生成。
`POST` 返回的 `job` 以及 `GET /api/remodel/jobs/{job_id}` 都会回传
`detail_enabled` 与 `detail_zh`，方便前端回填表单。

### 细节优化

`POST /api/remodel/items/{item_id}/detail-optimize` 以项目**最新一条结果的 PNG**
（按 `gen json` 的 `created_at` 取最新）作为 img2img 输入，只增加小型装饰细节，
生成一张**新的版本图**；旧版本不会被覆盖。

请求体：

```json
{
  "detail_zh": "几盆绿植、两幅挂画、叠搭地毯",
  "note": "细节优化"
}
```

- `detail_zh`：可选字符串，`strip()` 后最长 300 字符；超长返回 `400`
- `note`：可选备注，默认 `细节优化`，写进新版本的 `gen json`

校验与错误：

- 项目不存在 → `404 not_found`
- `detail_zh` 超过 300 字符 → `400 bad_request`
- 项目没有可用结果图（`gens` 为空，或对应 PNG 缺失）→
  `400 bad_request`，`message` 为 `还没有生成结果，无法做细节优化`

任务固定使用 `img2img` 与 `detail_enabled=true`；风格沿用最新版本（缺失时回落到项目默认风格）。
`POST` 返回以及 `GET /api/remodel/jobs/{job_id}` 额外回传：

| 字段 | 说明 |
| --- | --- |
| `base` | `"source"`（普通生成）或 `"current"`（细节优化） |
| `refine_of` | 源版本 `gen_id`；普通生成为空字符串 |
| `detail_enabled` / `detail_zh` | 与普通生成一致 |

新版本写入 `gen/<gid>.json` 时同样带 `detail_enabled`、`detail_zh`、`refine_of` 与 `note`。

## 凭据

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/credentials` | 返回 `source`、`key_masked`（`sk-…末4位`）、`base_url`、`has_local`、`env_present`，**从不返回明文** |
| POST | `/api/credentials` | 保存本地凭据：`{"api_key": "...", "base_url": "..."}` |
| DELETE | `/api/credentials` | 清除本地凭据；有环境变量时自动回落到环境变量 |

`source` 取值：`env`（环境变量）、`local`（`credentials.json`）、`none`（两者都没有）。
`none` 时网页顶部会显示「还没配置模型 Key，点右上「设置」填一个」。

## 静态资源

| 路径 | 说明 |
| --- | --- |
| `/` | 单页应用（`gui/index.html`） |
| `/gui/*.js`、`/gui/*.css` | 前端资源，`Cache-Control: no-cache, must-revalidate` + ETag |
| `/media/images/ai/<category>/<id>.webp` | 图库主图（AI 生成，WebP q88），按 `local_path` 拼路径，长缓存 |
| `/thumbs/<id>.jpg?v=<sha8>` | 图库列表缩略图（480 px 宽），URL 带内容版本号，换图自动失效缓存 |
| `/samples/*.png` | 生成样张，长缓存 |
| `/remodel/items/<id>/gen/<gen_id>.png` | 改造结果图，长缓存（文件名即版本，不会变） |
| `/remodel/thumbs/<id>.jpg` | 列表缩略图（长边 480），缺图时按需生成 |

## 通用错误码

| HTTP | `error` | 场景 |
| --- | --- | --- |
| 400 | `invalid_item_id` / `invalid_id` | 路径里的 ID 不合法 |
| 400 | `bad_request` | JSON 解析失败或缺字段 |
| 404 | `not_found` / `item_not_found` | 项目或版本不存在 |
| 409 | `busy` | 同一项目已有任务在跑 |
| 413 | `file_too_large` | 上传图片超过 12 MB |
| 500 | `internal` | 未预期异常（详情见 `logs/gui-service.log`） |

## curl 示例

```bash
# 列表
curl -s 'http://127.0.0.1:8765/api/remodel/items?limit=9' | jq '.items[0] | {item_id, name, gen_count}'

# 新建项目
curl -s -F 'file=@客厅.jpg' -F 'name=客厅测试' -F 'default_style=北欧' \
  http://127.0.0.1:8765/api/remodel/items

# 图生图（不需要分析）
curl -s -X POST http://127.0.0.1:8765/api/remodel/items/<item_id>/generate \
  -H 'Content-Type: application/json' \
  -d '{"mode":"img2img","style":"北欧"}' | jq -r .job_id

# 轮询
curl -s http://127.0.0.1:8765/api/remodel/jobs/<job_id> | jq '{status, stage, final_score, error}'

# 凭据状态（掩码）
curl -s http://127.0.0.1:8765/api/credentials
```