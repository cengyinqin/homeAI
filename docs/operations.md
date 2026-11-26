# 运维手册

## 服务

```bash
systemctl --user status homai-gallery        # 状态
systemctl --user restart homai-gallery       # 重启（改 gui_server.py / credentials.py 后必须）
systemctl --user stop homai-gallery          # 停止
journalctl --user -u homai-gallery -n 50     # 服务日志（stdout 另存见下）
```

- unit：`/home/woker/.config/systemd/user/homai-gallery.service`
- 监听：`0.0.0.0:8765`（同一局域网可直接访问，**无鉴权**，会消耗生图额度）
- 解释器：项目内 `.venv/bin/python`
- 日志：`logs/gui-service.log`（`StandardOutput` / `StandardError` 都追加到这里）
- 重启策略：`Restart=on-failure`、`RestartSec=3`

只改了 `gui/*.js`、`gui/*.css`、`gui/index.html` 不用重启——静态资源是
`no-cache, must-revalidate`，刷新即可拿到新文件。改了 `*.py` 必须重启。

## 首次启动与示例数据

启动时会自动导入仓库自带的示例数据（10 张图库 + 5 个改造项目）：
`metadata.json` 不存在就从 `demo/` 复制图库索引，`images/ai` 没有 WebP 就导入示例图，
`remodel/items` 为空就导入改造项目。已有文件一律不覆盖，重复启动不会重复导入。
首次导入时日志会出现一行 `已导入示例数据 10 张图库 / 5 个改造项目`。

健康检查：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/
curl -s http://127.0.0.1:8765/api/credentials | jq .
```

## 端口被占 / 服务一直重启（踩过的坑）

现象：`restart` 返回成功，但接口行为还是旧逻辑，`journalctl` 里反复出现
`OSError: [Errno 98] Address already in use`。

原因：有人（或上一次调试）用 `.venv/bin/python gui_server.py --host 0.0.0.0 --port 8765`
在前台或后台起过一个**不受 systemd 管理**的孤儿进程，占着 8765，
systemd 那个起不来，真正响应请求的是孤儿进程。

排查与修复：

```bash
ss -ltnp | grep 8765                 # 看 PID 是谁
ps -o pid,etime,cmd -p <PID>         # 不是 systemd 管的（没有 systemd 父进程、etime 很长）
kill <PID>
systemctl --user reset-failed homai-gallery
systemctl --user restart homai-gallery
systemctl --user is-active homai-gallery   # 期望 active
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/
```

判断"当前跑的是不是新代码"的两个可靠办法：

```bash
grep -c '关键词' gui_server.py                 # 源码里有
curl -s http://127.0.0.1:8765/api/<新接口>      # 服务上也有（404 说明跑的是旧进程）
```

## 测试

在项目根目录执行：

```bash
.venv/bin/python test_credentials.py     # 凭据解析单测（优先级、掩码、0600、清除）
.venv/bin/python gui_smoke.py            # HTTP 冒烟，自带隔离数据目录 + 桩模型
```

`gui_smoke.py` 会在 `127.0.0.1:8799` 起一个临时实例（`HOMAI_SMOKE=1` +
隔离的 `HOMAI_REMODEL_DIR`），**不会**碰 `remodel/items/`，也**不会**真调付费生图。
正常输出末尾是 `N PASS / 0 FAIL`，在仓库现有运行时数据下基线为 **116 PASS**
（114 项原有 + 2 项示例数据）。
失败时会把出错的断言和响应体贴出来，先看最后 20 行。

## 超时与队列

| 环节 | 预算 |
| --- | --- |
| 结构分析 | 180 s |
| 生图请求 | 900 s（原 720 s，网关变慢后上调） |
| 结果图下载 | 180 s |
| 单次自查 | 240 s |
| reaper 扫描间隔 | 30 s |

阶段超时统一抛 `RemodelTimeoutError`，任务以 `status=error` + 中文文案结束，
失败任务会出现在版本历史里（紧凑失败卡），不会静默挂死。
阶段切换会向日志追加 `remodel <job8> <stage> +<elapsed>s`，不含密钥。

## 静态资源与缓存

| 资源 | 缓存策略 |
| --- | --- |
| `gui/*.js`、`gui/*.css`、`/` | `no-cache, must-revalidate` + `ETag` |
| `images/`、`samples/`、`remodel/.../gen/*.png` | 长缓存（`immutable`），文件名即版本，内容不变 |

历史坑：早期 JS 走长缓存，普通刷新拿到旧 `remodel.js`，改完像没生效。
现在的规则是"代码不缓存、媒体可长缓存"，新增静态文件请沿用。

## 备份

Git 只跟踪代码、配置、文档、`demo/` 和 `style/`（见 `.gitignore`）。
运行数据必须单独备份；在项目根目录执行：

```bash
tar czf ~/backup/homai-gallery-$(date +%F).tar.gz \
  -C . metadata.json metadata.csv remodel images
```

`metadata.json` / `metadata.csv` 是图库索引，`remodel/` 是改造项目、原图与生成版本，
`images/` 是图库图片。首次启动会自动从 `demo/` 导入示例数据（10 张图库 + 5 个改造项目），
之后的图库与改造内容都由用户用自己的模型 Key 生成。

## 恢复被删除的项目

软删除的项目在 `remodel/trash/<item_id>-<ts>/`，结构同 `items/<item_id>/`：

```bash
mv remodel/trash/<item_id>-<ts> remodel/items/<item_id>
systemctl --user restart homai-gallery
```

`?purge=1` 删掉的目录不在 trash 里，只能从备份恢复。