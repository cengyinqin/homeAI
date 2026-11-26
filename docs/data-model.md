# 数据模型

图库部分是 `metadata.json`（唯一数据源，首次启动从 `demo/` 导入 10 条示例，
之后由用户用自己的模型 Key 生成扩充）+ 图片目录；这里只讲老房改造的**项目制存储**。

## 图库图片目录

```text
metadata.json                 # 图库索引，示例数据首次启动从 demo/ 导入
metadata.csv                  # 同上的 CSV 导出副本
images/ai/<category>/<id>.webp # 唯一对外图片，AI 生成效果图 WebP q88
samples/                      # 手动生图的历史样本 + index.json（不参与列表展示）
gui_cache/thumbs/<id>.jpg     # 列表缩略图缓存，480 px 宽，可随时删了重建
```

条目里与图片有关的字段：`local_path`、`width/height`、`actual_width/actual_height`、
`bytes`、`sha256`、`ai_generated`、`ai_model`、`ai_source`（`generated` / `manual-sample`）、
`ai_prompt_en`。

## 目录结构

```text
remodel/
├── items/<item_id>/
│   ├── item.json              # 项目元数据，唯一可变文件
│   ├── source/
│   │   ├── <sid>.jpg          # 原图，替换时新增、不覆盖旧图
│   │   └── <sid>.json         # 原图记录（上传时间、原始文件名、尺寸、sha256）
│   ├── gen/
│   │   ├── <gid>.png          # 生成结果，一次生成一个文件，永不覆盖
│   │   └── <gid>.json         # 该版本的完整参数、提示词、评分、自查明细
│   └── thumb.jpg              # 列表封面缩略图（长边 480）
├── thumbs/<item_id>.jpg       # 列表用的统一缩略图缓存
├── uploads/<upload_id>.jpg    # 旧版入口的上传暂存
├── jobs/<job_id>.json         # 旧版任务记录（兼容保留）
└── trash/<item_id>-<ts>/      # 软删除的项目目录，结构同 items/
```

`item_id`、`gid`（版本 ID）、`sid`（原图 ID）都是 32 位小写十六进制（`uuid4().hex`），
后端用 `ITEM_ID_RE` / `GEN_ID_RE` 校验，非法直接 400。

## item.json

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `item_id` | str | 项目 ID，等于目录名 |
| `name` | str | 项目名，默认「空间 · 风格」，如「客厅 · 北欧」 |
| `created_at` / `updated_at` | ISO8601 | UTC，毫秒精度，带 `Z` |
| `notes` | str | 项目备注 |
| `default_style` | str | 上次使用的风格包名 |
| `default_mode` | str | `img2img` 或 `text` |
| `source_current` | str | 当前生效的原图 `sid` |
| `source_ids` | list[str] | 所有历史原图 `sid`，最新在后 |
| `cover_gen` | str \| null | 指定的封面版本；`null` 时用最新成功版本 |
| `gens` | list[str] | 成功版本的 `gid` 列表，最新在后 |
| `structure` | object \| null | 结构分析结果；**图生图不需要**，纯文本模式生成前必须有 |
| `analysis_error` | str \| null | 最近一次分析失败原因 |
| `renamed_by_user` | bool | 用户改过名后置 `true`，自动命名不再覆盖 |
| `legacy_upload_id` | str \| null | 从旧版 `jobs/` 迁移时的原始 `upload_id` |

### structure（分析结果）

版本化结构（`schema_version: 4`），关键字段：

- `room_type_en` / `shape_en` / `ceiling_en` / `walls_en` / `floor_en`：一句话描述
- `geometry`：`width_m`、`depth_m`、`ceiling_m`、`floor_level_changes`
- `openings[]`：`side`（left/front-right/...）、`type`、`width_m`、`height_m`、`sill_m`、
  `center_offset_fraction`、`detail`
- `openings_total`：洞口数量，生成时写成「exactly N；do not add or remove」
- `camera`：`height_m`、`distance_to_far_wall_m`、`lens_mm`、`tilt_deg`、`horizon_fraction`
- `framing`：墙线在画面中的归一化坐标（`from_x`/`to_x`/`ceiling_bottom_y`/`floor_top_y`）
  与 `opening_width_over_wall_width`
- `fixed_features[]`：必须保留的固定构件（顶角线、踢脚线、电箱、门框…）
- `immovable_zh` / `movable_zh`：界面上「不可改结构 / 可替换软装」两块的内容来源
- `structure_en`：拼装好的英文结构段落，直接进提示词
- `model` / `vision_calls` / `analyzed_at`：谁在什么时候分析的

## gen/<gid>.json

| 字段 | 说明 |
| --- | --- |
| `gid` / `item_id` / `job_id` | 版本、项目、任务 ID |
| `created_at` | 该版本生成时间（UTC） |
| `mode` | `img2img` 或 `text` |
| `style` | 风格包名 |
| `model` | 默认 `qwen-image-3.0-pro` |
| `prompt` / `negative` | 实际发出的完整提示词与负面词（可直接复现） |
| `score` | 自查分数 0-1（视觉模型逐项判断，不是像素比对） |
| `attempts` | 尝试次数，>1 表示触发过自动修复 |
| `size_requested` / `size_final` | 请求尺寸 vs 实际像素（如 `1440*1080` → `2352*1760`） |
| `width` / `height` | 实际像素 |
| `model_seconds` / `total_seconds` | 模型耗时 / 端到端耗时 |
| `checks[]` | 每轮自查明细：`attempt`、`score`、`items[{key, ok, note}]`、`repair_en` |
| `source_sid` | 这一版用的是哪张原图 |
| `note` | 用户备注 |

## 不变量

1. **版本不覆盖**：每次生成写新的 `<gid>.png` + `<gid>.json`；改备注只动 json。
2. **原图不覆盖**：替换原图新增 `<sid>.jpg`，`source_current` 指向新图，旧图留在 `source_ids`。
3. **只有 `item.json` 可变**：改名、改封面、改备注、更新 `gens` 都落在这一个文件；
   写盘走临时文件 + 原子替换。
4. **删除是软的**：`DELETE /api/remodel/items/{id}` 把整个目录移进 `trash/`，
   `?purge=1` 才真删；删除单版只删该 `gid` 的 png 与 json。
5. **封面回退**：`cover_gen` 为空或对应的 png 不存在时，列表回退到最新成功版本。

## 迁移

内置的迁移逻辑把旧版 `remodel/jobs/*.json` 按 `upload_id`
归并成项目：同名上传合并成多版本，写入 `legacy_upload_id` 以便追溯。
迁移是**幂等**的——重复执行不会产生新项目（第二次运行新建 0 个）。

## 体积参考

| 路径 | 体积 | 是否进 Git |
| --- | --- | --- |
| `demo/` | 约 26 MB（10 张图库 + 5 个改造项目） | 是 |
| `metadata.json` / `metadata.csv` | 首次启动从 `demo/` 复制 | 否（运行时数据） |
| `remodel/items/` | 随用户生成增长 | 否 |
| `gui_cache/` | 图库缩略图缓存，可随时重建 | 否 |
| `images/` | 首次启动从 `demo/` 导入，随用户生成增长 | 否 |
| `samples/` | 生成样张（不入库，界面上按需展示） | 否 |
| `style/style_packs.json` | 8 套风格包 | 是 |
| `style/items`、`style/palette` | 每条图的拆解与色板，首次启动从 `demo/` 导入 | 否 |
| 代码 + 文档 | < 10 MB | 是 |

完整数据请直接备份整个目录；Git 只跟踪可以文本 diff 的部分。